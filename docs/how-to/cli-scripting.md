# Use the CLI for Scripting

Automate scanning workflows with saneless CLI commands, JSON output, and predictable exit codes.

## What you'll need

- saneless installed ([Install on Bare Metal](install-bare-metal.md) or [Deploy with Docker Compose](deploy-docker-compose.md))
- Familiarity with shell scripting (Bash or similar)

## JSON output mode

All read commands support `--json` for machine-parseable output:

```bash
saneless devices --json
saneless jobs --json --limit 10
```

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

### Job history JSON

```bash
saneless jobs --json --limit 5
```

```json
[
  {
    "id": "a1b2c3d4",
    "profile": "default",
    "title": "Invoice March 2026",
    "state": "DONE",
    "created_at": "2026-03-22T14:30:00",
    "outcome": "SUCCESS",
    "warning": null
  }
]
```

`state` is always the raw uppercase enum value — `PENDING`, `SCANNING`,
`AWAITING_FLIP`, `SCANNING_REVERSE`, `ASSEMBLING`, `UPLOADING`, `DONE`, `ERROR`
or `FALLBACK` — so it is safe to compare against in a script. `AWAITING_FLIP`
and `SCANNING_REVERSE` only occur in manual duplex jobs: the wait for the
operator to flip the stack, and the second pass over the back sides. The human-readable labels the table
view prints ("Complete", "Failed", "Saved to folder") never appear in `--json`.

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
| 1 | Scan or runtime error | Scanner disconnected mid-scan, no pages scanned |
| 2 | Configuration or profile error | Unknown profile name, missing config file, a manual duplex profile run without an interactive terminal |
| 3 | Paperless upload error | paperless-ngx unreachable, invalid API token |

!!! warning "Manual duplex profiles cannot be scripted"
    A profile with `duplex = "manual"` needs a person to flip the stack between the two passes,
    and `saneless scan` asks for that confirmation at the terminal. When stdin is not a terminal
    -- cron, a pipe, redirected input, most CI runners -- `saneless scan` refuses the profile
    and exits with code 2 before the scanner feeds a single page, so no stack is half-scanned:

    ```
    Profile 'manual-duplex' is manual duplex, which needs an interactive terminal: saneless must prompt you to flip the stack between the two passes. Run it from a terminal, or scan from the web UI.
    ```

    Use a simplex or hardware duplex profile for automation. From a terminal, a manual duplex
    scan that is aborted at the flip prompt or not confirmed within `flip_timeout_seconds` exits
    with code 1. See [Set Up ADF Duplex Scanning](set-up-adf-duplex.md#manual-duplex).

## Scripting examples

### Basic scan with error handling

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
  esac
  exit $exit_code
fi
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

This overwrites existing auto-generated profiles with fresh profiles based on the scanner's current capabilities. Manually-created profiles are not affected unless they have the same name as an auto-generated one.
