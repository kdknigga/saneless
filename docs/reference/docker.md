# Docker

saneless publishes an OCI container image for deployment alongside paperless-ngx.

## Image

| Property | Value |
|----------|-------|
| Image | `ghcr.io/kdknigga/saneless:latest` |
| Base | `python:3.14-slim` |
| Entrypoint | `saneless serve` |
| Port | `8080` |
| User | `1000:1000` (non-root) |
| Working directory | `/var/lib/saneless` |

## User and file ownership

The container runs as UID/GID **1000**, not root, and everything it writes on a
mounted host directory is owned by 1000. On a single-user Linux host your own
account is 1000, so the `./config` directory you created is already correct and
nothing further is needed.

If `id -u` reports something else, hand the config directory over once:

```bash
chown -R 1000:1000 ./config
```

This applies to bind mounts only. A named or anonymous volume -- what
`/var/lib/saneless` gets -- inherits `1000:1000` from the image the first time
it is used, so durable state needs no fix-up. A bind mount keeps whatever
ownership the host directory already has, and without write access there
saneless cannot rewrite `config.toml` when it saves a generated profile. The
shipped `docker-compose.yml` also carries a commented `user:` line for running
the container as your own UID instead.

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
| `/etc/saneless` | Configuration directory holding `config.toml` (mount read-write; a missing `config.toml` means defaults plus environment variables) | Recommended |
| `/var/lib/saneless` | **Durable state:** the job database (`saneless.db`) and preserved scans (`failed/`) | **Yes -- do not treat as disposable** |
| `/tmp/saneless` | Scratch space for the scan in progress; every file in it is deleted as the scan finishes | No (ephemeral OK) |
| `/consume` | Consume directory fallback for file-based ingestion | No (only if using fallback) |

`/consume` is the path the shipped `docker-compose.yml` uses, as a commented
line you uncomment. Share it with paperless-ngx -- mount the same volume at
paperless-ngx's `PAPERLESS_CONSUMPTION_DIR` -- and **set
`paperless.consume_dir` to the same container path in `config.toml`.** The
mount alone changes nothing: saneless falls back only when `consume_dir` names
a directory. See [Consume directory
fallback](../explanation/consume-directory-fallback.md) for when it activates
and what it costs.

Mount the configuration *directory* (`./config:/etc/saneless`), not `config.toml` itself. saneless rewrites `config.toml` by writing a temp file beside it and renaming it over the original; over a single-file bind mount that rename fails with EBUSY, and over a read-only mount the write is refused. See [Moving from a single-file config mount](../how-to/deploy-docker-compose.md#moving-from-a-single-file-config-mount).

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
- **Partial scans land here too.** A scanner fault part-way through a stack keeps
  the sheets already fed, and a manual duplex job whose second pass or flip
  failed keeps the fronts. Each is a PDF, named so you can tell it from a
  complete one. Budget for them: a jam on a 50-sheet job writes a PDF of
  everything up to the jam, and a rescan of the same stack writes a second,
  complete one.
- **A scan that could not be assembled leaves a directory, not a file.** When PDF
  assembly itself fails, the individual page files are preserved instead, in a
  job-keyed subdirectory holding one PNG per sheet. Those PNGs are uncompressed-
  document-scale: roughly 13 MB per A4 300 DPI colour page, so one such
  directory can be larger than any PDF beside it.
- Once the directory holds 20 or more preserved scans -- counting those
  subdirectories alongside the PDFs -- saneless logs a WARNING each time it
  preserves another, naming the count, the total size and the path. Watch
  for it in the container log: individual failures show up as failed jobs, but
  that warning is the only signal that the directory as a whole is filling up.
- To drain it: confirm the documents are in paperless-ngx, or re-ingest the PDFs
  by copying them into the paperless-ngx consume directory, then delete the
  files you have accounted for. A preserved page directory has no PDF to
  re-ingest -- assemble or rescan it, then remove the directory. The PNG names
  are the order the sheets were acquired, one pass at a time, which is document
  order for a simplex scan but not for a manual duplex one; see
  [what `failed/` holds](../explanation/consume-directory-fallback.md#when-it-activates)
  before assembling a duplex job by hand.
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
| `SANELESS_PAPERLESS__TOKEN` | `abc123def456` | Paperless-ngx API token. Prefer `config.toml` -- see below |
| `SANELESS_OUTPUT__WEB_PORT` | `8080` | **The container's port is fixed at 8080.** `web_port` is a bare-metal setting: setting it here moves the server off the port the image exposes and the healthcheck probes, so the container reports unhealthy while the UI is in fact running somewhere else. Remap on the host instead -- `-p 8888:8080` |
| `SANELESS_OUTPUT__DATA_DIR` | `/var/lib/saneless` | Durable state directory. **Already set by the image** -- override it only if you mount the volume somewhere else |
| `TZ` | `America/Chicago` | Standard container variable, **not** a saneless setting. A container's clock reports UTC without it, and saneless renders every timestamp in the server's local zone, so `TZ` is what makes the job history, `saneless jobs` and the fallback document title show your local time |

See [Environment Variables](environment-variables.md) for the full list.

### Placeholder tokens are detected

saneless keeps a small fixed list of literal token values that mean "nobody
configured this" -- `changeme`, `your-api-token-here` and their family -- and
treats an empty or whitespace-only token the same way. When the configured
token is one of them, the server still starts, so you can see why, but the
status strip shows Paperless red, the web UI's Scan button is disabled with the
reason, and `saneless scan` exits 2 before the scanner is opened. The check is
exact, never a substring match: `changeme7f3a91` is a real token.

So do not copy a placeholder into a deployment expecting to fix it later --
nothing will scan until it is replaced.

**An environment variable overrides `config.toml`.** Setting
`SANELESS_PAPERLESS__TOKEN` in a compose file wins over the token in the
mounted config file, silently. Keep the secret in `config/config.toml` alone,
and leave the compose `environment:` block free of it -- which is what the
shipped template now does.

## Minimal docker-compose.yml

```yaml
services:
  saneless:
    image: ghcr.io/kdknigga/saneless:latest
    ports:
      - "8080:8080"
    volumes:
      - ./config:/etc/saneless
      - saneless-data:/var/lib/saneless

volumes:
  saneless-data:
```

The data volume is part of the minimum. Without it the job database and any
preserved scans live inside the container's writable layer and are lost the
next time the container is recreated.

## Scanner Access

The container never reaches a scanner directly, not even one plugged into its own
host. It reaches every scanner over the SANE network protocol, which is why no
device mapping and no `--privileged` flag appear anywhere on this page: `saned`
owns the scanner, and saneless talks to `saned`. Set the scanner host to the
machine `saned` runs on -- the container's own host, or another one:

```yaml
services:
  saneless:
    image: ghcr.io/kdknigga/saneless:latest
    ports:
      - "8080:8080"
    volumes:
      - ./config:/etc/saneless
    environment:
      - SANELESS_SCANNER__HOST=192.168.1.50
```

saneless injects this value into `SANE_NET_HOSTS` before initializing the SANE backend, enabling automatic scanner discovery inside the container.

For detailed setup instructions, see [Scanner Host Discovery](../how-to/scanner-host-discovery.md).

## Full Example

```yaml
services:
  saneless:
    image: ghcr.io/kdknigga/saneless:latest
    ports:
      - "8080:8080"
    volumes:
      - ./config:/etc/saneless
      - saneless-data:/var/lib/saneless
      # Optional consume-directory fallback; also set
      # paperless.consume_dir = "/consume" in config.toml.
      # - paperless-consume:/consume
    environment:
      - TZ=America/Chicago
      - SANELESS_SCANNER__HOST=192.168.1.50
      # The paperless-ngx URL and token belong in config/config.toml. Setting
      # them here overrides that file silently.
    restart: unless-stopped

volumes:
  saneless-data:
```

`config/config.toml` alongside it carries the connection:

```toml
[paperless]
url = "http://paperless:8000"
token = "abc123def456"
```
