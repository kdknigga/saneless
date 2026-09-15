# CLI Commands

saneless provides five commands for scanning, device discovery, job history, web serving, and automatic profile generation.

## Global Options

These options apply to all commands and must appear **before** the subcommand name.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--config PATH` | string | *(search path)* | Path to TOML config file (overrides search path). A path that does not exist or is not a regular file is an error (exit code 2) |
| `-v, --verbose` | flag | off | Log saneless's own debug detail (DEBUG) to the log file and mirror it to stderr; other libraries and the web server keep the configured `log_level` |

`--help` on any command works without a valid config file; settings are loaded only when a command runs. Every command exits with code 2 when the configuration cannot be loaded, and prints a header naming the file then one line per problem; a TOML syntax error names its line and column (see [Validation](configuration.md#validation)). On start, saneless logs at INFO which config file it loaded and which setting names came from environment variables.

## Exit codes

Every command uses the same exit codes. Each failure prints one line to stderr, with no traceback.

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Scan error: the scanner failed, the feeder was empty, no pages were scanned, or the flip wait timed out |
| 2 | Configuration, profile or setup error: invalid config, unknown profile, python-sane not installed, the web server cannot start, or a job database saneless cannot use (unreadable, or an unsupported schema) |
| 3 | Paperless-ngx error: unreachable after retries, upload rejected, or a malformed Paperless URL |
| 4 | PDF assembly error: the scanned pages could not be written as a PDF |
| 5 | Unexpected error only: a saneless bug. The line names the exception type and the traceback is in the log file |
| 130 | Cancelled by the operator |

Every command exits 5 on an unexpected error, and 130 on Ctrl-C, except `serve` once the web server is running, where Ctrl-C is a graceful stop that exits 0. Each command's table below lists the codes it can return. See [Troubleshoot a Failed Scan](../how-to/troubleshoot-a-failed-scan.md) for what to check for each code.

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
| 1 | Scan error (scanner unavailable, feeder jam, empty feeder, no pages scanned, flip wait timed out, or the terminal failed at the flip prompt) |
| 2 | Configuration or profile error (unknown profile, invalid config, a `--config` file that does not exist, an unknown config key or `SANELESS_*` variable, a manual duplex profile run without an interactive terminal, no scanner found, or python-sane not installed) |
| 3 | Paperless-ngx upload error (unreachable after retries, upload rejected, malformed Paperless URL) |
| 4 | PDF assembly error (disk full, unwritable output directory) |
| 5 | Unexpected error (a saneless bug; the traceback is in the log file) |
| 130 | Cancelled (no, Ctrl-D or Ctrl-C at the flip prompt, or Ctrl-C during the scan) |

For a profile with `duplex = "manual"`, `scan` pauses between the two passes and asks `Flip the stack over and load it back into the feeder. Scan the back sides? [Y/n]:`. Yes (the default) scans the back sides. No, Ctrl-D (end of input) or Ctrl-C at the flip prompt cancels the scan, prints one line and exits with code 130. A terminal read error at the flip prompt fails the scan with exit code 1, and the error is logged with its traceback. When stdin is not a terminal, `scan` refuses the profile with exit code 2 before any page is fed. See [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md#manual-duplex).

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
| 1 | Scan error (SANE failed while listing devices or reading capabilities) |
| 2 | Configuration error (invalid config, or python-sane not installed) |
| 5 | Unexpected error (a saneless bug; the traceback is in the log file) |
| 130 | Cancelled (Ctrl-C) |

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
| 2 | Configuration error (invalid config, or the job database is unreadable or has an unsupported schema) |
| 5 | Unexpected error (a saneless bug; the traceback is in the log file) |
| 130 | Cancelled (Ctrl-C) |

`jobs` does not need python-sane, and on a fresh install it creates the data directory and prints an empty history.

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
| 0 | Clean shutdown, including Ctrl-C once the web server is running |
| 2 | Cannot start (port already in use, web server failed to start, SANE could not be initialised, python-sane not installed, invalid config, or the job database is unreadable or has an unsupported schema) |
| 3 | Malformed Paperless URL |
| 5 | Unexpected error (a saneless bug; the traceback is in the log file) |
| 130 | Cancelled (Ctrl-C before the web server has started) |

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
| 1 | Scan error (SANE failed while listing devices, or could not open or read the scanner's capabilities) |
| 2 | Configuration or setup error: the config could not be loaded, no scanner found, python-sane is not installed, or the config file could not be rewritten (for example `config.toml` bind-mounted as a single file, which fails with EBUSY -- mount its directory instead) |
| 5 | Unexpected error (a saneless bug; the traceback is in the log file) |
| 130 | Cancelled (Ctrl-C) |

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
