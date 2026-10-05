# Every FROM reference is `image:tag@sha256:...`, and both halves are needed:
# the digest is the immutable pin, and the tag tells Dependabot which stream to
# bump it along. A bare digest never gets updated.

# Stage 0: the uv binary.
#
# A FROM stage, not a COPY --from naming the registry image: Dependabot only
# updates digests on FROM lines, so a pin on a COPY would never move.
#
# Pinned to the uv series pyproject.toml builds with,
# `requires = ["uv_build>=0.12.18,<0.13.0"]`; a bump across that ceiling needs
# the constraint widened in the same pull request.
FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv

# Stage 1: Build.
#
# Every compiler, header and installer stays in this stage; the runtime stage
# receives only the finished /opt/venv.
#
# `--locked` refuses a stale uv.lock and checks every download against its
# recorded sha256, including the setuptools that compiles python-sane (an
# sdist): it is the lock's `build` group, and pyproject.toml builds python-sane
# against it with build isolation off.
#
# The first sync installs dependencies and the build group; the second adds the
# project and drops the build group. A source change reruns only the second.
# `--no-editable` makes the venv self-contained once copied.
FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2 AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libsane-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
# UV_PYTHON_DOWNLOADS and UV_PYTHON hold uv to this image's interpreter, which
# the runtime stage shares; a uv-managed CPython would live outside /opt/venv
# and leave its python symlink dangling. UV_COMPILE_BYTECODE matters because
# /opt/venv is read-only to UID 1000.
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_PYTHON=/usr/local/bin/python3.14 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1
WORKDIR /app
# Named inputs, never the whole context. This and the .dockerignore allow-list
# are two independent gates, so a mistake in one alone leaks nothing from a
# config file sitting in the build context.
#
# README.md and LICENSE are build inputs: pyproject.toml's `readme` and
# `license-files` keys fail the project build without them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-default-groups --group build --no-install-project --no-editable
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-default-groups --no-editable

# Stage 2: Runtime.
#
# The same base digest as the builder, so the copied venv's interpreter path
# resolves.
#
# - libsane1 is the one package the compiled python-sane links against.
# - dll.conf enables only `net` (a saned) and `escl` (a network scanner). The
#   stock list probes about eighty backends on first enumeration, 8.5 seconds
#   with no scanner attached. dll.d/ is emptied so no drop-in adds any back.
# - pip and ensurepip's wheels are removed, so a `docker exec` finds no
#   installer.
FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2
RUN apt-get update && apt-get install -y --no-install-recommends libsane1 \
    && rm -rf /var/lib/apt/lists/* \
    && printf 'net\nescl\n' > /etc/sane.d/dll.conf \
    && rm -rf /etc/sane.d/dll.d/* \
    && PIP_ROOT_USER_ACTION=ignore python -m pip uninstall -y pip \
    && rm -f /usr/local/lib/python3.14/ensurepip/_bundled/*.whl
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# UID and GID 1000 match the operator's own ./config on a single-user Linux
# host, so the read-write config mount needs no chown. Other operators get a
# documented chown and a compose `user:` line; see
# docs/how-to/deploy-docker-compose.md.
#
# This RUN must stay BEFORE the VOLUME below: the legacy builder discards
# changes made inside a declared volume afterwards, so the wrong order leaves a
# fresh volume owned by root. BuildKit and buildah keep them, so a BuildKit or
# buildah build cannot catch it.
#
# The chmod makes the directory, and so a fresh volume, private to UID 1000.
RUN groupadd --gid 1000 saneless \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin saneless \
    && mkdir -p /var/lib/saneless \
    && chown 1000:1000 /var/lib/saneless \
    && chmod 700 /var/lib/saneless
# UID 1000 has no home, so the state defaults need a base that exists. saneless
# derives its data directory and its log file from XDG_STATE_HOME, so this puts
# both inside the volume, `docker exec` runs included. It is the
# lowest-precedence channel; an `[output] data_dir` setting still wins.
ENV XDG_STATE_HOME=/var/lib
# A bare `docker run` with no -v gets an anonymous volume instead of writing
# the job database and scans into the container's writable layer. Named and
# anonymous volumes inherit the ownership above; a bind mount does not.
VOLUME ["/var/lib/saneless"]
# Relative writes land in the volume, not the writable layer. A saneless.toml
# here would be read ahead of /etc/saneless, which is where
# `saneless auto-profiles` writes.
WORKDIR /var/lib/saneless
USER saneless
# 8080 is fixed inside the container; remap on the host with -p 8888:8080. The
# healthcheck URL is literal because expanding SANELESS_OUTPUT__WEB_PORT would
# miss a web_port set in saneless.toml.
EXPOSE 8080
# /health, never `doctor`: doctor does network I/O and would mark the container
# unhealthy during a paperless-ngx restart. urlopen raises on a refused
# connection and on any non-2xx status, so a 503 counts as unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=4)"]
ENTRYPOINT ["saneless"]
CMD ["serve"]
