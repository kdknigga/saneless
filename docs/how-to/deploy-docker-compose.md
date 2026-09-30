# Deploy with Docker Compose

Run saneless alongside paperless-ngx using Docker Compose -- the most common homelab deployment.

## What you'll need

- Docker and Docker Compose installed on your host
- A running paperless-ngx instance, reachable from this host: paperless-ngx 2.16 or later, which speaks API version 9 or 10. This guide covers the saneless side only; stand paperless-ngx up from [its own compose files](https://docs.paperless-ngx.com/setup/) first
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

[profiles.default]
source = "Flatbed"
resolution = 300
mode = "Color"
```

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
- **Config mount:** `./config:/etc/saneless` -- a read-write directory mount. saneless replaces `saneless.toml` atomically (it writes a temp file in the same directory, then renames it over the original). A single-file bind mount makes that rename fail with EBUSY, and saneless reports "Mount its directory instead"
- **Data volume:** `saneless-data:/var/lib/saneless` is required, not optional. It holds the job database and the `failed/` directory, where saneless preserves any scan it could not deliver to paperless-ngx. The image sets `XDG_STATE_HOME=/var/lib`, so its defaults put the job database, `failed/` and the log of one-shot commands such as `saneless jobs` under `/var/lib/saneless`, and mounting the volume there is all that is needed. See [Docker volumes](../reference/docker.md#volumes) for what accumulates in `failed/` and how to drain it
- **Reaching paperless-ngx:** the two stacks are separate compose projects, so each gets its own network and the name `paperless` does not resolve from here on its own. Either join the paperless-ngx network -- uncomment both `networks:` blocks above, after checking the real name with `docker network ls` -- and keep `url = "http://paperless:8000"`, or leave the networks alone and point `url` at the host the paperless-ngx stack publishes on, such as `http://192.168.1.10:8000`. Whichever you pick, the URL has to resolve from *inside* the saneless container: `localhost` there is the container itself, not your host
- **Network scanners:** Set `SANELESS_SCANNER__HOST=192.168.1.50` to discover scanners on a remote host. See [Scanner Host Discovery](scanner-host-discovery.md) for details

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
{"status": "ok"}
```

Then open the web UI at `http://localhost:8080` in your browser. You should see the scan interface with your configured profiles.

## Environment variable configuration

You can configure saneless entirely through environment variables using the `SANELESS_` prefix with `__` as the nested delimiter. This is useful when you prefer not to mount a config file:

!!! warning "An environment variable overrides `saneless.toml`"
    Environment variables sit above the config file, so a variable set in your compose file wins over the same setting in `config/saneless.toml` -- silently, with nothing in the UI to say where the value came from. That is why the example above sets the paperless-ngx connection in the file and not here. Pick one place per setting; for the token, make it `config/saneless.toml`.

| Setting | Environment Variable |
|---|---|
| Paperless URL | `SANELESS_PAPERLESS__URL` |
| Paperless token | `SANELESS_PAPERLESS__TOKEN` |
| Scanner host | `SANELESS_SCANNER__HOST` |
| Log level | `SANELESS_OUTPUT__LOG_LEVEL` |

See [Environment Variables](../reference/environment-variables.md) for the full list.

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

### Upgrading: settings are checked when saneless loads

This release checks more of `config/saneless.toml` when saneless starts, and
changes how a few settings are read. Go through this list before you start the
new image.

- **A `0` or out-of-range number now stops saneless loading.** Every numeric
  setting has a range, listed in the [configuration reference](../reference/configuration.md#output):
  `history_retention_days`, `history_max_rows`, `paperless_task_timeout`,
  `log_max_bytes`, `log_backup_count` and a profile's `resolution` among them.
  A value outside it, or `true`/`false` in place of a number, stops saneless
  with exit 2 and a line naming the key, such as
  `[output] history_retention_days: Input should be greater than or equal to 1`.
  There is no "no limit" value:
    - replace `history_retention_days = 0` with `history_retention_days = 36500`
      to keep history for about a century;
    - replace `history_max_rows = 0`, which erased the history, with the
      number of jobs you want kept, up to `1000000`.
- **Relative paths now follow the config file.** A relative `data_dir`,
  `tmp_dir`, `log_file` or `consume_dir` is resolved against the directory of
  the config file that loaded, not the directory saneless was started from.
  In the container that is `/etc/saneless`, so `data_dir = "data"` now means
  `/etc/saneless/data`. Write the path absolute, such as
  `data_dir = "/var/lib/saneless/data"`, to keep the old location. Nothing is
  moved for you.
- **Generated profile names change only when you ask.** Profiles that
  `auto-profiles` generated earlier keep their old `label` and `description`
  until you run `docker compose exec saneless saneless auto-profiles --force`.
  That rewrites only the tables marked `auto_generated = true`, keeps your
  `default_tags`, `title` and other keys, and leaves a profile without the
  marker alone. See [Auto-generated profiles](configure-scan-profiles.md#auto-generated-profiles)
  for the new wording.
- **Free space is counted in decimal megabytes.** `min_free_space_mb` now
  counts a megabyte as 1,000,000 bytes, as every figure saneless prints about
  free space does, so the same number reserves about 5% less than before.
- **The job database gains an index when saneless opens it.** Its schema
  version does not change, so an earlier release still opens the database if
  you roll back.
- **`default_title` in a profile is refused; write `title`.** An earlier
  release also loaded a profile's title spelled `default_title`. That spelling
  now stops saneless with exit 2 and a line saying to write it as `title`.
- **Look for a config file in the data volume.** An earlier release's
  `auto-profiles`, run with no `config/saneless.toml`, created
  `/var/lib/saneless/saneless.toml` in the data volume, and that file loads
  ahead of `./config`. This release never creates it and reports it: while
  both files exist, the Configuration row is amber and names both, and a file
  in the volume alone loads without a warning, so check either way:

    ```bash
    docker compose exec saneless cat /var/lib/saneless/saneless.toml
    ```

    If it exists, merge anything you still need from it into
    `config/saneless.toml` on the host -- the Configuration row's own advice
    runs the other way, because it keeps the file in use, but the file to keep
    is the one in `./config`. Only then delete it and restart:

    ```bash
    docker compose exec saneless rm /var/lib/saneless/saneless.toml
    docker compose restart saneless
    ```

### Upgrading: paperless-ngx 2.16 or later

This release needs paperless-ngx 2.16 or later. It speaks API version 9, and
10 once paperless-ngx says it allows it, as 3.x does. An older paperless-ngx
refuses both with `406`: the status strip and `GET /api/paperless/test` then
report `incompatible_version`, and every scan fails with `Paperless error: ...
does not accept API version 9 or 10; saneless needs paperless-ngx 2.16 or
later` (exit 3). Upgrade paperless-ngx first.

- An upload is sent again only when it cannot have reached paperless-ngx, for
  about 60 seconds, so a paperless-ngx restart is ridden out before the
  consume-directory fallback is used -- when saneless reaches paperless-ngx
  directly. Behind a reverse proxy, a restarting paperless-ngx usually shows
  up as a `502` or `503` from the proxy, which is treated as an upload that
  may have arrived: amber, exit 9, the PDF in `failed/`, no retry and no
  consume-folder copy. An upload that may have arrived is never
  resent or copied: it ends amber, **May be in paperless-ngx**, with the PDF
  in `failed/`, and `saneless scan` exits 9. Check paperless-ngx before you
  scan again.
- The consume directory is never created. If the mount is missing, a fallback
  fails with `does not exist — is the paperless-ngx volume mounted?` and the
  PDF is kept in `failed/`.
- A duplicate paperless-ngx refuses is now **Uploaded with a warning** (exit
  7), naming the document it already holds, rather than a failure.
- The web form now shows a profile's `default_tags` and
  `default_correspondent` pre-ticked, so an untouched form scans with them, as
  `saneless scan --profile` does. A default that no longer exists in
  paperless-ngx is skipped with a warning.
- A profile `title` longer than 118 characters, or a tag or correspondent id
  outside 1 to 2147483647, now stops the config from loading (exit 2).
- Jobs recorded by an earlier release keep the error category they were
  recorded with; nothing in the job history is rewritten.

### Upgrading: `[output] data_dir` now takes effect in the container

**If your `saneless.toml` sets `[output] data_dir`, the container now uses it.**
Before this release the image's environment set `SANELESS_OUTPUT__DATA_DIR` to
`/var/lib/saneless`, and an environment variable overrides `saneless.toml`, so
the key was silently ignored and everything went to `/var/lib/saneless`. The
image now sets only `XDG_STATE_HOME=/var/lib`, which is a default and nothing
more, so your own setting wins.

If you do nothing, saneless opens a new, empty job database in the directory
you configured. **The job history then appears empty, and any scans preserved
in `failed/` stay behind in the old volume**, where nothing reports them. Pick
one of these before you start the new image:

- **Keep using `/var/lib/saneless`.** Delete the `data_dir` line from
  `config/saneless.toml`. Nothing needs to move.
- **Use the directory you configured.** It must sit on a mount of its own, or
  it is lost when the container is recreated. Stop saneless, then copy the
  database and `failed/` across. For `data_dir = "/data"` with `./data`
  mounted at `/data`:

    ```bash
    docker compose stop saneless
    docker compose run --rm --no-deps --entrypoint sh saneless \
        -c 'cp -a /var/lib/saneless/saneless.db* /var/lib/saneless/failed /data/'
    docker compose up -d
    ```

    The `saneless.db*` glob copies the `-wal` and `-shm` files with the
    database; copy them only while saneless is stopped. `failed/` exists only
    if saneless ever preserved a scan, so `cp` complaining that it is missing
    is harmless. A bind-mounted `./data` must be writable by UID 1000 -- see
    [User and file ownership](../reference/docker.md#user-and-file-ownership).
    Check the job history and `/data/failed/` before you clear the old volume.

### Upgrading from config.toml to saneless.toml

Every container deployed from an earlier version of this guide holds
`config/config.toml`, which saneless no longer reads: `saneless.toml` is now
the one name it searches for, in every location. Rename the file:

```bash
mv config/config.toml config/saneless.toml
```

Then `docker compose up -d` to recreate the container.

**The old name is not a fallback.** In every search location saneless reads
`saneless.toml` and never `config.toml`, so until you rename the file your
deployment runs on its defaults plus whatever the `environment:` block sets --
which, if you followed this guide, is not the paperless-ngx connection.

**You will not have to guess.** The status page's first row, Configuration, turns **red** and names both the `/etc/saneless/config.toml` it found and the `/etc/saneless/saneless.toml` rename that fixes it. `saneless doctor` shows the same row and exits 2.

!!! warning "If an earlier release's `auto-profiles` ran in the container before you renamed"
    An earlier release's `auto-profiles`, run with no config file loaded, wrote `/var/lib/saneless/saneless.toml` into the data volume, and that file loads ahead of `/etc/saneless`. The current release never writes there, and it refuses to write anything while an old `config.toml` is the only config file found. If that volume file exists, the Configuration row is **amber** instead of red: a config file did load, and your `/etc/saneless/config.toml` is a leftover beside the loaded `saneless.toml`.

    That leftover is probably the only copy of your paperless-ngx URL and token. **Rename it first**, as above, so that it becomes `config/saneless.toml`. The row stays amber, now naming the volume file as the one in use and `/etc/saneless/saneless.toml` as a second file that is not read. The row's next step says to move what you need into the file in use; in the container, do it the other way round, because the file to keep is the one in `./config`. Print the volume file:

    ```bash
    docker compose exec saneless cat /var/lib/saneless/saneless.toml
    ```

    Copy anything you still need from it into `config/saneless.toml` on the host. Only then delete it and restart:

    ```bash
    docker compose exec saneless rm /var/lib/saneless/saneless.toml
    docker compose restart saneless
    ```

    Deleting either file before its contents are merged throws the token away.

**A `config.toml` that belongs to something else.** saneless searches its own working directory, so an unrelated tool's `config.toml` sitting there is reported in exactly the same way -- saneless reads only `saneless.toml` and cannot tell whose file it is. Move that file, or run saneless from a directory of its own.

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
them. **Delete them.** An environment variable overrides `saneless.toml`, so
while that line is there:

- The token in `config/saneless.toml` is ignored, however correct it is.
- If the value is a placeholder -- `changeme` is one of the literals saneless
  now detects -- the status strip stays **red**, the Scan button stays
  disabled, and `saneless scan` exits 2, no matter what you edit into the file.

After deleting the lines, put the connection in `config/saneless.toml`:

```toml
[paperless]
url = "http://paperless:8000"
token = "your-api-token-here"
```

Then `docker compose up -d` to recreate the container, and check the status
strip: the Paperless row turns green once the token is real and reachable.

While you are there, add `TZ` to the `environment:` block if it is not already
set. Without it the container reports UTC and every timestamp saneless shows is
UTC rather than your local time.

### Moving from a single-file config mount

Earlier versions of this guide bind-mounted the host's config file by itself,
read-only (`:ro`), onto `/etc/saneless/saneless.toml`. With that mount, every profile
write fails. A writable single-file bind mount makes the rename fail with EBUSY, and
saneless checks for a read-only mount before it writes anything. Either way it
reports:

```text
Cannot replace /etc/saneless/saneless.toml: it is bind-mounted as a single file. Mount its directory instead (see docs/how-to/deploy-docker-compose.md).
```

`saneless auto-profiles` exits with status 2. The server logs a warning and uses
the generated profiles for that run only; they do not survive a restart. Switch to
the directory mount:

1. Stop the stack: `docker compose down`
2. Move the file into a directory: `mkdir config && mv config.toml config/saneless.toml`
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
paperless-ngx, and profiles and settings come from `saneless.toml`. Preserved scans
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
- TLS verification now uses the operating system's trust store instead of a
  certificate bundle shipped inside a Python package. The stock image is
  unaffected, because it ships `ca-certificates`, and plain-`http://`
  deployments are unaffected either way. A private or corporate CA installed in
  the OS trust store now works where it previously did not. A private CA
  installed only by editing the Python bundle now fails with a certificate
  error: mount the CA file and point `SSL_CERT_FILE` at it.

    ```yaml
    services:
      saneless:
        environment:
          SSL_CERT_FILE: /etc/ssl/certs/my-ca.crt
        volumes:
          - ./my-ca.crt:/etc/ssl/certs/my-ca.crt:ro
    ```

    Create `./my-ca.crt` on the host **before** the stack comes up. Docker
    silently creates a *directory* at a bind-mount source that does not
    exist, so the container finds a directory where the PEM file should be,
    and saneless refuses to start with `Paperless error: Could not build the
    TLS trust store for Paperless at <url>: <OS error text>;
    check SSL_CERT_FILE and SSL_CERT_DIR`. If you hit that, put the file in
    place and recreate the container. Note also that `SSL_CERT_FILE` replaces the
    operating system's trust store rather than adding to it, so the file you
    name must carry every CA saneless needs -- if `paperless.url` is signed by
    a public CA, install the private CA into the OS trust store instead.

    Both variables are described in
    [Environment Variables](../reference/environment-variables.md#not-a-saneless-variable-ssl_cert_file-and-ssl_cert_dir),
    and [Troubleshoot a Failed Scan](troubleshoot-a-failed-scan.md#paperless-errors-exit-3)
    shows what the failure looks like from the CLI and from the web UI -- they
    do not look alike.
- The outgoing `User-Agent` is now `python-httpx2/<version>`. This matters only
  to a reverse proxy or WAF in front of paperless-ngx that filters on it.
