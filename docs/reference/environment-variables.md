# Environment Variables

All configuration can be set via environment variables, which override values from the TOML config file.

## Naming Convention

Environment variables use the `SANELESS_` prefix with double underscores (`__`) as the nesting delimiter:

```
SANELESS_{SECTION}__{FIELD}
```

For example, `scanner.host` in TOML becomes `SANELESS_SCANNER__HOST`.

## Priority Order

Settings are resolved in this order (highest to lowest priority):

1. Environment variables (`SANELESS_*`)
2. TOML config file
3. Built-in defaults

## Variable Reference

### Scanner

| Variable | Config Path | Type | Example |
|----------|-------------|------|---------|
| `SANELESS_SCANNER__HOST` | `scanner.host` | string | `192.168.1.50` |
| `SANELESS_SCANNER__DEVICE` | `scanner.device` | string | `net:192.168.1.50:pixma:MF740C` |

### Paperless

| Variable | Config Path | Type | Example |
|----------|-------------|------|---------|
| `SANELESS_PAPERLESS__URL` | `paperless.url` | string | `http://paperless:8000` |
| `SANELESS_PAPERLESS__TOKEN` | `paperless.token` | string | `abc123def456` |
| `SANELESS_PAPERLESS__CONSUME_DIR` | `paperless.consume_dir` | string | `/consume` |

### Output

| Variable | Config Path | Type | Example |
|----------|-------------|------|---------|
| `SANELESS_OUTPUT__TMP_DIR` | `output.tmp_dir` | string | `/tmp/saneless` |
| `SANELESS_OUTPUT__DATA_DIR` | `output.data_dir` | string | `/var/lib/saneless` |
| `SANELESS_OUTPUT__LOG_FILE` | `output.log_file` | string | `/var/log/saneless.log` |
| `SANELESS_OUTPUT__LOG_LEVEL` | `output.log_level` | string | `DEBUG` |
| `SANELESS_OUTPUT__LOG_MAX_BYTES` | `output.log_max_bytes` | int | `10485760` |
| `SANELESS_OUTPUT__LOG_BACKUP_COUNT` | `output.log_backup_count` | int | `5` |
| `SANELESS_OUTPUT__HISTORY_RETENTION_DAYS` | `output.history_retention_days` | int | `7` |
| `SANELESS_OUTPUT__HISTORY_MAX_ROWS` | `output.history_max_rows` | int | `500` |
| `SANELESS_OUTPUT__PAPERLESS_TASK_TIMEOUT` | `output.paperless_task_timeout` | int | `300` |
| `SANELESS_OUTPUT__PAPERLESS_CACHE_TTL_SECONDS` | `output.paperless_cache_ttl_seconds` | int | `60` |
| `SANELESS_OUTPUT__FLIP_TIMEOUT_SECONDS` | `output.flip_timeout_seconds` | int | `600` |
| `SANELESS_OUTPUT__MIN_FREE_SPACE_MB` | `output.min_free_space_mb` | int | `500` |
| `SANELESS_OUTPUT__WEB_HOST` | `output.web_host` | string | `0.0.0.0` (the default: all network interfaces) |
| `SANELESS_OUTPUT__WEB_PORT` | `output.web_port` | int | `8080` |

`SANELESS_OUTPUT__MIN_FREE_SPACE_MB` is the free disk space saneless keeps in reserve for assembling the PDF. It is checked twice: once before a scan starts, and again before each page is written to disk, against that page's size *plus* this reserve. A scan that runs out of room fails naming the page number and the path, and the pages already scanned are preserved. See [`[output]`](configuration.md#output) for every field in this section.

### Web

| Variable | Config Path | Type | Example |
|----------|-------------|------|---------|
| `SANELESS_WEB__SHOW_TAGS` | `web.show_tags` | bool | `false` |
| `SANELESS_WEB__SHOW_CORRESPONDENT` | `web.show_correspondent` | bool | `false` |

Both default to `true`. Setting one to `false` hides that control on the scan form; the profile's `default_tags` and `default_correspondent` still apply, so hiding a control changes the form and never the scan. The web server's bind address is **not** in this section: it is `SANELESS_OUTPUT__WEB_HOST` and `SANELESS_OUTPUT__WEB_PORT` above. See [`[web]`](configuration.md#web).

### Not a saneless variable: `TZ`

| Variable | Type | Example |
|----------|------|---------|
| `TZ` | string | `America/Chicago` |

`TZ` is the standard POSIX/container timezone variable, not a `SANELESS_` setting, and saneless never reads it directly -- Python's own local-time conversion does. It matters because saneless renders every user-facing timestamp in the server's local zone with the zone named: the web UI's job history and status area, the `saneless jobs` table, the status strip's `Last checked` line, and the `Scan <date time>` title a document gets in paperless-ngx when no title was given.

A container's clock reports UTC unless `TZ` is set, so **without it every one of those timestamps is UTC**, including the document title that ends up in paperless-ngx. Set it in your compose file's `environment:` block. A bare-metal install normally inherits the host's zone and needs nothing.

### Not a saneless variable: `SSL_CERT_FILE` and `SSL_CERT_DIR`

| Variable | Type | Example |
|----------|------|---------|
| `SSL_CERT_FILE` | path | `/etc/ssl/certs/my-ca.crt` |
| `SSL_CERT_DIR` | path | `/etc/ssl/my-ca-dir` |

These are OpenSSL's own variables, not `SANELESS_` settings, and saneless never reads them -- the TLS layer beneath its HTTP client does. They name the certificate authorities to trust when saneless connects to paperless-ngx over `https://`, and they are honoured **first**, ahead of the operating system's trust store. Their behaviour is unchanged: the previous HTTP client honoured them too. What changed is the default, which is now the operating system's trust store rather than a certificate bundle shipped inside a Python package. Set one of these only when your paperless-ngx certificate is signed by a private or corporate CA that is not installed on this machine; installing that CA into the OS trust store is the better fix wherever you can do it. [Troubleshoot a Failed Scan](../how-to/troubleshoot-a-failed-scan.md#paperless-errors-exit-3) describes the failure they resolve, under **TLS certificate not trusted**.

Prefer `SSL_CERT_FILE`. It takes a single PEM file and needs nothing else. `SSL_CERT_DIR` takes a directory and carries a trap: OpenSSL reads only files named `<8-hex-hash>.<n>` in it, so dropping a bare `.pem` into the directory fails exactly as if you had set nothing at all, with no diagnostic anywhere to tell you why. Run `c_rehash` over the directory, or make the link yourself with `ln -s my-ca.pem "$(openssl x509 -hash -noout -in my-ca.pem).0"`.

## Notes

- **Profile fields** use `SANELESS_PROFILES__<NAME>__<FIELD>`, for example `SANELESS_PROFILES__RECEIPT__TITLE=Receipt` for `title` in `[profiles.receipt]`. Variable names are case-insensitive, and profile names are lower-cased. A `default` profile must still exist: with no config file, set at least one `SANELESS_PROFILES__DEFAULT__<FIELD>` too, or loading fails.

- **Unknown variables are rejected.** A `SANELESS_` variable whose first segment after the prefix is not `SCANNER`, `PAPERLESS`, `OUTPUT`, `WEB` or `PROFILES` stops saneless at startup with exit code 2. The error names the variable and suggests a fix: `SANELESS_PAPERLES__TOKEN` suggests `SANELESS_PAPERLESS__TOKEN`, and a single underscore such as `SANELESS_OUTPUT_WEB_PORT` suggests `SANELESS_OUTPUT__WEB_PORT`. An unknown field in a known section, such as `SANELESS_SCANNER__HOSTNAME`, is reported naming the variable, the same way as an unknown key in the TOML file (see [Validation](configuration.md#validation)). Values are never printed.

- **saneless logs where its settings came from.** At startup it writes one INFO line naming the config file it loaded (or saying there was none) and the dotted names of the settings that came from environment variables, for example `paperless.url, paperless.token`. Names only, never values.

- **Docker deployments** commonly use environment variables for `SANELESS_PAPERLESS__URL`, `SANELESS_PAPERLESS__TOKEN`, and `SANELESS_SCANNER__HOST` while mounting a TOML file for profile definitions.

- **Multiple scanner hosts** can be specified in `SANELESS_SCANNER__HOST` using colon separation: `192.168.1.50:192.168.1.51`.

- **`SANELESS_OUTPUT__DATA_DIR` is durable state, `SANELESS_OUTPUT__TMP_DIR` is not.** `data_dir` holds the job database (`saneless.db`) and the `failed/` directory of scans that could not be delivered to paperless-ngx; it defaults to `$XDG_STATE_HOME/saneless` (`~/.local/state/saneless` when `XDG_STATE_HOME` is unset) and must survive restarts. `tmp_dir` is scratch space for the scan in progress and can be thrown away. The official container image already sets `SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless`, so you only need to set it yourself if you mount the volume somewhere else.
