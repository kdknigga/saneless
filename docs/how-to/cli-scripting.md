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
    "created_at": "2026-03-22T14:30:00"
  }
]
```

## Exit codes

saneless uses distinct exit codes so scripts can handle different failure modes:

| Exit Code | Meaning | Example |
|---|---|---|
| 0 | Success | Scan completed and uploaded |
| 1 | Scan or runtime error | Scanner disconnected mid-scan, no pages scanned |
| 2 | Configuration or profile error | Unknown profile name, missing config file |
| 3 | Paperless upload error | paperless-ngx unreachable, invalid API token |

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
