# Docker

saneless publishes an OCI container image for deployment alongside paperless-ngx.

## Image

| Property | Value |
|----------|-------|
| Image | `ghcr.io/kris-knigga/saneless:latest` |
| Base | `python:3.14-slim` |
| Entrypoint | `saneless serve` |
| Port | `8080` |

## Healthcheck

The image includes a built-in healthcheck:

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1
```

The `/health` endpoint returns 200 when the worker thread is alive, 503 when it is down.

## Volumes

| Mount Point | Purpose | Required |
|-------------|---------|----------|
| `/etc/saneless/config.toml` | Configuration file (mount read-only) | Yes |
| `/tmp/saneless` | Scan temp files and SQLite database | No (ephemeral OK) |
| `/consume` | Consume directory fallback for file-based ingestion | No (only if using fallback) |

## Environment Variables

All `SANELESS_*` environment variables are supported inside the container. Common ones for Docker deployments:

| Variable | Typical Value | Purpose |
|----------|---------------|---------|
| `SANELESS_SCANNER__HOST` | `192.168.1.50` | Network scanner IP address |
| `SANELESS_PAPERLESS__URL` | `http://paperless:8000` | Paperless-ngx URL (Docker network) |
| `SANELESS_PAPERLESS__TOKEN` | `abc123def456` | Paperless-ngx API token |
| `SANELESS_OUTPUT__WEB_PORT` | `8080` | Override web server port |

See [Environment Variables](environment-variables.md) for the full list.

## Minimal docker-compose.yml

```yaml
services:
  saneless:
    image: ghcr.io/kris-knigga/saneless:latest
    ports:
      - "8080:8080"
    volumes:
      - ./config.toml:/etc/saneless/config.toml:ro
```

## USB Scanner Access

For USB-connected scanners managed by a local `saned`, pass the USB bus:

```yaml
services:
  saneless:
    image: ghcr.io/kris-knigga/saneless:latest
    devices:
      - /dev/bus/usb:/dev/bus/usb
    ports:
      - "8080:8080"
    volumes:
      - ./config.toml:/etc/saneless/config.toml:ro
```

This is **not** required for network scanners. Use `SANELESS_SCANNER__HOST` instead.

## Network Scanner Access

For scanners exposed via `saned` on a remote host, set the scanner host:

```yaml
services:
  saneless:
    image: ghcr.io/kris-knigga/saneless:latest
    ports:
      - "8080:8080"
    volumes:
      - ./config.toml:/etc/saneless/config.toml:ro
    environment:
      - SANELESS_SCANNER__HOST=192.168.1.50
```

saneless injects this value into `SANE_NET_HOSTS` before initializing the SANE backend, enabling automatic scanner discovery inside the container.

For detailed setup instructions, see [Scanner Host Discovery](../how-to/scanner-host-discovery.md).

## Full Example

```yaml
services:
  saneless:
    image: ghcr.io/kris-knigga/saneless:latest
    ports:
      - "8080:8080"
    volumes:
      - ./config.toml:/etc/saneless/config.toml:ro
      - saneless-data:/tmp/saneless
    environment:
      - SANELESS_PAPERLESS__URL=http://paperless:8000
      - SANELESS_PAPERLESS__TOKEN=changeme
      - SANELESS_SCANNER__HOST=192.168.1.50
    restart: unless-stopped

volumes:
  saneless-data:
```
