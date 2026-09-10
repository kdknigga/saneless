# Deploy with Docker Compose

Run saneless alongside paperless-ngx using Docker Compose -- the most common homelab deployment.

## What you'll need

- Docker and Docker Compose installed on your host
- A running paperless-ngx instance (or you will set one up in this guide)
- A paperless-ngx API token (generate one in paperless-ngx under Settings > API Tokens)
- For network scanners: the scanner's IP address

## Step 1: Create the configuration file

Create a `config.toml` on the host with your paperless-ngx connection details:

```toml
[paperless]
url = "http://paperless:8000"
token = "your-paperless-api-token"

[profiles.default]
source = "Flatbed"
resolution = 300
mode = "Color"
```

!!! warning
    `config.toml` must exist on the host before starting the container. The saneless container reads it at `/etc/saneless/config.toml` (mounted read-only) and will fail to start if the file is missing.

## Step 2: Create the Docker Compose file

Create a `docker-compose.yml` with both saneless and paperless-ngx on the same Docker network so that `http://paperless:8000` resolves between containers:

```yaml
services:
  paperless:
    image: ghcr.io/paperless-ngx/paperless-ngx:latest
    ports:
      - "8000:8000"
    volumes:
      - paperless-data:/usr/src/paperless/data
      - paperless-media:/usr/src/paperless/media
    environment:
      - PAPERLESS_SECRET_KEY=change-me-to-a-long-random-string
    restart: unless-stopped

  saneless:
    image: ghcr.io/kris-knigga/saneless:latest
    ports:
      - "8080:8080"
    volumes:
      - ./config.toml:/etc/saneless/config.toml:ro
      - saneless-data:/tmp/saneless
    environment:
      - SANELESS_PAPERLESS__URL=http://paperless:8000
      - SANELESS_PAPERLESS__TOKEN=your-paperless-api-token
      # Uncomment for network scanners:
      # - SANELESS_SCANNER__HOST=192.168.1.50
    restart: unless-stopped

volumes:
  paperless-data:
  paperless-media:
  saneless-data:
```

Key details:

- **Image:** `ghcr.io/kris-knigga/saneless:latest` includes `libsane` and handles `python-sane` compilation automatically
- **Port 8080:** The saneless web UI
- **Config mount:** `./config.toml:/etc/saneless/config.toml:ro` -- read-only bind mount
- **Shared network:** Both services are on the default Docker Compose network, so `http://paperless:8000` resolves automatically
- **Network scanners:** Set `SANELESS_SCANNER__HOST=192.168.1.50` to discover scanners on a remote host. See [Scanner Host Discovery](scanner-host-discovery.md) for details

## Step 3: Start the services

```bash
docker compose up -d
```

## Step 4: Verify the deployment

Check that the saneless health endpoint responds:

```bash
curl http://localhost:8080/health
```

Expected response:

```json
{"status": "ok"}
```

Then open the web UI at `http://localhost:8080` in your browser. You should see the scan interface with your configured profiles.

## Environment variable configuration

You can configure saneless entirely through environment variables using the `SANELESS_` prefix with `__` as the nested delimiter. This is useful when you prefer not to mount a config file:

| Setting | Environment Variable |
|---|---|
| Paperless URL | `SANELESS_PAPERLESS__URL` |
| Paperless token | `SANELESS_PAPERLESS__TOKEN` |
| Scanner host | `SANELESS_SCANNER__HOST` |
| Log level | `SANELESS_OUTPUT__LOG_LEVEL` |

See [Environment Variables](../reference/environment-variables.md) for the full list.

## Updating

Pull the latest image and recreate the container:

```bash
docker compose pull saneless
docker compose up -d
```
