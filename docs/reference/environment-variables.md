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

## Notes

- **Profile fields** use `SANELESS_PROFILES__<NAME>__<FIELD>`, for example `SANELESS_PROFILES__RECEIPT__TITLE=Receipt` for `title` in `[profiles.receipt]`. Variable names are case-insensitive, and profile names are lower-cased. A `default` profile must still exist: with no config file, set at least one `SANELESS_PROFILES__DEFAULT__<FIELD>` too, or loading fails.

- **Unknown variables are rejected.** A `SANELESS_` variable whose first segment after the prefix is not `SCANNER`, `PAPERLESS`, `OUTPUT` or `PROFILES` stops saneless at startup with exit code 2. The error names the variable and suggests a fix: `SANELESS_PAPERLES__TOKEN` suggests `SANELESS_PAPERLESS__TOKEN`, and a single underscore such as `SANELESS_OUTPUT_WEB_PORT` suggests `SANELESS_OUTPUT__WEB_PORT`. An unknown field in a known section, such as `SANELESS_SCANNER__HOSTNAME`, is reported naming the variable, the same way as an unknown key in the TOML file (see [Validation](configuration.md#validation)). Values are never printed.

- **saneless logs where its settings came from.** At startup it writes one INFO line naming the config file it loaded (or saying there was none) and the dotted names of the settings that came from environment variables, for example `paperless.url, paperless.token`. Names only, never values.

- **Docker deployments** commonly use environment variables for `SANELESS_PAPERLESS__URL`, `SANELESS_PAPERLESS__TOKEN`, and `SANELESS_SCANNER__HOST` while mounting a TOML file for profile definitions.

- **Multiple scanner hosts** can be specified in `SANELESS_SCANNER__HOST` using colon separation: `192.168.1.50:192.168.1.51`.

- **`SANELESS_OUTPUT__DATA_DIR` is durable state, `SANELESS_OUTPUT__TMP_DIR` is not.** `data_dir` holds the job database (`saneless.db`) and the `failed/` directory of scans that could not be delivered to paperless-ngx; it defaults to `$XDG_STATE_HOME/saneless` (`~/.local/state/saneless` when `XDG_STATE_HOME` is unset) and must survive restarts. `tmp_dir` is scratch space for the scan in progress and can be thrown away. The official container image already sets `SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless`, so you only need to set it yourself if you mount the volume somewhere else.
