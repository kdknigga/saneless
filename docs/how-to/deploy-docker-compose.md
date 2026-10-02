# Deploy with Docker Compose

Run saneless alongside paperless-ngx using Docker Compose -- the most common homelab deployment.

## What you'll need

- Docker and Docker Compose installed on your host
- A running paperless-ngx instance, reachable from this host: paperless-ngx 2.16 or later, which speaks API version 9 or 10. An older paperless-ngx refuses every upload with `406`, and the status strip reports `incompatible_version`. This guide covers the saneless side only; stand paperless-ngx up from [its own compose files](https://docs.paperless-ngx.com/setup/) first
- A paperless-ngx API token (generate one in paperless-ngx under Settings > API Tokens)
- For network scanners: the scanner's IP address

## Step 1: Create the configuration file

Create a `config` directory next to your `docker-compose.yml`:

```bash
mkdir config
```

Then create `config/saneless.toml` with your paperless-ngx connection details:

```toml
[paperless]
url = "http://paperless:8000"
token = "your-api-token-here"
```

Leave the scan profiles out. When the file holds none, saneless [generates profiles at startup](configure-scan-profiles.md#generation-at-server-startup) from what the scanner reports and writes them into this file; `docker compose exec saneless saneless auto-profiles` does the same on demand once the container is running. A hand-written `[profiles.default]` turns off that generation at startup.

!!! note
    The container reads `/etc/saneless/saneless.toml` from the mounted `./config` directory. If `saneless.toml` is missing, saneless uses its defaults plus environment variables and the container still starts. The directory must be writable by the container, because `saneless auto-profiles` and the profile generation at startup rewrite `saneless.toml` there. With no `saneless.toml`, `docker compose exec saneless saneless auto-profiles` creates `/etc/saneless/saneless.toml` in the mounted `./config` directory, because that directory exists and the container can write to it; you do not need to create the file first. The new file has mode `0600`, and when the command runs as root it is given the directory's owner. The profile generation at startup never creates the file: without `saneless.toml`, the server keeps the profiles it generates in memory only. If the container cannot write to `./config`, `auto-profiles` does not fall back to the data volume. It tries the per-user file under the container user's home instead, which the image does not provide, and exits 2 naming it and saying why `/etc/saneless` was passed over: `mounted read-only` means the volume line ends in `:ro`, so remove it; `not writable by this user` means fix the ownership as the next note describes. Then run it again. Keep only `saneless.toml` in the directory, and do not let untrusted users write to it.

!!! note "If your user ID is not 1000"
    The container runs as UID/GID **1000**, not root, so it writes to `./config` as 1000 no matter who owns the directory on the host. On a single-user Linux machine your own account is already 1000 and the directory you just created belongs to it, so there is nothing to do. If `id -u` reports anything else, hand the directory over once:

    ```bash
    chown -R 1000:1000 ./config
    ```

    A bind mount keeps the host's ownership -- unlike the named data volume, which inherits 1000 from the image -- so without this the container cannot rewrite `saneless.toml`, and saving a generated profile fails. The alternative is the commented `user:` line in the compose file below, which runs the container as your UID instead.

## Step 2: Create the Docker Compose file

This file defines saneless and nothing else. paperless-ngx is stood up from its own compose file:

!!! note "Where paperless-ngx comes from"
    paperless-ngx needs more than one container -- a database and a Redis-compatible message broker alongside the web service -- and it publishes [compose files](https://github.com/paperless-ngx/paperless-ngx/tree/main/docker/compose) that wire all of it together. Download the one for your database from [the paperless-ngx setup guide](https://docs.paperless-ngx.com/setup/) and run it in its own directory. This guide does not reproduce it: a copy here would go stale the first time their requirements changed, and a partial copy produces a stack that never finishes starting.

```yaml
services:
  saneless:
    image: ghcr.io/kdknigga/saneless:0.2.0-rc.6
    ports:
      - "8080:8080"
    # The image already runs as UID/GID 1000, so on a single-user Linux host --
    # where ./config is 1000:1000 because you created it -- this needs no
    # action and stays commented. Uncomment and edit it only if your own UID
    # differs, or run `chown -R 1000:1000 ./config` once instead. A literal,
    # not "${UID}:${GID}": compose does not populate UID unless your shell
    # exports it, and the silent fallback is a container that will not start.
    # user: "1000:1000"
    volumes:
      - ./config:/etc/saneless
      - saneless-data:/var/lib/saneless
      # Optional: the consume-directory fallback. When the paperless-ngx API
      # cannot be reached at all, saneless drops the assembled PDF here
      # instead of failing the scan, and paperless-ngx ingests it once it is
      # back. Set paperless.consume_dir in saneless.toml to /consume as well --
      # the mount on its own does nothing.
      # - paperless-consume:/consume
    # Uncomment to put saneless on the network the paperless-ngx stack
    # created, so that its service name resolves from here. See below.
    # networks:
    #   - paperless
    environment:
      # Without this the container reports UTC, so every timestamp saneless
      # shows is UTC. Set your own zone.
      - TZ=America/Chicago
      # The paperless URL and token are NOT set here on purpose: anything set
      # here overrides saneless.toml silently. See below.
      # Uncomment for network scanners:
      # - SANELESS_SCANNER__HOST=192.168.1.50
    restart: unless-stopped
    # Time for a stopped scan's pages to be copied into failed/ before Docker
    # kills saneless. See "Stopping and restarting" below.
    stop_grace_period: 90s

volumes:
  saneless-data:
  # The consume directory is shared with the paperless-ngx stack, which is a
  # separate compose project. Create the volume once, outside both, with
  # `docker volume create paperless-consume`, then declare it external here
  # and mount it on the paperless-ngx side too.
  # paperless-consume:
  #   external: true

# Uncomment together with the networks: block on the service above. Find the
# real name with `docker network ls` -- compose names it after the directory
# the paperless-ngx compose file runs in, usually <directory>_default.
# networks:
#   paperless:
#     external: true
#     name: paperless-ngx_default
```

Key details:

- **Image:** `ghcr.io/kdknigga/saneless:0.2.0-rc.6` ships with `libsane` and a prebuilt `python-sane`; nothing compiles at run time
- **Port 8080:** The saneless web UI
- **One place for the token:** the paperless-ngx URL and token live in `config/saneless.toml`, and this compose file deliberately sets neither. **An environment variable overrides the config file**, silently: set `SANELESS_PAPERLESS__TOKEN` here and saneless uses that value and ignores the one in `saneless.toml`. If it is a placeholder, or empty, saneless shows the status strip red and refuses to scan, and the token you carefully put in `saneless.toml` has nothing to do with it. Leave the block commented and edit the file
- **`TZ`:** a container's clock reports UTC. Without `TZ`, every timestamp saneless displays -- the job history, `saneless jobs`, and the fallback title it gives a document in paperless-ngx -- is UTC rather than your local time. It is a standard container variable, not a saneless setting
- **Consume mount (optional):** a directory both stacks can see is the [consume-directory fallback](../explanation/consume-directory-fallback.md). Create the shared volume with `docker volume create paperless-consume`, uncomment the mount and the external `volumes:` entry here, mount the same volume at paperless-ngx's consumption directory on its side, and set `consume_dir = "/consume"` under `[paperless]` in `saneless.toml`. Mounting without setting `consume_dir` does nothing
- **Config mount:** `./config:/etc/saneless` -- a read-write directory mount. saneless replaces `saneless.toml` atomically (it writes a temp file in the same directory, then renames it over the original). A single-file bind mount makes that rename fail with EBUSY; see [Moving from a single-file config mount](#moving-from-a-single-file-config-mount)
- **Data volume:** `saneless-data:/var/lib/saneless` is required, not optional. It holds the job database and the `failed/` directory, where saneless preserves any scan it could not deliver to paperless-ngx. The image sets `XDG_STATE_HOME=/var/lib`, so its defaults put the job database, `failed/` and the log of one-shot commands such as `saneless jobs` under `/var/lib/saneless`, and mounting the volume there is all that is needed. See [Docker volumes](../reference/docker.md#volumes) for what accumulates in `failed/` and how to drain it
- **Reaching paperless-ngx:** the two stacks are separate compose projects, so each gets its own network and the name `paperless` does not resolve from here on its own. Either join the paperless-ngx network -- uncomment both `networks:` blocks above, after checking the real name with `docker network ls` -- and keep `url = "http://paperless:8000"`, or leave the networks alone and point `url` at the host the paperless-ngx stack publishes on, such as `http://192.168.1.10:8000`. Whichever you pick, the URL has to resolve from *inside* the saneless container: `localhost` there is the container itself, not your host
- **Network scanners:** Set `SANELESS_SCANNER__HOST=192.168.1.50` to discover scanners on a remote host. See [Scanner Host Discovery](scanner-host-discovery.md) for details

### Moving from a single-file config mount

saneless writes `saneless.toml` by renaming a new file over it, so it needs the
whole `/etc/saneless` directory mounted. If you mount `saneless.toml` by itself
onto `/etc/saneless/saneless.toml`, every profile write fails: a writable
single-file mount makes the rename fail with EBUSY, and a read-only one is
refused before anything is written. Either way saneless reports:

```text
Cannot replace /etc/saneless/saneless.toml: it is bind-mounted as a single file. Mount its directory instead (see https://kdknigga.github.io/saneless/how-to/deploy-docker-compose/#moving-from-a-single-file-config-mount).
```

`saneless auto-profiles` exits with status 2. The server logs a warning and uses
the generated profiles for that run only, so they are gone after a restart.
Mount the directory instead:

1. Stop the stack: `docker compose down`
2. Move the file into a directory: `mkdir config && mv saneless.toml config/saneless.toml`
3. In `docker-compose.yml`, change the volume line to `- ./config:/etc/saneless`
4. Start the stack: `docker compose up -d`

## Step 3: Start saneless

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
{"status":"ok"}
```

Then open the web UI at `http://localhost:8080` in your browser. You should see the scan interface with your configured profiles.

## Environment variable configuration

`SANELESS_SCANNER__HOST` belongs in the `environment:` block, as in the compose file above. Keep the paperless-ngx URL and token in `./config/saneless.toml` instead, because a variable that is set silently overrides the file. [Where saneless reads settings](../reference/configuration.md#where-saneless-reads-settings) gives the order, and [Environment Variables](../reference/environment-variables.md) lists every variable.

## Running behind a reverse proxy

saneless answers only requests whose `Host` names it (see [Host check](../reference/web-api.md#host-check)), and it rejects state-changing requests that did not come from a saneless page (see [Cross-site requests](../reference/web-api.md#cross-site-requests)). A reverse proxy in front of saneless has to leave the browser's view of the site intact, or saneless mistakes your own scans for cross-site requests.

**Add the proxy's public name to `[web] allowed_hosts`.** A proxy that passes the original `Host` header, as described next, sends saneless the name the browser used, such as `scan.example.com`. saneless answers to IP addresses, names without a dot, and names under `.local`, `.home.arpa`, `.internal` and `.lan` without configuration; any other name gets `421` with "saneless does not answer to this address." until you list it:

```toml
[web]
allowed_hosts = ["scan.example.com"]
```

or `SANELESS_WEB__ALLOWED_HOSTS='["scan.example.com"]'` in the environment. A leading-dot entry such as `.example.com` covers that domain and every name under it, so use one only for a domain you control. For a dynamic DNS name, write your own full name, such as `me.duckdns.org`. Never write the shared suffix `.duckdns.org` or a registry suffix such as `.co.uk`: anybody can register a name under those and use it to reach saneless through DNS rebinding. See [Allowed host names](../reference/configuration.md#allowed-host-names).

**Have the proxy pass the original `Host` header, whatever the scheme.** This is required, and setting `X-Forwarded-Host` instead is not a substitute. saneless never trusts `X-Forwarded-Host`, so a proxy that replaces `Host` with its upstream's name, such as `saneless:8080`, sends a name saneless always answers to. That turns the Host check off for every request through the proxy, DNS rebinding included, and no request is ever refused to tell you so. saneless logs a warning, at most once an hour, when it sees a `Host` beside an `X-Forwarded-Host` that names another host. When the browser sends no `Sec-Fetch-Site`, saneless compares the browser's `Origin` with `Host` and `X-Forwarded-Host`. Browsers never send `Sec-Fetch-Site` over plain HTTP, and browsers without Fetch Metadata support (Safari before 16.4, for example) do not send it over HTTPS either. Behind an HTTPS proxy, current browsers do send it and saneless decides from that header alone, so HTTPS usually works without this step, but an older browser is then rejected. nginx replaces `Host` with the upstream address by default, so tell it to pass the original:

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

saneless itself refuses a DNS rebinding attack, in which a hostile site makes its own hostname resolve to saneless's address: the rebinding page's requests carry the hostile hostname in `Host`, and saneless answers them with `421` (see [Host check](../reference/web-api.md#host-check)). A proxy that answers only saneless's hostname adds a second layer in front of that check, as defence in depth. Its hostname still has to be in `[web] allowed_hosts`, as described above. With nginx, add a default server that drops every other hostname:

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

A Caddy site block named after a hostname and a Traefik router with a `Host()` rule match only that hostname in the same way. This layer protects saneless only if browsers cannot reach it directly: do not publish its port on the host (drop `ports:` and put the proxy on the same Docker network), or bind it to an address only the proxy can reach.

## Stopping and restarting

`docker compose stop`, `docker compose down`, `docker compose restart` and
`docker compose up -d` after an upgrade all stop saneless the same way: Docker
sends SIGTERM, waits for the container's grace period, and then kills it.

What a stop keeps depends on where the scan is. A manual duplex job waiting
at the flip prompt is interrupted at once and keeps its front sides as a
`(fronts)` PDF in `failed/`, and the job records that the server restarted and
names the file. One still in pass A does the same when pass A finishes, if
that is within 5 seconds of the stop. saneless waits up to 5 seconds for the
scan worker to finish, and up to 60 seconds more only while it is writing
pages into `failed/`.

A scan busy with anything else when those 5 seconds run out -- pass A still
feeding, pass B, a single-pass scan, assembly or the upload -- is not
interrupted. saneless exits under it, and its pages stay in the container's
`/tmp`. The next start of the same container, after `docker compose restart`
or `docker compose stop` and `start`, recovers them into `failed/`, but
`docker compose up -d` recreates the container and they are lost. Let a scan
finish before you stop or upgrade saneless.

That copy can be slow. saneless spools pages under `/tmp` inside the
container, and `failed/` is on the data volume: they are different
filesystems, so keeping the pages means copying every one of them. Docker's
default grace period is 10 seconds, which a large scan can outlast, and a copy
that is killed half way is lost for good. `docker compose up -d` recreates the
container and discards its `/tmp`, so the next start has nothing left to
recover.

The compose file above sets `stop_grace_period: 90s` for this reason. Keep it,
or something at least as long, in your own compose file. If you run the image
with `docker run` instead, pass the same budget as `--stop-timeout 90`.

## Updating

The image in `docker-compose.yml` is pinned to a tag, so pulling again only
fetches a newer image when that tag has moved. To move to another release,
change the tag on the `image:` line to the release you want -- see
[Image tags](../reference/docker.md#image-tags) for which tags exist and which
of them move -- then pull it and recreate the container:

```bash
docker compose pull saneless
docker compose up -d
```
