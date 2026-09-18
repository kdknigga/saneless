# Configuration (TOML)

saneless uses a TOML configuration file with environment variable overrides. All settings have sensible defaults -- a minimal config only needs `[paperless]` credentials and a `[profiles.default]` section.

## Config File Search Path

Settings are loaded from the first file found, in priority order:

1. `--config PATH` -- explicit CLI flag (highest priority)
2. `./saneless.toml` -- current working directory
3. `$XDG_CONFIG_HOME/saneless/config.toml` -- XDG config directory (`~/.config/saneless/config.toml` when `XDG_CONFIG_HOME` is unset, empty or relative)
4. `/etc/saneless/config.toml` -- system-wide (typical for Docker)

If no file is found, defaults and environment variables are used.

A path that is not a regular file (for example a directory) is skipped. An explicit `--config PATH` that does not exist, or is not a regular file, is an error (exit code 2). A leading `~` in `--config` is expanded to your home directory.

## Validation

Every section rejects keys it does not know, and so does the top level. A misspelt key is an error when the config loads, not a setting that is silently ignored. The error names the file (or the environment variable that supplied the value), the section and the key, suggests a close match when there is one, and lists the valid keys (or names the section a misplaced key belongs in). Type and value errors use the same `[section] key` form. Each problem gets its own line, and values are never printed, so a token in a mistyped key does not end up in your terminal or log. saneless then exits with code 2.

```text
Configuration error in /etc/saneless/config.toml:
  [paperless] unknown key 'tokne' (did you mean 'token'?); valid keys: url, token, consume_dir
  [paperless] unknown key 'web_port'; it belongs in [output]
```

Unknown `SANELESS_*` environment variables are rejected the same way; see [Environment Variables](environment-variables.md#notes).

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
| `token` | string | `""` | API authentication token. Never written to logs or error messages. |
| `consume_dir` | string | `""` | Fallback directory for PDF deposit when API is unavailable. A leading `~` is expanded. |

## `[output]`

Output, logging, and web server settings.

In the path settings (`tmp_dir`, `data_dir`, `log_file`, and `consume_dir` under `[paperless]`), a leading `~` is expanded to your home directory. Environment variables such as `$HOME` inside a value are not expanded.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tmp_dir` | string | `"/tmp/saneless"` | Scratch space for the scan in progress; its contents are deleted as each scan finishes and nothing durable is kept here. A leading `~` is expanded |
| `data_dir` | string | `$XDG_STATE_HOME/saneless` (`~/.local/state/saneless` when `XDG_STATE_HOME` is unset) | Durable state: the job database (`saneless.db`) and `failed/`, where scans that could not be delivered to paperless-ngx are preserved. Must survive restarts. The container image sets this to `/var/lib/saneless`. A leading `~` is expanded |
| `log_file` | string | `$XDG_STATE_HOME/saneless/saneless.log` (`~/.local/state/saneless/saneless.log` when `XDG_STATE_HOME` is unset) | Log file path, **for one-shot CLI commands only**. `saneless serve` streams its records to stderr and writes no file at all, so under Docker or systemd the platform (`docker logs`, journald) holds them and owns retention. A leading `~` is expanded |
| `log_level` | string | `"INFO"` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, case-insensitive (`warn` means `WARNING`); any other value is rejected when the config loads. Applies in **both** modes, unlike the three keys around it. `saneless -v` shows saneless's own debug detail without changing this setting |
| `log_max_bytes` | int | `10485760` | Max log file size before rotation (10 MB). **One-shot CLI commands only**, like `log_file`: `saneless serve` writes no file, so there is nothing to rotate |
| `log_backup_count` | int | `5` | Number of rotated log files to keep. **One-shot CLI commands only**, like `log_file`: `saneless serve` writes no file, so there is nothing to keep |
| `history_retention_days` | int | `7` | Days to keep job history, by creation time and regardless of whether the job finished |
| `history_max_rows` | int | `500` | Maximum job history entries retained in SQLite; the newest are kept |
| `paperless_task_timeout` | int | `300` | Seconds to wait for paperless-ngx task completion |
| `paperless_cache_ttl_seconds` | int | `60` | Cache TTL for paperless tag/correspondent lists (seconds) |
| `flip_timeout_seconds` | int | `600` | Seconds a manual duplex scan waits for the operator to flip the stack between passes, in the web UI and the CLI. If nobody confirms in time, the job fails and nothing is uploaded. Must be a whole number of seconds from 1 to 86400 (one day); 0 and negative values are rejected when the config is loaded, so there is no "wait forever" setting |
| `min_free_space_mb` | int | `500` | Free disk space (MB) saneless keeps in reserve for assembling the PDF. It is checked twice: once before a scan starts, and again before each page is written to disk, against that page's size *plus* this reserve. A scan that runs out of room fails naming the page number and the path, and the pages already scanned are preserved |
| `web_host` | string | `"0.0.0.0"` | Web server bind address. The default `0.0.0.0` listens on all network interfaces |
| `web_port` | int | `8080` | Web server port |

## `[web]`

Which optional controls the scan form shows. Both default to `true`, so an existing deployment's form is unchanged by upgrading.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `show_tags` | bool | `true` | Show the Tags checkbox list on the scan form. `false` hides the whole Tags block, filter included |
| `show_correspondent` | bool | `true` | Show the Correspondent dropdown on the scan form. `false` hides it |

This is one appliance with one configured form shape, not a per-browser preference: everyone who opens the page sees the same form, and there is no control in the UI to turn either back on. Edit the file and restart saneless.

**Hiding a control changes the form, never the scan.** The profile's `default_tags` and `default_correspondent` still apply to every scan it runs, exactly as they do when the controls are visible and left untouched -- the same way a blank title still falls back to the profile's `title`. So `show_tags = false` with `default_tags = [3, 7]` means every scan from that profile is tagged 3 and 7, and nobody has to think about it. Use this to hand a household member a form with a Profile, a Title and a Scan button.

**The bind address is not here.** `web_host` and `web_port` stayed under [`[output]`](#output), where they have always been, because moving them would break every deployment that already sets them or their `SANELESS_OUTPUT__WEB_*` variables. `[web]` holds only the form-shape keys.

```toml
[web]
show_tags = true
show_correspondent = false
```

## `[profiles.NAME]`

Scan profiles define scanner settings and default metadata. At least one profile named `default` must exist.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | `""` | What the web UI's profile dropdown calls this profile, at most 64 characters. Empty means the option shows the profile's own name. `saneless auto-profiles` fills it in ("Feeder, double-sided") |
| `description` | string | `""` | One short sentence shown beneath the dropdown when this profile is selected, at most 200 characters. Empty means no description line. `saneless auto-profiles` fills it in |
| `source` | string | `"Flatbed"` | Scan source, as your scanner reports it: for example `Flatbed`, `ADF`, `ADF Duplex`, `Auto`. Run `saneless devices --capabilities` to list them. |
| `duplex` | string | `"none"` | How both sides of a sheet are scanned: `none`, `hardware` or `manual`. `manual` runs the two-pass flip workflow and needs a single-sided feeder source (see [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md#manual-duplex)). `hardware` is declarative: nothing reads it, and the scanner still decides from `source` whether to scan both sides; it records what the scanner does so the profile describes itself. |
| `auto_source_mode` | string | `"flatbed"` | When source is `"Auto"`: route as `"flatbed"` (single page) or `"adf"` (multi-page feeder). Ignored for explicit sources. |
| `paper_size` | string | `"full"` | Constrain scan area to a standard paper size. Presets: `full` (entire scanner bed), `a3`, `a4`, `a5`, `letter`, `legal`. Sets SANE geometry options when supported; falls back to post-scan crop otherwise. |
| `resolution` | int | `300` | Scan resolution in DPI |
| `mode` | string | `"color"` | Color mode: `Color`, `Gray`, `Lineart` |
| `default_tags` | int[] | `[]` | Paperless-ngx tag IDs to apply automatically |
| `default_correspondent` | int or null | `null` | Paperless-ngx correspondent ID |
| `title` | string | `""` | Default document title, used as written when the title is left blank (typed title first, then this, then `Scan <date time>`) |
| `empty_page_mean_threshold` | float | `250.0` | Mean pixel value threshold for empty page detection |
| `empty_page_stddev_threshold` | float | `5.0` | Standard deviation threshold for empty page detection |
| `enable_empty_page_detection` | bool | `true` | Enable automatic empty page removal |
| `auto_generated` | bool | `false` | Whether this profile was auto-generated from scanner capabilities |

`auto_generated = true` marks a profile as tool-owned: `saneless auto-profiles --force` rewrites its generated keys, `label` and `description` among them, so anything you write there is replaced the next time you run it. **To take a profile over, delete its `auto_generated` line.** saneless then leaves the whole profile alone, and your own `label` and `description` are what the dropdown shows. See [Configure Scan Profiles](../how-to/configure-scan-profiles.md) for the full ownership rules.

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
data_dir = "/var/lib/saneless"
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

[web]
show_tags = true
show_correspondent = true

[profiles.default]
label = "Glass (flatbed)"
description = "One page at a time from the scanner glass."
source = "Flatbed"
resolution = 300
mode = "Color"

[profiles.receipts]
source = "ADF"
resolution = 200
mode = "Gray"
paper_size = "letter"
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

[profiles.auto]
source = "Auto"
auto_source_mode = "adf"
resolution = 300
mode = "Color"
```
