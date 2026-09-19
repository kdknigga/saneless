# CLI Commands

saneless provides six commands for scanning, device discovery, job history, web serving, automatic profile generation, and a readiness check.

## Global Options

These options apply to all commands and must appear **before** the subcommand name.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--config PATH` | string | *(search path)* | Path to TOML config file (overrides search path). A path that does not exist or is not a regular file is an error (exit code 2) |
| `-v, --verbose` | flag | off | Raise saneless's own loggers to DEBUG. In a one-shot command the detail goes to the log file and is mirrored to stderr; in `saneless serve` it goes to the stream, which is stderr and the only sink there is. Other libraries and the web server keep the configured `log_level` |

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
| 5 | Unexpected error only: a saneless bug. The line names the exception type and the traceback is in the log file -- or, under `saneless serve`, in the stream, because a service writes no file |
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
| `--title` | TEXT | the profile's `title`, else `Scan <local date time with the zone named>` | Document title for paperless-ngx; a blank title counts as omitted |
| `--profile` | TEXT | `default` | Scan profile name from config |

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Scan and upload completed successfully |
| 1 | Scan error (scanner unavailable, feeder jam, empty feeder, no pages scanned, flip wait timed out, or a read error at the flip prompt) |
| 2 | Configuration or profile error (unknown profile, invalid config, a `--config` file that does not exist, an unknown config key or `SANELESS_*` variable, a manual duplex profile run without an interactive terminal, an unset or placeholder paperless-ngx API token, no scanner found, or python-sane not installed) |
| 3 | Paperless-ngx upload error (unreachable after retries, upload rejected, malformed Paperless URL) |
| 4 | PDF assembly error (disk full, unwritable output directory) |
| 5 | Unexpected error (a saneless bug; the traceback is in the log file) |
| 130 | Cancelled (no, Ctrl-D or Ctrl-C at the flip prompt, or Ctrl-C during the scan) |

With neither `--title` nor a profile `title`, the document title is the scan's start time rendered in the server's local timezone with the zone named, for example `Scan 2026-03-22 09:30 CDT`. Set `TZ` on the server (or in `docker-compose.yml`) if that zone is wrong; a container reports UTC unless you do.

If the paperless-ngx API token is unset, blank or still one of the shipped placeholders such as `changeme`, `scan` refuses with exit code 2 before the scanner is opened, so no paper is fed for an upload that cannot succeed. A configured `paperless.consume_dir` fallback does not change this: run `saneless doctor` to see the same fact the web UI reports.

For a profile with `duplex = "manual"`, `scan` pauses between the two passes and asks `Flip the stack over and load it back into the feeder. Scan the back sides? [Y/n]:`. Yes (the default) scans the back sides. No, Ctrl-D (end of input, which is also what a terminal that closes produces) or Ctrl-C at the flip prompt cancels the scan, prints one line and exits with code 130. A read error at the flip prompt, such as an I/O error or undecodable input, fails the scan with exit code 1, and the error is logged with its traceback. When stdin is not a terminal, `scan` refuses the profile with exit code 2 before any page is fed. See [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md#manual-duplex).

---

## `saneless devices`

List available scanning devices discovered via SANE.

```
saneless [--config PATH] [-v] devices [--json] [--capabilities]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--json` | flag | off | Output device list as JSON |
| `--capabilities` | flag | off | Show each device's sources, modes and resolution support — either a list of values or a minimum/maximum/step range, whichever the device reports — plus its raw SANE option names. With `--json`, each device object gains a `capabilities` object |

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Success (even if no devices found) |
| 1 | Scan error (SANE failed while listing devices, or at least one device's capabilities could not be read; the other devices are still reported) |
| 2 | Configuration error (invalid config, or python-sane not installed) |
| 5 | Unexpected error (a saneless bug; the traceback is in the log file) |
| 130 | Cancelled (Ctrl-C) |

Only data goes to stdout. The `Discovering scanners...` status line, which the table mode prints, and any per-device capability error go to stderr, so `saneless devices | grep` and `saneless devices --json | jq` see the device list and nothing else.

**Example output (table):**

```
Discovering scanners...
Name                 Vendor          Model                Type
------------------------------------------------------------
net:192.168.1.50:pi  Canon           MF740C Series        scanner
```

The first line is on stderr; the table is on stdout.

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

Without `--capabilities` the JSON has exactly these four keys per device, in this order.

**Example output (`--json --capabilities`):**

```json
[
  {
    "name": "net:192.168.1.50:pixma:MF740C",
    "vendor": "Canon",
    "model": "MF740C Series",
    "type": "scanner",
    "capabilities": {
      "sources": ["Flatbed", "ADF Simplex", "ADF Duplex"],
      "resolutions": [150, 300, 600],
      "modes": ["Color", "Gray", "Lineart"],
      "raw_options": ["source", "mode", "resolution"]
    }
  },
  {
    "name": "test:0",
    "vendor": "Noname",
    "model": "frontend-tester",
    "type": "virtual device",
    "capabilities": null,
    "capabilities_error": "Could not open scanner test:0: Device busy"
  }
]
```

The output is one JSON document. A `capabilities` object has a key only for what the device reported. A device that constrains resolution with a range gets `"resolution_range": {"min": 1.0, "max": 1200.0, "step": 1.0}` instead of `resolutions`, and the numbers are the ones the device gave. When a device's capabilities cannot be read, that device gets `"capabilities": null` and a one-line `"capabilities_error"`. Every other device is still reported, the same reason is printed on stderr as `Capabilities for <name>: <reason>`, and the command exits 1 after writing the whole document. The table mode does the same: it prints that stderr line for the failed device, lists the others, and exits 1.

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

The table's `Timestamp` column renders each job's start time in the server's local timezone with the zone named, for example `2026-03-22 09:30 CDT`, and drops seconds. `--json` is a machine contract and is unaffected: its `created_at` stays a UTC ISO-8601 string carrying the `+00:00` offset. See [Use the CLI for Scripting](../how-to/cli-scripting.md#job-history-json).

`jobs` does not need python-sane, and on a fresh install it creates the data directory and prints an empty history.

---

## `saneless doctor`

Check that saneless is ready to scan, printing one line per health check.

```
saneless [--config PATH] [-v] doctor
```

`doctor` takes no options of its own. It runs the same five checks the web UI's system status list shows, in the same order and with the same wording, so the command and the page cannot disagree about whether the appliance is healthy.

| Check | What it looks at |
|-------|------------------|
| Scanner | Whether scanner support is installed and a device answers. A configured sane-net host has its saned port probed first, so an unplugged network scanner is reported in about two seconds rather than two minutes |
| Paperless | Whether the API token has been set to something real, and whether paperless-ngx accepts it. A placeholder token is reported without sending a request |
| Profiles | Whether any scan profiles are configured, whether they were saved to a config file, and whether the generated ones have names yet |
| Fallback | Whether a fallback folder is configured for when paperless-ngx is down, and whether saneless can write to it |
| Data folder | Whether the folder holding the job database will take a write |

**Example output:**

```text
[ OK ] Scanner     Canon MF740C Series is ready.
[ OK ] Paperless   Connected to paperless-ngx.
[ OK ] Profiles    4 scan profiles configured.
[WARN] Fallback    Not configured; scans cannot be kept if paperless-ngx is down.
                   Set a fallback folder in the saneless config so scans are kept when paperless-ngx is down.
[ OK ] Data folder The data folder is writable.
```

Every `[WARN]` and `[FAIL]` row is followed by an indented next step. An `[ OK ]` row has nothing to do about it and prints no second line.

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Every check came out OK or a warning |
| 2 | At least one check failed (scanner support not installed, no scanner reachable, an unset or rejected API token, no scan profiles, or a folder saneless cannot write to), or the configuration could not be loaded |
| 5 | Unexpected error (a saneless bug; the traceback is in the log file) |
| 130 | Cancelled (Ctrl-C) |

**A warning does not fail the command.** An appliance that scans and files correctly is not broken because it could be tidier, and a health gate that goes red for tidiness is one people learn to ignore. Only a failure exits non-zero, so `if saneless doctor; then ...` means "everything that stops scanning or filing is fine".

Unlike `scan`, `devices`, `serve` and `auto-profiles`, `doctor` does **not** refuse to run when python-sane is missing. That machine is exactly the one whose owner needs a diagnosis, so the missing scanner support becomes one failed row among five and the other four checks still report.

`doctor` has no `--json` mode: it prints a table for a person to read, and scripts should gate on the exit code.

The container `HEALTHCHECK` deliberately keeps calling `/health` instead of this command. `doctor` does network I/O — it probes the scanner and talks to paperless-ngx — so wiring it to the healthcheck would mark the container unhealthy during a routine paperless-ngx restart, and restart saneless for a fault that is not saneless's.

---

## `saneless serve`

Start the web server (FastAPI + uvicorn).

```
saneless [--config PATH] [-v] serve [--host ADDR] [--port N]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--host` | TEXT | `0.0.0.0` (from config) | Bind address. The default `0.0.0.0` listens on all network interfaces. IPv6 addresses such as `::1` or `::` work too |
| `--port` | int | `8080` (from config) | Bind port, 0 to 65535. `--port 0` lets the OS choose a free port. A value outside that range is a usage error, exit 2 |

Once the address is bound, `serve` prints `Serving on http://<host>:<port>` to stderr and logs the same line. The port on that line is the one actually bound, so with `--port 0` it names the port the OS chose. An IPv6 address is shown in brackets, for example `Serving on http://[::1]:43127`.

`--host` takes an address. A hostname is resolved and only its first address is bound. Binding `::` listens on IPv6 only, not on IPv4 as well; the default `0.0.0.0` listens on IPv4.

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Clean shutdown, including Ctrl-C once the web server is running |
| 2 | Cannot start (port already in use, a host that does not resolve or an address that cannot be bound, web server failed to start, SANE could not be initialised, python-sane not installed, invalid config, or the job database is unreadable or has an unsupported schema) |
| 3 | Malformed Paperless URL |
| 5 | Unexpected error (a saneless bug; the traceback is in the stream, not a file -- `serve` writes none) |
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

Profiles are written to the config file that was loaded: the `--config` path, or else the first file found in the [config file search path](configuration.md#config-file-search-path). When no config file was loaded, they are written to `./saneless.toml` in the current directory. In the Docker image, whose working directory is `/var/lib/saneless`, that is `/var/lib/saneless/saneless.toml` -- in the durable data volume rather than the mounted `./config` directory, where it outlives the container and keeps loading ahead of any `config.toml` you add later -- so create `config/config.toml` on the host first (see [Deploy with Docker Compose](../how-to/deploy-docker-compose.md)). A config file created from scratch gets mode `0600`; rewriting an existing file keeps its permission bits, owner and group, each when the process is permitted to set it and the filesystem supports it.

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
