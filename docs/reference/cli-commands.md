# CLI Commands

saneless provides five commands for scanning, device discovery, job history, web serving, and automatic profile generation.

## Global Options

These options apply to all commands and must appear **before** the subcommand name.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--config PATH` | string | *(search path)* | Path to TOML config file (overrides search path) |
| `-v, --verbose` | flag | off | Enable debug logging (sets log level to DEBUG) |

---

## `saneless scan`

Scan a document and upload to paperless-ngx.

```
saneless [--config PATH] [-v] scan --title TEXT [--profile NAME]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--title` | TEXT | *(required)* | Document title for paperless-ngx |
| `--profile` | TEXT | `default` | Scan profile name from config |

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Scan and upload completed successfully |
| 1 | Scan error (scanner unavailable, feeder jam, etc.) |
| 2 | Configuration or profile error (unknown profile, invalid config) |
| 3 | Paperless-ngx upload error (unreachable, auth failure, etc.) |

---

## `saneless devices`

List available scanning devices discovered via SANE.

```
saneless [--config PATH] [-v] devices [--json] [--capabilities]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--json` | flag | off | Output device list as JSON |
| `--capabilities` | flag | off | Show raw SANE options for each device |

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Success (even if no devices found) |

**Example output (table):**

```
Discovering scanners...
Name                 Vendor          Model                Type
------------------------------------------------------------
net:192.168.1.50:pi  Canon           MF740C Series        scanner
```

**Example output (JSON):**

```json
[
  {
    "name": "net:192.168.1.50:pixma:MF740C",
    "vendor": "Canon",
    "model": "MF740C Series",
    "type": "scanner"
  }
]
```

---

## `saneless jobs`

List recent scan job history from the SQLite database.

```
saneless [--config PATH] [-v] jobs [--json] [--limit N]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--json` | flag | off | Output job history as JSON |
| `--limit` | int | `20` | Maximum number of jobs to show |

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Success |

---

## `saneless serve`

Start the web server (FastAPI + uvicorn).

```
saneless [--config PATH] [-v] serve [--host ADDR] [--port N]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--host` | TEXT | `0.0.0.0` (from config) | Bind address |
| `--port` | int | `8080` (from config) | Bind port |

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Clean shutdown |
| 1 | Port bind error (address already in use) |

---

## `saneless auto-profiles`

Generate scan profiles automatically from scanner capabilities.

```
saneless [--config PATH] [-v] auto-profiles [--force]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--force` | flag | off | Overwrite existing profiles in config file |

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Profiles generated successfully |
| 1 | No scanners found |

Profiles are written to the TOML config file. Without `--force`, existing profiles are preserved and only new ones are added.
