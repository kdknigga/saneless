# Stage 1: Build
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY . .
RUN uv build --wheel --out-dir /dist

# Stage 2: Runtime
FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsane1 curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /dist/*.whl /tmp/
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libsane-dev \
    && pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl \
    && apt-get purge -y gcc libc6-dev libsane-dev && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1
ENTRYPOINT ["saneless"]
CMD ["serve"]
