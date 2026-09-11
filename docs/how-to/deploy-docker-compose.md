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
      - saneless-data:/var/lib/saneless
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
- **Data volume:** `saneless-data:/var/lib/saneless` is required, not optional. It holds the job database and the `failed/` directory, where saneless preserves any scan it could not deliver to paperless-ngx. The image already sets `SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless`, so mounting the volume there is all that is needed. See [Docker volumes](../reference/docker.md#volumes) for what accumulates in `failed/` and how to drain it
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

### Upgrading from a pre-`data_dir` release

Earlier releases kept the job database inside the scan scratch directory, and the
compose file mounted the `saneless-data` volume at `/tmp/saneless`. Durable state now
has its own setting, `output.data_dir`, which the image points at `/var/lib/saneless`,
and the compose file above mounts the same named volume there instead.

**saneless does not migrate anything.** It never moves, copies or reads a database
at the old location; it simply opens `<data_dir>/saneless.db`. What that means for
you depends on how you deployed.

**Compose deployments keep their history, as long as you reuse the volume.** The
old mount put `saneless.db` at the root of the `saneless-data` volume, and the new
mount point is the root of that same volume. Point the `saneless-data` volume at
`/var/lib/saneless` instead of the old path, leave the volume itself alone, and the
existing database is found in place. Recreating or deleting the volume is what
loses the history.

**Bare-metal installs start with empty history.** The old database was at
`<tmp_dir>/saneless.db`, typically `/tmp/saneless/saneless.db`; the new default is
`~/.local/state/saneless/saneless.db`. Nothing copies it across. If you want your
history, stop saneless and move the file yourself:

```bash
mkdir -p ~/.local/state/saneless
cp -a /tmp/saneless/saneless.db* ~/.local/state/saneless/
```

!!! warning "Copy the `-wal` and `-shm` sidecars too"
    saneless runs SQLite in WAL mode, so the database is up to three files:
    `saneless.db`, `saneless.db-wal` and `saneless.db-shm`. After an unclean
    shutdown the `-wal` file holds committed transactions that are not yet in the
    main file, and copying only `saneless.db` silently loses them. Copy all three
    -- the `saneless.db*` glob above does -- and only while saneless is stopped.

If you would rather start clean, delete the old files and let saneless create a new
database. Only job history is at stake either way: the scanned documents are in
paperless-ngx, and profiles and settings come from `config.toml`. Preserved scans
are not a concern for this upgrade, because the `failed/` directory is new in this
release and there is nothing of that kind at the old path.

### What else changed in this release

- Scans that cannot be delivered to paperless-ngx are preserved as PDFs under
  `<data_dir>/failed/` instead of being deleted, and the job's error message names
  the file. See [Docker volumes](../reference/docker.md#volumes) for how the
  directory grows and how to drain it.
- A scan that fell back to the consume directory now ends in a new `FALLBACK`
  state, shown as **Saved to folder** in amber rather than being reported as a
  plain success. `saneless jobs` prints humanised labels (`Complete`, `Failed`,
  `Saved to folder`) where it used to print the raw enum value;
  `saneless jobs --json` still reports the raw `state` and now also carries
  `outcome` and `warning`.
- `GET /api/paperless/test` gained two outcomes: a 404 from the paperless-ngx API
  is now `not_found` and a 5xx is `server_error`, where both were previously
  reported as `connected`. The three original values are unchanged.
