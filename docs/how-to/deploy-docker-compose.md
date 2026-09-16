# Deploy with Docker Compose

Run saneless alongside paperless-ngx using Docker Compose -- the most common homelab deployment.

## What you'll need

- Docker and Docker Compose installed on your host
- A running paperless-ngx instance (or you will set one up in this guide)
- A paperless-ngx API token (generate one in paperless-ngx under Settings > API Tokens)
- For network scanners: the scanner's IP address

## Step 1: Create the configuration file

Create a `config` directory next to your `docker-compose.yml`:

```bash
mkdir config
```

Then create `config/config.toml` with your paperless-ngx connection details:

```toml
[paperless]
url = "http://paperless:8000"
token = "your-paperless-api-token"

[profiles.default]
source = "Flatbed"
resolution = 300
mode = "Color"
```

!!! note
    The container reads `/etc/saneless/config.toml` from the mounted `./config` directory. If `config.toml` is missing, saneless uses its defaults plus environment variables and the container still starts; it does not create the file for you. The directory must be writable, because `saneless auto-profiles` and the profile generation at startup rewrite `config.toml` there once it exists. Neither one creates it. Without `config.toml`, the server keeps the profiles it generates in memory only, and `docker compose exec saneless saneless auto-profiles` writes `/saneless.toml` (the image sets no working directory, so `./saneless.toml` is the container root) inside the container instead of anything in `./config`. That file is lost when the container is recreated, and until then saneless loads it ahead of any `config.toml` you add later. So before you run `auto-profiles` in the container, create the file with `touch config/config.toml` (an empty file is a valid config). Keep only `config.toml` in the directory, and do not let untrusted users write to it.

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
      # Uncomment with the saneless side below to enable the fallback:
      # - paperless-consume:/usr/src/paperless/consume
    environment:
      - PAPERLESS_SECRET_KEY=change-me-to-a-long-random-string
      # - PAPERLESS_CONSUMPTION_DIR=/usr/src/paperless/consume
    restart: unless-stopped

  saneless:
    image: ghcr.io/kris-knigga/saneless:latest
    ports:
      - "8080:8080"
    volumes:
      - ./config:/etc/saneless
      - saneless-data:/var/lib/saneless
      # Optional: the consume-directory fallback. When the paperless-ngx API
      # cannot be reached at all, saneless drops the assembled PDF here
      # instead of failing the scan, and paperless-ngx ingests it once it is
      # back. Set paperless.consume_dir in config.toml to /consume as well --
      # the mount on its own does nothing.
      # - paperless-consume:/consume
    environment:
      # Without this the container reports UTC, so every timestamp saneless
      # shows is UTC. Set your own zone.
      - TZ=America/Chicago
      # The paperless URL and token are NOT set here on purpose: anything set
      # here overrides config.toml silently. See below.
      # Uncomment for network scanners:
      # - SANELESS_SCANNER__HOST=192.168.1.50
    restart: unless-stopped

volumes:
  paperless-data:
  paperless-media:
  saneless-data:
  # paperless-consume:
```

Key details:

- **Image:** `ghcr.io/kris-knigga/saneless:latest` includes `libsane` and handles `python-sane` compilation automatically
- **Port 8080:** The saneless web UI
- **One place for the token:** the paperless-ngx URL and token live in `config/config.toml`, and this compose file deliberately sets neither. **An environment variable overrides the config file**, silently: set `SANELESS_PAPERLESS__TOKEN` here and saneless uses that value and ignores the one in `config.toml`. If it is a placeholder, or empty, saneless shows the status strip red and refuses to scan, and the token you carefully put in `config.toml` has nothing to do with it. Leave the block commented and edit the file
- **`TZ`:** a container's clock reports UTC. Without `TZ`, every timestamp saneless displays -- the job history, `saneless jobs`, and the fallback title it gives a document in paperless-ngx -- is UTC rather than your local time. It is a standard container variable, not a saneless setting
- **Consume mount (optional):** `paperless-consume:/consume` shared with paperless-ngx is the [consume-directory fallback](../explanation/consume-directory-fallback.md). Uncomment the volume on both services, the `PAPERLESS_CONSUMPTION_DIR` line, and the entry under `volumes:`, then set `consume_dir = "/consume"` under `[paperless]` in `config.toml`. Mounting without setting `consume_dir` does nothing
- **Config mount:** `./config:/etc/saneless` -- a read-write directory mount. saneless replaces `config.toml` atomically (it writes a temp file in the same directory, then renames it over the original). A single-file bind mount makes that rename fail with EBUSY, and saneless reports "Mount its directory instead"
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

!!! warning "An environment variable overrides `config.toml`"
    Environment variables sit above the config file, so a variable set in your compose file wins over the same setting in `config/config.toml` -- silently, with nothing in the UI to say where the value came from. That is why the example above sets the paperless-ngx connection in the file and not here. Pick one place per setting; for the token, make it `config/config.toml`.

| Setting | Environment Variable |
|---|---|
| Paperless URL | `SANELESS_PAPERLESS__URL` |
| Paperless token | `SANELESS_PAPERLESS__TOKEN` |
| Scanner host | `SANELESS_SCANNER__HOST` |
| Log level | `SANELESS_OUTPUT__LOG_LEVEL` |

See [Environment Variables](../reference/environment-variables.md) for the full list.

## Running behind a reverse proxy

saneless rejects state-changing requests that did not come from a saneless page (see [Cross-site requests](../reference/web-api.md#cross-site-requests)). A reverse proxy in front of saneless has to leave the browser's view of the site intact, or saneless mistakes your own scans for cross-site requests.

**Have the proxy pass the original `Host` header, or set `X-Forwarded-Host`, whatever the scheme.** When the browser sends no `Sec-Fetch-Site`, saneless compares the browser's `Origin` with `Host` and `X-Forwarded-Host`. Browsers never send `Sec-Fetch-Site` over plain HTTP, and browsers without Fetch Metadata support (Safari before 16.4, for example) do not send it over HTTPS either. Behind an HTTPS proxy, current browsers do send it and saneless decides from that header alone, so HTTPS usually works without this step, but an older browser is then rejected. nginx replaces `Host` with the upstream address by default, so tell it to pass the original:

```nginx
location / {
    proxy_pass http://saneless:8080;
    proxy_set_header Host $host;
}
```

nginx's `$host` carries no port. If the proxy listens on a port other than 80, the browser's `Origin` includes that port, so pass the header unchanged with `$http_host` instead.

Other proxies:

- **Caddy** passes the incoming `Host` header through and sets `X-Forwarded-Host` by default, so `reverse_proxy` needs no extra configuration.
- **Traefik** forwards the client's `Host` header by default (`passHostHeader` is `true`). Leave it enabled.

**What a rejection looks like.** The web UI shows "This request was blocked because it did not come from the saneless page. If saneless is behind a reverse proxy, make sure the proxy passes the original Host header." and the request gets a `403`. The saneless log records a warning beginning `Blocked cross-site POST` that names the `Origin`, `Host`, `X-Forwarded-Host` and `Sec-Fetch-Site` values it received: if `Origin` and `Host` disagree there, the proxy is rewriting `Host`.

### Answering only your own hostname

The cross-site check cannot stop a DNS rebinding attack, in which a hostile site makes its own hostname resolve to saneless's address (see [Cross-site requests](../reference/web-api.md#cross-site-requests)). A proxy that answers only saneless's hostname closes that gap, because a rebinding page's requests carry the hostile hostname in `Host`. With nginx, add a default server that drops every other hostname:

```nginx
server {
    listen 80 default_server;
    return 444;
}

server {
    listen 80;
    server_name scanner.home.example;

    location / {
        proxy_pass http://saneless:8080;
        proxy_set_header Host $host;
    }
}
```

A Caddy site block named after a hostname and a Traefik router with a `Host()` rule match only that hostname in the same way. This protects saneless only if browsers cannot reach it directly: do not publish its port on the host (drop `ports:` and put the proxy on the same Docker network), or bind it to an address only the proxy can reach.

## Updating

Pull the latest image and recreate the container:

```bash
docker compose pull saneless
docker compose up -d
```

### Remove your own `SANELESS_PAPERLESS__TOKEN` line

Earlier versions of this guide, and of the `docker-compose.yml` shipped in the
repository, set the paperless-ngx connection in the `environment:` block:

```yaml
    environment:
      # Delete both of these from your own compose file. `changeme` is one of
      # the placeholder literals saneless now detects and refuses to scan with.
      # - SANELESS_PAPERLESS__URL=http://paperless:8000
      # - SANELESS_PAPERLESS__TOKEN=changeme
```

Those lines are in *your* compose file, and upgrading the image does not touch
them. **Delete them.** An environment variable overrides `config.toml`, so
while that line is there:

- The token in `config/config.toml` is ignored, however correct it is.
- If the value is a placeholder -- `changeme` is one of the literals saneless
  now detects -- the status strip stays **red**, the Scan button stays
  disabled, and `saneless scan` exits 2, no matter what you edit into the file.

After deleting the lines, put the connection in `config/config.toml`:

```toml
[paperless]
url = "http://paperless:8000"
token = "your-paperless-api-token"
```

Then `docker compose up -d` to recreate the container, and check the status
strip: the Paperless row turns green once the token is real and reachable.

While you are there, add `TZ` to the `environment:` block if it is not already
set. Without it the container reports UTC and every timestamp saneless shows is
UTC rather than your local time.

### Moving from a single-file config mount

Earlier versions of this guide bind-mounted the host's `./config.toml` by itself,
read-only (`:ro`), onto `/etc/saneless/config.toml`. With that mount, every profile
write fails. A writable single-file bind mount makes the rename fail with EBUSY, and
saneless checks for a read-only mount before it writes anything. Either way it
reports:

```text
Cannot replace /etc/saneless/config.toml: it is bind-mounted as a single file. Mount its directory instead (see docs/how-to/deploy-docker-compose.md).
```

`saneless auto-profiles` exits with status 2. The server logs a warning and uses
the generated profiles for that run only; they do not survive a restart. Switch to
the directory mount:

1. Stop the stack: `docker compose down`
2. Move the file into a directory: `mkdir config && mv config.toml config/`
3. In `docker-compose.yml`, change the volume line to `- ./config:/etc/saneless`
4. Start the stack: `docker compose up -d`

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
