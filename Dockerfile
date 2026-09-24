# Digests re-resolved 2026-09-21 against the Docker Hub and GHCR v2 APIs.
#
# Every FROM reference below is `image:tag@sha256:...`. Both halves are
# load-bearing. The digest is the immutable content pin; the tag has to stay in
# the reference (not move into the comment) because that is what tells
# Dependabot which stream the pin belongs to and what to bump it to. A bare
# digest is immutable and unmaintained, which is just a slower kind of rot.

# Stage 0: the uv binary.
#
# This exists as a FROM stage rather than a COPY --from naming the registry
# image directly, and the difference is not cosmetic. Dependabot's docker file
# parser matches FROM directives only -- it deliberately skips builder-stage
# references on COPY lines -- so a digest written straight onto a COPY that
# named the registry image in its --from would never be updated. The previous
# form was worse still: it pulled an executable into the build from the
# floating tag, which can be repointed at new content between two builds of
# the same source.
#
# Pinned to the uv series this project builds with. pyproject.toml declares
# `requires = ["uv_build>=0.12.18,<0.13.0"]`, so a Dependabot bump across that
# ceiling needs the constraint widened in the same pull request -- which is
# the coupling surfacing where it can be reviewed, rather than breaking later.
FROM ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc AS uv

# Stage 1: Build.
#
# Every compiler, header and installer in this build lives in this stage and
# nowhere else. The runtime stage below receives one finished virtualenv,
# /opt/venv, and nothing that built it.
#
# The environment is made by `uv sync` with `--locked`, which refuses to run if
# uv.lock is out of date with pyproject.toml and checks every wheel and sdist
# it downloads against the sha256 the lock recorded. A substituted or
# republished artifact fails the build instead of shipping. That includes the
# build backend: python-sane publishes an sdist and no wheel, so it compiles
# here, and the setuptools that compiles it is not resolved fresh at build
# time. It is the lock's `build` dependency group, installed and hash-checked
# by the first sync below, and pyproject.toml builds python-sane against that
# installed copy with build isolation off.
#
# The two syncs are split on purpose. The first installs the dependencies and
# the build group from pyproject.toml and uv.lock alone; the second adds the
# project once its sources arrive, and drops the build group again because it
# is not named. A change to the sources therefore reruns only the second, not
# apt and not the python-sane compile. `--no-editable` installs the project
# itself into the venv rather than pointing it back at /app, so the venv is
# self-contained once copied.
FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2 AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libsane-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
# UV_PYTHON_DOWNLOADS and UV_PYTHON hold uv to this image's own interpreter.
# The runtime stage shares this base digest, so the venv's python symlink
# resolves there too; a uv-managed CPython would live outside /opt/venv, would
# not be copied, and would leave that symlink dangling. UV_COMPILE_BYTECODE
# precompiles every module, because /opt/venv is read-only to UID 1000 and an
# uncompiled venv would recompile in memory on every start.
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_PYTHON=/usr/local/bin/python3.14 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1
WORKDIR /app
# Named inputs, never the whole context. This is the second of TWO independent
# gates on what can reach a build layer; the first is the .dockerignore
# allow-list. Copying the whole context here would sweep in whatever the daemon
# was sent -- which, before this was written, included a real config file
# holding a live paperless-ngx API token, recoverable afterwards from the
# discarded builder stage. With both gates in place, a mistake in either one
# alone leaks nothing.
#
# README.md and LICENSE are inputs, not documentation: pyproject.toml's
# `readme` and PEP 639 `license-files` keys make the project build in the
# second sync fail outright if either file is absent. uv.lock is an input for
# the same reason: the locked sync refuses to run without it.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-default-groups --group build --no-install-project --no-editable
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-default-groups --no-editable

# Stage 2: Runtime.
#
# The same base digest as the builder, so the copied venv's interpreter path
# resolves. This one RUN is the only root-level system change in the stage:
#
# - libsane1 is the one package the compiled python-sane links against.
# - dll.conf enables exactly the `net` and `escl` backends. The stock list
#   enables about eighty, and SANE probes every one of them on the first device
#   enumeration -- measured at 8.5 seconds with no scanner attached. `net`
#   reaches a saned; `escl` reaches a network scanner directly. dll.d/ is
#   emptied so no package's drop-in file can add backends back.
# - pip and ensurepip's bundled wheels are removed, so a `docker exec` into a
#   running container finds no installer to fetch and run new code with. The
#   application's own packages are all in /opt/venv, which carries none.
FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2
RUN apt-get update && apt-get install -y --no-install-recommends libsane1 \
    && rm -rf /var/lib/apt/lists/* \
    && printf 'net\nescl\n' > /etc/sane.d/dll.conf \
    && rm -rf /etc/sane.d/dll.d/* \
    && PIP_ROOT_USER_ACTION=ignore python -m pip uninstall -y pip \
    && rm -f /usr/local/lib/python3.14/ensurepip/_bundled/*.whl
COPY --from=builder /opt/venv /opt/venv
# The exec-form ENTRYPOINT and the healthcheck's `python` both resolve through
# PATH, so both run from the venv.
ENV PATH=/opt/venv/bin:$PATH
# The application account. Everything above this line needs root -- apt and the
# system changes -- and everything below it does not.
#
# The UID and GID are fixed at 1000 on purpose. On the single-user Linux host
# this appliance targets, the operator's own ./config directory is already
# 1000:1000, so the read-write bind mount the config rewrite depends on works
# with no chown at all. A high UID such as 10001 was rejected precisely because
# it can never match: it would put a fix-up on every deployment's happy path. A
# PUID/PGID entrypoint was rejected too -- it still starts as root, which is the
# thing running as a non-root user exists to stop. Operators whose UID differs
# get a documented `chown -R 1000:1000 ./config` and a commented compose
# `user:` line instead; see docs/how-to/deploy-docker-compose.md.
#
# This RUN must stay strictly BEFORE the VOLUME below. Whether a build step
# that changes data inside a declared volume path afterwards survives depends
# on which builder ran -- Docker's reference says the legacy builder discards
# it, BuildKit keeps it -- and this order is the one that is correct under
# both. On the builder that discards, the wrong order means a fresh volume
# comes up owned by root and the app cannot write its job database. A local
# build proving otherwise proves nothing: buildah was measured keeping the
# change even with the order wrong.
RUN groupadd --gid 1000 saneless \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin saneless \
    && mkdir -p /var/lib/saneless \
    && chown 1000:1000 /var/lib/saneless
# The app runs as UID 1000, whose home useradd was told not to create, so the
# state defaults need a base that exists. saneless derives both its data
# directory and its log file from XDG_STATE_HOME, so this one variable puts the
# job database, preserved scans and the log inside the declared volume below --
# including for a one-shot `saneless jobs` run through `docker exec`, which
# would otherwise warn that it cannot write its log. It is the lowest-precedence
# channel: an `[output] data_dir` in a mounted saneless.toml, or set through its
# own environment variable, still wins. `docker run -v ...:/var/lib/saneless`
# is correct without the operator having to know any of this, and the WORKDIR
# below keeps a stray relative write inside the same volume rather than in /.
ENV XDG_STATE_HOME=/var/lib
# Declared so a bare `docker run` with no -v gets an anonymous volume instead
# of writing the job database and preserved scans into the container's
# writable layer, where an image commit or export could carry them off-host.
# A named or anonymous volume inherits the ownership of the image's directory
# at this path on first use, which is what the chown above buys. A bind mount
# gets no such treatment -- hence the documented chown for the operator.
VOLUME ["/var/lib/saneless"]
# Relative writes land in the durable, owned data directory rather than in /.
# `saneless auto-profiles` with no config file loaded writes ./saneless.toml;
# without this it went to /saneless.toml, inside the container's own writable
# layer, ahead of everything in the config search order and gone on the next
# container recreation.
WORKDIR /var/lib/saneless
USER saneless
# 8080 is fixed inside the container: the output.web_port setting is for
# bare-metal installs, and in Docker you remap on the host with -p 8888:8080.
# The healthcheck URL is written out rather than expanded from the environment
# on purpose -- a shell-form expansion would track SANELESS_OUTPUT__WEB_PORT
# but silently not a web_port set in saneless.toml, which replaces one false
# claim with a half-true one. 8080 is above 1024 and a probe of localhost needs
# no privilege, so neither the bind nor the probe cares that this is UID 1000.
EXPOSE 8080
# Kept on /health, never on `doctor`: doctor does network I/O, so it would mark
# the container unhealthy during a routine paperless-ngx restart.
#
# The probe is the standard library, in exec form, so the image carries no HTTP
# client binary and needs no shell. urlopen raises on a refused connection and
# on any non-2xx status, so a 503 from /health exits non-zero and counts as
# unhealthy. The start period keeps a slow first SANE initialisation from
# counting against the retries.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=4)"]
ENTRYPOINT ["saneless"]
CMD ["serve"]
