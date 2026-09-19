# Use the CLI for Scripting

Automate scanning workflows with saneless CLI commands, JSON output, and predictable exit codes.

## What you'll need

- saneless installed ([Install on Bare Metal](install-bare-metal.md) or [Deploy with Docker Compose](deploy-docker-compose.md))
- Familiarity with shell scripting (Bash or similar)

## JSON output mode

`saneless devices` and `saneless jobs` support `--json` for machine-parseable output:

```bash
saneless devices --json
saneless jobs --json --limit 10
```

`saneless doctor` has no `--json` mode; it prints a table for a person to read, and scripts gate on its exit code instead (see [Checking readiness before a scan](#checking-readiness-before-a-scan)).

### Device list JSON

```bash
saneless devices --json
```

```json
[
  {
    "name": "net:192.168.1.50:pixma:MF740C",
    "vendor": "Canon",
    "model": "MF740C Series",
    "type": "multi-function peripheral"
  }
]
```

These four keys, in this order, are the whole object unless you add `--capabilities`. Only the JSON document is written to stdout; status lines and errors go to stderr, so you can pipe the output straight into `jq`.

With `--capabilities`, each device also carries a `capabilities` object:

```bash
saneless devices --json --capabilities | jq '.[] | {name, sources: .capabilities.sources}'
```

```json
[
  {
    "name": "net:192.168.1.50:pixma:MF740C",
    "vendor": "Canon",
    "model": "MF740C Series",
    "type": "multi-function peripheral",
    "capabilities": {
      "sources": ["Flatbed", "ADF Simplex", "ADF Duplex"],
      "resolutions": [150, 300, 600],
      "modes": ["Color", "Gray", "Lineart"],
      "raw_options": ["source", "mode", "resolution"]
    }
  }
]
```

A key appears only when the device reported something for it. A device that gives its resolution as a range has `"resolution_range": {"min": ..., "max": ..., "step": ...}` instead of `resolutions`.

If one device's capabilities cannot be read, that device gets `"capabilities": null` and a one-line `"capabilities_error"` string. The other devices are still reported and stdout is still valid JSON, but the command exits 1, so check the exit code as well as the document:

```bash
if ! caps=$(saneless devices --json --capabilities); then
  echo "$caps" | jq -r '.[] | select(.capabilities == null) | "\(.name): \(.capabilities_error)"' >&2
fi
```

### Job history JSON

```bash
saneless jobs --json --limit 5
```

```json
[
  {
    "id": "8eae6099-6b24-4b69-a339-8daa8e6c9a5c",
    "profile": "default",
    "title": "Invoice March 2026",
    "state": "DONE",
    "created_at": "2026-03-22T14:30:00+00:00",
    "outcome": "SUCCESS",
    "warning": null
  }
]
```

`state` is always the raw uppercase enum value — `PENDING`, `SCANNING`,
`AWAITING_FLIP`, `SCANNING_REVERSE`, `ASSEMBLING`, `UPLOADING`, `DONE`, `ERROR`,
`FALLBACK` or `CANCELLED` — so it is safe to compare against in a script. `AWAITING_FLIP`
and `SCANNING_REVERSE` only occur in manual duplex jobs: the wait for the
operator to flip the stack, and the second pass over the back sides. `CANCELLED`
is a scan the operator stopped at the flip prompt, not a failure. The human-readable labels the table
view prints ("Complete", "Failed", "Saved to folder", "Cancelled") never appear in `--json`.

`created_at` is always a UTC ISO-8601 timestamp carrying the `+00:00` offset, whatever timezone the server is in, so a script can parse it without knowing where the appliance lives. The table `saneless jobs` prints without `--json` is the other way round: it renders the same instant in the server's local timezone with the zone named, for example `2026-03-22 09:30 CDT`. Only the table follows the server's timezone; the JSON never does.

`outcome` is `"SUCCESS"`, `"FALLBACK"` or `null`, and `warning` carries a note
about something odd that did not fail the scan, or `null`. A job whose `state`
is `FALLBACK` was scanned and saved, but paperless-ngx did not accept it over
the API, so the PDF went to the consume directory and its title, tags and
correspondent were not applied.

## Exit codes

saneless uses distinct exit codes so scripts can handle different failure modes:

| Exit Code | Meaning | Example |
|---|---|---|
| 0 | Success | Scan completed and uploaded |
| 1 | Scan error | Scanner disconnected mid-scan, empty feeder, no pages scanned, flip wait timed out |
| 2 | Configuration, profile or setup error | Unknown profile name, a `--config` file that does not exist, a TOML syntax error, an unknown config key or `SANELESS_*` variable, a manual duplex profile run without an interactive terminal, python-sane not installed, a job database saneless cannot use (unreadable, or an unsupported schema) |
| 3 | Paperless upload error | paperless-ngx unreachable, invalid API token, a malformed `paperless.url` |
| 4 | PDF assembly error | Disk full while writing the PDF, unwritable output directory |
| 5 | Unexpected error (a saneless bug) | Prints one line; the traceback is in the log file -- attach it to a bug report |
| 130 | Cancelled by the operator | Answered no, Ctrl-D or Ctrl-C at the flip prompt; Ctrl-C during a one-shot command |

Every failure prints one line to stderr (a configuration error prints a header naming the file,
then one line per problem). [Troubleshoot a Failed Scan](troubleshoot-a-failed-scan.md) explains
what each code means and what to check.

### `saneless doctor`'s exit code

`saneless doctor` reports five health checks and collapses them to one code:

- **0** — every check came out OK, **or** came out as a warning. A warning is a true statement
  about a deployment that still scans and files: no fallback folder is configured, or the
  generated profiles live only in memory. It is deliberately **not** a failure, because a gate
  that goes red for tidiness is a gate people learn to ignore.
- **2** — at least one check failed: scanner support is not installed, no scanner is reachable,
  the paperless-ngx API token is unset or rejected, no scan profiles are configured, or a folder
  saneless needs is not writable.
- **5** and **130** behave as they do for every other command.

`doctor` prints a human-readable table and has no `--json` mode, so a script should gate on the
exit code rather than parse the output. Do not wire it to a container `HEALTHCHECK`: it probes
the scanner and talks to paperless-ngx, so it would mark the container unhealthy during a routine
paperless-ngx restart. Use the web server's `/health` endpoint for that.

!!! warning "Manual duplex profiles cannot be scripted"
    A profile with `duplex = "manual"` needs a person to flip the stack between the two passes,
    and `saneless scan` asks for that confirmation at the terminal. When stdin is not a terminal
    -- cron, a pipe, redirected input, most CI runners -- `saneless scan` refuses the profile
    and exits with code 2 before the scanner feeds a single page, so no stack is half-scanned:

    ```
    Profile 'manual-duplex' is manual duplex, which needs an interactive terminal: saneless must prompt you to flip the stack between the two passes. Run it from a terminal, or scan from the web UI.
    ```

    Use a simplex or hardware duplex profile for automation. From a terminal, answering no,
    Ctrl-D or Ctrl-C at the flip prompt cancels the scan and exits with code 130. A flip wait
    that is not confirmed within `flip_timeout_seconds`, or a read error at the prompt (an I/O
    error, or input that cannot be decoded), fails the scan with code 1. See
    [Set Up ADF Duplex Scanning](set-up-adf-duplex.md#manual-duplex).

## Scripting examples

### Basic scan with error handling

`--title` may be omitted, in which case the profile's `title` is used; with neither, the title is the scan's start time in the server's local timezone with the zone named, for example `Scan 2026-03-22 09:30 CDT`. So a cron entry can rely on a profile title instead of building one.

```bash
#!/bin/bash
if saneless scan --profile default --title "Automated Scan"; then
  echo "Scan uploaded successfully"
else
  exit_code=$?
  case $exit_code in
    1) echo "Scan failed -- check scanner connection" ;;
    2) echo "Configuration error -- check profile name" ;;
    3) echo "Upload failed -- check paperless-ngx connection" ;;
    4) echo "PDF assembly failed -- check disk space and the output directory" ;;
    5) echo "Unexpected error -- see the log file and report a bug" ;;
    130) echo "Cancelled" ;;
  esac
  exit $exit_code
fi
```

### Checking readiness before a scan

`saneless doctor` exits 0 while every check is OK or a warning, so it reads as a plain condition:

```bash
#!/bin/bash
if ! saneless doctor; then
  echo "saneless is not ready to scan -- see the failed rows above"
  exit 2
fi
saneless scan --profile adf --title "Batch $(date +%Y-%m-%d)"
```

### Scan only if scanner is available

```bash
#!/bin/bash
if saneless devices --json | grep -q '"name"'; then
  saneless scan --profile adf --title "Batch $(date +%Y-%m-%d)"
else
  echo "No scanner found, skipping"
fi
```

### Scheduled scanning with cron

Add a cron job to scan at a specific time (useful for shared office scanners with a known document tray):

```bash
# Scan every weekday at 9:00 AM
0 9 * * 1-5 saneless scan --profile adf --title "Morning batch $(date +\%Y-\%m-\%d)"
```

## Configuration via environment variables

For containerized or automated environments, configure saneless entirely through environment variables using the `SANELESS_` prefix with `__` as the nested delimiter:

```bash
export SANELESS_PAPERLESS__URL="http://paperless:8000"
export SANELESS_PAPERLESS__TOKEN="your-api-token"
export SANELESS_SCANNER__HOST="192.168.1.50"

saneless scan --profile default --title "Scripted scan"
```

This is particularly useful in CI/CD pipelines or Docker containers where config files are impractical.

## Regenerating profiles

If your scanner's capabilities change (e.g., after a firmware update or switching scanners), regenerate auto-profiles:

```bash
saneless auto-profiles --force
```

This refreshes the generated keys of profiles marked `auto_generated = true` from the scanner's current capabilities, and keeps their other keys, such as `default_tags` and `title`. Profiles you wrote yourself are never changed, even when one has the same name as a generated profile; it is reported as skipped. See [Auto-generated profiles](configure-scan-profiles.md#auto-generated-profiles).
