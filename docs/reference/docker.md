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

The `/health` endpoint returns `200` when saneless is healthy. It returns `503` when the worker thread is down (`worker thread is down`) or when the worker is degraded because its job store is failing (`job store failing`); see [`GET /health`](web-api.md#get-health). After three failed checks in a row, Docker reports the container as unhealthy.

Docker's restart policies act only when a container exits, so an unhealthy container keeps running and `restart: unless-stopped` does not restart it. A degraded worker clears itself once the job store accepts writes again; a worker thread that is down needs saneless restarted.

## Volumes

| Mount Point | Purpose | Required |
|-------------|---------|----------|
| `/etc/saneless/config.toml` | Configuration file (mount read-only) | **Yes** |
| `/var/lib/saneless` | **Durable state:** the job database (`saneless.db`) and preserved scans (`failed/`) | **Yes -- do not treat as disposable** |
| `/tmp/saneless` | Scratch space for the scan in progress; every file in it is deleted as the scan finishes | No (ephemeral OK) |
| `/consume` | Consume directory fallback for file-based ingestion | No (only if using fallback) |

`/var/lib/saneless` is not optional, and it is not the same kind of directory
`/tmp/saneless` is. When a scan cannot be delivered to paperless-ngx at all --
the upload fails and no consume directory is configured, paperless-ngx rejects
the upload outright, the consumption task reports a failure, or the task has not
finished when `paperless_task_timeout` expires -- saneless moves the assembled
PDF into `/var/lib/saneless/failed/`. That copy is then the only remaining copy
of the document. Pruning the volume, or leaving it unmounted so that it vanishes
when the container is recreated, destroys scans that were never ingested.

The image sets `SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless` and declares it as
a `VOLUME`, so a plain `docker run -v saneless-data:/var/lib/saneless` is correct
without setting anything else. `/tmp/saneless` is not mounted by the compose
files below: it holds nothing worth keeping between runs.

### Preserved scans in `failed/`

`/var/lib/saneless/failed/` grows over time and **saneless never deletes, moves
or rotates anything in it.** That is deliberate: automatically removing a file
there would destroy the only copy of a scanned document. Draining the directory
is an operator task.

- One PDF is written per unrecoverable delivery -- two for an ADF duplex scan
  whose halves were uploaded separately. Each file corresponds to a job the web
  UI shows as **Failed**, whose error message names that exact path.
- Once the directory holds 20 or more PDFs, saneless logs a WARNING each time it
  preserves another, naming the file count, the total size and the path. Watch
  for it in the container log: individual failures show up as failed jobs, but
  that warning is the only signal that the directory as a whole is filling up.
- To drain it: confirm the documents are in paperless-ngx, or re-ingest the PDFs
  by copying them into the paperless-ngx consume directory, then delete the
  files you have accounted for.
- Re-ingesting is safe. paperless-ngx checksums documents on consumption and
  rejects a duplicate, so dropping a preserved PDF back into the consume
  directory cannot create a second copy of a document it already holds.

Upgrading from a release where the job database lived under `/tmp/saneless`?
See [Upgrading from a pre-`data_dir` release](../how-to/deploy-docker-compose.md#upgrading-from-a-pre-data_dir-release).

## Environment Variables

All `SANELESS_*` environment variables are supported inside the container. Common ones for Docker deployments:

| Variable | Typical Value | Purpose |
|----------|---------------|---------|
| `SANELESS_SCANNER__HOST` | `192.168.1.50` | Network scanner IP address |
| `SANELESS_PAPERLESS__URL` | `http://paperless:8000` | Paperless-ngx URL (Docker network) |
| `SANELESS_PAPERLESS__TOKEN` | `abc123def456` | Paperless-ngx API token |
| `SANELESS_OUTPUT__WEB_PORT` | `8080` | Override web server port |
| `SANELESS_OUTPUT__DATA_DIR` | `/var/lib/saneless` | Durable state directory. **Already set by the image** -- override it only if you mount the volume somewhere else |

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
      - saneless-data:/var/lib/saneless

volumes:
  saneless-data:
```

The data volume is part of the minimum. Without it the job database and any
preserved scans live inside the container's writable layer and are lost the
next time the container is recreated.

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
      - saneless-data:/var/lib/saneless
    environment:
      - SANELESS_PAPERLESS__URL=http://paperless:8000
      - SANELESS_PAPERLESS__TOKEN=changeme
      - SANELESS_SCANNER__HOST=192.168.1.50
    restart: unless-stopped

volumes:
  saneless-data:
```
