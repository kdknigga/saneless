# Configuration (TOML)

saneless uses a TOML configuration file with environment variable overrides. All settings have sensible defaults -- a minimal config only needs `[paperless]` credentials and a `[profiles.default]` section.

## Config File Search Path

Settings are loaded from the first file found, in priority order:

1. `--config PATH` -- explicit CLI flag (highest priority)
2. `./saneless.toml` -- current working directory
3. `~/.config/saneless/config.toml` -- XDG config directory
4. `/etc/saneless/config.toml` -- system-wide (typical for Docker)

If no file is found, defaults and environment variables are used.

---

## `[scanner]`

Scanner connection settings.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `""` | SANE net host IP/hostname. Empty = local USB. Colon-separated for multiple hosts (e.g., `192.168.1.50:192.168.1.51`). |
| `device` | string | `""` | Pin a specific SANE device name. Empty = auto-detect first available. |

## `[paperless]`

Paperless-ngx API connection settings.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | `""` | Paperless-ngx base URL (e.g., `http://paperless:8000`) |
| `token` | string | `""` | API authentication token |
| `consume_dir` | string | `""` | Fallback directory for PDF deposit when API is unavailable |

## `[output]`

Output, logging, and web server settings.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tmp_dir` | string | `"/tmp/saneless"` | Temporary directory for scan files and SQLite database |
| `log_file` | string | `"~/.local/state/saneless/saneless.log"` | Log file path (XDG state directory) |
| `log_level` | string | `"INFO"` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `log_max_bytes` | int | `10485760` | Max log file size before rotation (10 MB) |
| `log_backup_count` | int | `5` | Number of rotated log files to keep |
| `history_retention_days` | int | `7` | Days to keep completed job history |
| `history_max_rows` | int | `500` | Maximum job history entries in SQLite |
| `paperless_task_timeout` | int | `300` | Seconds to wait for paperless-ngx task completion |
| `paperless_cache_ttl_seconds` | int | `60` | Cache TTL for paperless tag/correspondent lists (seconds) |
| `min_free_space_mb` | int | `500` | Minimum free disk space (MB) required before a scan starts |
| `web_host` | string | `"0.0.0.0"` | Web server bind address |
| `web_port` | int | `8080` | Web server port |

## `[profiles.NAME]`

Scan profiles define scanner settings and default metadata. At least one profile named `default` must exist.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `source` | string | `"Flatbed"` | Scan source: `Flatbed`, `ADF`, `ADF Duplex`, `Auto` |
| `auto_source_mode` | string | `"flatbed"` | When source is `"Auto"`: route as `"flatbed"` (single page) or `"adf"` (multi-page feeder). Ignored for explicit sources. |
| `resolution` | int | `300` | Scan resolution in DPI |
| `mode` | string | `"color"` | Color mode: `Color`, `Gray`, `Lineart` |
| `default_tags` | int[] | `[]` | Paperless-ngx tag IDs to apply automatically |
| `default_correspondent` | int or null | `null` | Paperless-ngx correspondent ID |
| `title` | string | `""` | Default title template |
| `empty_page_mean_threshold` | float | `250.0` | Mean pixel value threshold for empty page detection |
| `empty_page_stddev_threshold` | float | `5.0` | Standard deviation threshold for empty page detection |
| `enable_empty_page_detection` | bool | `true` | Enable automatic empty page removal |
| `auto_generated` | bool | `false` | Whether this profile was auto-generated from scanner capabilities |

---

## Complete Example

```toml
[scanner]
host = "192.168.1.50"
device = ""

[paperless]
url = "http://paperless:8000"
token = "abc123def456ghi789"
consume_dir = ""

[output]
tmp_dir = "/tmp/saneless"
log_file = "/var/log/saneless/saneless.log"
log_level = "INFO"
log_max_bytes = 10485760
log_backup_count = 5
history_retention_days = 7
history_max_rows = 500
paperless_task_timeout = 300
paperless_cache_ttl_seconds = 60
min_free_space_mb = 500
web_host = "0.0.0.0"
web_port = 8080

[profiles.default]
source = "Flatbed"
resolution = 300
mode = "Color"

[profiles.receipts]
source = "ADF"
resolution = 200
mode = "Gray"
default_tags = [3, 7]
default_correspondent = 12
title = "Receipt"
enable_empty_page_detection = true

[profiles.duplex-letters]
source = "ADF Duplex"
resolution = 300
mode = "Color"
default_tags = [1]
title = "Letter"

[profiles.auto-scan]
source = "Auto"
auto_source_mode = "adf"
resolution = 300
mode = "Color"
```
