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
| `SANELESS_OUTPUT__LOG_FILE` | `output.log_file` | string | `/var/log/saneless.log` |
| `SANELESS_OUTPUT__LOG_LEVEL` | `output.log_level` | string | `DEBUG` |
| `SANELESS_OUTPUT__LOG_MAX_BYTES` | `output.log_max_bytes` | int | `10485760` |
| `SANELESS_OUTPUT__LOG_BACKUP_COUNT` | `output.log_backup_count` | int | `5` |
| `SANELESS_OUTPUT__HISTORY_RETENTION_DAYS` | `output.history_retention_days` | int | `7` |
| `SANELESS_OUTPUT__HISTORY_MAX_ROWS` | `output.history_max_rows` | int | `500` |
| `SANELESS_OUTPUT__PAPERLESS_TASK_TIMEOUT` | `output.paperless_task_timeout` | int | `300` |
| `SANELESS_OUTPUT__PAPERLESS_CACHE_TTL_SECONDS` | `output.paperless_cache_ttl_seconds` | int | `60` |
| `SANELESS_OUTPUT__MIN_FREE_SPACE_MB` | `output.min_free_space_mb` | int | `500` |
| `SANELESS_OUTPUT__WEB_HOST` | `output.web_host` | string | `0.0.0.0` |
| `SANELESS_OUTPUT__WEB_PORT` | `output.web_port` | int | `8080` |

## Notes

- **Profile fields cannot be set via environment variables.** Use the TOML config file for `[profiles.*]` sections. The pydantic-settings nested delimiter does not support dynamic dict keys.

- **Docker deployments** commonly use environment variables for `SANELESS_PAPERLESS__URL`, `SANELESS_PAPERLESS__TOKEN`, and `SANELESS_SCANNER__HOST` while mounting a TOML file for profile definitions.

- **Multiple scanner hosts** can be specified in `SANELESS_SCANNER__HOST` using colon separation: `192.168.1.50:192.168.1.51`.
