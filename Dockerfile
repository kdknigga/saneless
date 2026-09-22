# Digests re-resolved 2026-09-18 against the Docker Hub and GHCR v2 APIs.
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
# `requires = ["uv_build>=0.10.3,<0.11.0"]`, so a Dependabot bump across that
# ceiling needs the constraint widened in the same pull request -- which is
# the coupling surfacing where it can be reviewed, rather than breaking later.
FROM ghcr.io/astral-sh/uv:0.10.3@sha256:7a88d4c4e6f44200575000638453a5a381db0ae31ad5c3a51b14f8687c9d93a3 AS uv

# Stage 1: Build
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
# Named inputs, never the whole context. This is the second of TWO independent
# gates on what can reach a build layer; the first is the .dockerignore
# allow-list. Copying the whole context here would sweep in whatever the daemon
# was sent -- which, before this was written, included a real config.toml
# holding a live paperless-ngx API token, recoverable afterwards from the
# discarded builder stage. With both gates in place, a mistake in either one
# alone leaks nothing.
#
# README.md and LICENSE are inputs, not documentation: pyproject.toml's
# `readme` and PEP 639 `license-files` keys make `uv build --wheel` fail
# outright if either file is absent.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv build --wheel --out-dir /dist
RUN uv export --locked --no-dev --no-emit-project -o /dist/requirements.txt

# Stage 2: Runtime
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsane1 curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /dist/*.whl /tmp/
COPY --from=builder /dist/requirements.txt /tmp/requirements.txt
# The dependency set installed below comes from uv.lock, not from the project
# wheel's own metadata, and every byte of it is checked against a sha256 the
# lock recorded. Two invocations, in this order, never merged into one: the
# exported requirements file first -- each of its entries carrying its digest --
# and then the locally built wheel alone, with --no-deps.
#
# Installing the wheel on its own, which is what this replaced, resolved the
# dependency tree from the project's `>=` floors at build time and never read
# uv.lock. A lock refreshed to escape an advisory therefore constrained CI and
# constrained nothing that shipped, and the published image could carry the very
# tree this project had already upgraded away from. The hash check refuses
# anything not listed with a matching digest -- a substituted or republished
# artifact fails the build instead of shipping -- and --no-deps stops those
# floors re-entering through the wheel. The wheel is deliberately absent from
# the hashed file: it is built in the preceding stage of this same build, so
# hashing it would only compare it against itself.
#
# gcc and the headers are installed, used and purged inside this single RUN
# because python-sane publishes an sdist and no wheel, so it compiles here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libsane-dev \
    && pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && pip install --no-cache-dir --no-deps /tmp/*.whl \
    && rm /tmp/*.whl /tmp/requirements.txt \
    && apt-get purge -y gcc libc6-dev libsane-dev && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
# The application account. Everything above this line needs root -- apt and the
# system-wide pip install -- and everything below it does not.
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
# The app runs as UID 1000, so an unset data_dir would resolve under that
# account's home -- which useradd was told not to create. Baking the path into
# the image makes `docker run -v ...:/var/lib/saneless` correct without the
# operator having to know this variable exists, and the WORKDIR below keeps a
# stray relative write inside the same declared volume rather than in /.
ENV SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless
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
# but silently not a web_port set in config.toml, which replaces one false
# claim with a half-true one. 8080 is above 1024 and curl to localhost needs no
# privilege, so neither the bind nor the probe cares that this is UID 1000.
EXPOSE 8080
# Kept on /health, never on `doctor`: doctor does network I/O, so it would mark
# the container unhealthy during a routine paperless-ngx restart.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1
ENTRYPOINT ["saneless"]
CMD ["serve"]
