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
| 1 | Scan error (scanner unavailable, feeder jam, manual duplex aborted at the flip prompt or flip wait timed out, etc.) |
| 2 | Configuration or profile error (unknown profile, invalid config, or a manual duplex profile run without an interactive terminal) |
| 3 | Paperless-ngx upload error (unreachable, auth failure, etc.) |

For a profile with `duplex = "manual"`, `scan` pauses between the two passes and asks `Flip the stack over and load it back into the feeder. Scan the back sides? [Y/n]:`. Yes (the default) scans the back sides; no, Ctrl-C or end of input aborts the scan. An error reading the terminal at the prompt also aborts the scan at once, and the error is logged as `Flip prompt failed; treating it as an abort` with its traceback. When stdin is not a terminal, `scan` refuses the profile with exit code 2 before any page is fed. See [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md#manual-duplex).

---

## `saneless devices`

List available scanning devices discovered via SANE.

```
saneless [--config PATH] [-v] devices [--json] [--capabilities]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--json` | flag | off | Output device list as JSON |
| `--capabilities` | flag | off | Show each device's sources, modes and resolution support — either a list of values or a minimum/maximum/step range, whichever the device reports — plus its raw SANE option names |

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
| `--host` | TEXT | `0.0.0.0` (from config) | Bind address. The default `0.0.0.0` listens on all network interfaces |
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
| 2 | Configuration error — the config file could not be loaded |

Profiles are written to the config file that was loaded: the `--config` path, or else the first file found in the [config file search path](configuration.md#config-file-search-path). When no config file was loaded, they are written to `./saneless.toml`. Without `--force`, existing profiles are preserved and only new ones are added.

`saneless serve` also generates profiles once at startup when the config holds only the untouched `default` profile. See [Auto-generated profiles](../how-to/configure-scan-profiles.md#auto-generated-profiles).

A `default` profile is always written, because saneless requires one: your scanner's flatbed backs it when it has one, and otherwise its first reported source does. Regenerating never removes that profile; every other auto-generated profile a new run no longer produces is pruned, while profiles you wrote yourself are left alone.
