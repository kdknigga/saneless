# CLI Commands

saneless provides five commands for scanning, device discovery, job history, web serving, and automatic profile generation.

## Global Options

These options apply to all commands and must appear **before** the subcommand name.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--config PATH` | string | *(search path)* | Path to TOML config file (overrides search path). A path that does not exist or is not a regular file is an error (exit code 2) |
| `-v, --verbose` | flag | off | Log saneless's own debug detail (DEBUG) to the log file and mirror it to stderr; other libraries and the web server keep the configured `log_level` |

`--help` on any command works without a valid config file; settings are loaded only when a command runs. Every command exits with code 2 when the configuration cannot be loaded, and prints one line per problem (see [Validation](configuration.md#validation)). On start, saneless logs at INFO which config file it loaded and which setting names came from environment variables.

---

## `saneless scan`

Scan a document and upload to paperless-ngx.

```
saneless [--config PATH] [-v] scan [--title TEXT] [--profile NAME]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--title` | TEXT | the profile's `title`, else `Scan <date time>` | Document title for paperless-ngx; a blank title counts as omitted |
| `--profile` | TEXT | `default` | Scan profile name from config |

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Scan and upload completed successfully |
| 1 | Scan error (scanner unavailable, feeder jam, manual duplex aborted at the flip prompt or flip wait timed out, etc.) |
| 2 | Configuration or profile error (unknown profile, invalid config, a `--config` file that does not exist, an unknown config key or `SANELESS_*` variable, or a manual duplex profile run without an interactive terminal) |
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
| `--force` | flag | off | Refresh the generated keys of profiles marked `auto_generated = true`; never changes a profile without it |

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Profiles generated successfully |
| 1 | No scanners found |
| 2 | Configuration error: the config could not be loaded, or the config file could not be rewritten (for example `config.toml` bind-mounted as a single file, which fails with EBUSY -- mount its directory instead) |

Profiles are written to the config file that was loaded: the `--config` path, or else the first file found in the [config file search path](configuration.md#config-file-search-path). When no config file was loaded, they are written to `./saneless.toml` in the current directory. In the Docker image, which sets no working directory, that is `/saneless.toml`, inside the container rather than the mounted `./config` directory, so create `config/config.toml` on the host first (see [Deploy with Docker Compose](../how-to/deploy-docker-compose.md)). A config file created from scratch gets mode `0600`; rewriting an existing file keeps its permission bits, owner and group, each when the process is permitted to set it and the filesystem supports it.

Without `--force`, a profile that already exists is left alone. With `--force`, the command merges rather than replaces: in a profile marked `auto_generated = true`, the generated keys are refreshed in place, and every other key (`default_tags`, `title`, and so on) and your comments are kept. A profile without `auto_generated = true` is never changed; it is skipped and reported. The output names the absolute path written, then prints one line for each kind of change that happened:

```text
Added: ...
Refreshed: ...
Skipped (not auto-generated): ...
Skipped (already exists; use --force to refresh): ...
Removed (scanner no longer offers it): ...
```

See [Auto-generated profiles](../how-to/configure-scan-profiles.md#auto-generated-profiles) for which keys are generated and how a hand edit is treated.

`saneless serve` also generates profiles once at startup when the config holds only the untouched `default` profile. See [Auto-generated profiles](../how-to/configure-scan-profiles.md#auto-generated-profiles).

A `default` profile is always written, because saneless requires one: your scanner's flatbed backs it when it has one, and otherwise its first reported source does. Regenerating never removes that profile; every other auto-generated profile a new run no longer produces is pruned, while profiles you wrote yourself are left alone.
