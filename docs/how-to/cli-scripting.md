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
saneless devices --json --capabilities
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

To pick out one field per device, filter it with `jq`:

```bash
saneless devices --json --capabilities | jq -c '.[] | {name, sources: .capabilities.sources}'
```

```json
{"name":"net:192.168.1.50:pixma:MF740C","sources":["Flatbed","ADF Simplex","ADF Duplex"]}
```

A key appears only when the device reported something for it. A device that gives its resolution as a range has `"resolution_range": {"min": ..., "max": ..., "step": ...}` instead of `resolutions`.

If one device's capabilities cannot be read, that device gets `"capabilities": null` and a one-line `"capabilities_error"` string. The other devices are still reported and stdout is still valid JSON, but the command exits 1, so check the exit code as well as the document:

```bash
if ! caps=$(saneless devices --json --capabilities); then
  echo "$caps" | jq -r '.[] | select(.capabilities == null) | "\(.name): \(.capabilities_error)"' >&2
fi
```

### Job history JSON

`saneless jobs` lists the scans the web UI ran, from the job database the server keeps. A
`saneless scan` run is not recorded there: it reports through its own output and exit code (see
[Exit codes](#exit-codes)), so a script that runs `saneless scan` should check that, not
`saneless jobs`.

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
    "warning": null,
    "error": null,
    "pages_scanned": 4,
    "pages_removed": 2,
    "pages_uploaded": 2,
    "pages_removed_positions": [2, 4]
  }
]
```

`state` is always the raw uppercase enum value — `PENDING`, `SCANNING`,
`AWAITING_FLIP`, `AWAITING_NEXT_PASS`, `AWAITING_BLANK_DECISION`, `AWAITING_RETRY`,
`SCANNING_REVERSE`, `ASSEMBLING`, `UPLOADING`, `DONE`, `ERROR`, `FALLBACK` or
`CANCELLED` — so it is safe to compare against in a script. `AWAITING_FLIP`
and `SCANNING_REVERSE` only occur in manual duplex jobs: the wait for the
operator to flip the stack, and the second pass over the back sides.
`AWAITING_NEXT_PASS`, `AWAITING_BLANK_DECISION` and `AWAITING_RETRY` only occur in
[multi-page](scan-a-multi-page-document.md) jobs, while the job waits for the
operator's answer: whether there is another page, what to do about pages that
look blank, and what to do after a scan that failed. Like `AWAITING_FLIP`, they
are not finished, but nothing happens in them until someone answers or the wait
runs out. `CANCELLED` is a scan the operator stopped at the flip prompt or with
Abort at a multi-page question, not a failure. The human-readable labels the table
view prints ("Complete", "Failed", "Saved to folder", "Cancelled", "Waiting for more
pages") never appear in `--json`.

`created_at` is always a UTC ISO-8601 timestamp carrying the `+00:00` offset, whatever timezone the server is in, so a script can parse it without knowing where the appliance lives. The table `saneless jobs` prints without `--json` is the other way round: it renders the same instant in the server's local timezone with the zone named, for example `2026-03-22 09:30 CDT`. Only the table follows the server's timezone; the JSON never does.

`outcome` is `"SUCCESS"`, `"FALLBACK"` or `null`, and `warning` carries a note
about something odd that did not fail the scan, or `null`. A job whose `state`
is `FALLBACK` was scanned and saved, but paperless-ngx did not accept it over
the API, so the PDF went to the consume directory and its title, tags and
correspondent were not applied. A job whose `state` is `DONE` and whose
`warning` is set was uploaded with a warning -- a sheet the scanner skipped, or
a manual duplex scan uploaded as two documents -- and the table view labels it
"Uploaded with a warning" rather than "Complete".

`error` holds the full stored text of what stopped a job -- a failure, a
refused submit or a cancellation -- or `null`. It includes any file path on the
server and the paperless-ngx URL. The web page shows the same failure only as a sentence with
no paths in it, which points here: `saneless jobs --json` is where to find
where a failed scan's PDF was kept. `error` was added after the other keys, so
a script that reads only those is unaffected.

`pages_scanned`, `pages_removed` and `pages_uploaded` are the page counts the
web page shows under a finished scan: how many pages the scanner produced, how
many were removed as blank, and how many went to paperless-ngx.
`pages_removed_positions` lists the pages that were removed as blank, by
their scanned page number in document order, so `[2, 4]` with
`pages_scanned` 4 means the second and fourth scanned pages. For a duplex scan
that is the page number in the interleaved document. Removed pages are not
kept, so these numbers are how you find the sheets to rescan if a real page
was taken for a blank one. The list is information, not a warning: a `DONE`
job that removed blank pages keeps `"warning": null`.

All four are `null` when the job never recorded them: a failed, cancelled or
refused job, or one that ran before saneless recorded them. A measured zero is
`0` and an empty list is `[]`, never `null`. The four keys come after all
the others, so a script that reads only the earlier keys is unaffected.

## Exit codes

saneless uses distinct exit codes so scripts can handle different failure modes:

| Exit Code | Meaning | Example |
|---|---|---|
| 0 | Success | Scan completed and uploaded |
| 1 | Scan error | No scanner found, scanner disconnected mid-scan, empty feeder, no pages scanned, flip wait timed out |
| 2 | Configuration, profile or setup error | Unknown profile name, a `--config` file that does not exist, a TOML syntax error, an unknown config key or `SANELESS_*` variable, a manual duplex profile run without an interactive terminal, `saneless scan --multi-page` without an interactive terminal or with a manual duplex profile, python-sane not installed, a job database saneless cannot use (unreadable, or an unsupported schema), a malformed `paperless.url` or `paperless.token` (refused when the config loads), or `paperless.url` not set (a scan is refused before the scanner is opened) |
| 3 | Paperless upload error | paperless-ngx unreachable, invalid API token |
| 4 | PDF assembly error | The scanned pages could not be written as a PDF: a page image the PDF library refused, or an unwritable output directory. A full disk is exit 10 |
| 5 | Unexpected error (a saneless bug) | Prints one line; the traceback is in the log file -- attach it to a bug report |
| 6 | Saved to the consume folder | paperless-ngx could not be reached, so the PDF went to the consume folder without its title, tags or correspondent; stdout reads `Saved to folder: <title>`. The document was delivered: do not rescan |
| 7 | Uploaded with a warning | A sheet the scanner skipped, manual-duplex front and back counts that differed (uploaded as two documents), or a multi-page document finished because nobody answered in time or it reached the page limit; stdout reads `Uploaded with a warning: <title>` and the warning is on stderr. The document was delivered: do not rescan the whole stack |
| 8 | Every page looked blank | Empty-page detection judged every page blank, so nothing was uploaded; the pages were kept in `failed/`, normally as one PDF, and stderr names what was kept. The scanner worked: lower `empty_page_coverage_threshold` or turn detection off if the pages are not blank |
| 9 | The document may already be in paperless-ngx | The upload was sent but no answer came back, or paperless-ngx received the document but did not confirm filing it (a long OCR can outlast `paperless_task_timeout`). Never rescan on 9: check paperless-ngx's document list first. A copy is kept in `failed/`; import it only if the document is not in paperless-ngx |
| 10 | Out of disk space | The server ran out of disk space while scanning or assembling the PDF; the error line names the folder, and how much space is needed when saneless found the shortfall before writing. Free space, then scan again |
| 129 | Interrupted by SIGHUP | The terminal or SSH session running the command went away. Pages a scan already had were kept in `failed/` when they could be; the `Interrupted:` line says what was kept, or where the pages were left |
| 130 | Cancelled by the operator | Answered no, Ctrl-D or Ctrl-C at the flip prompt; a confirmed abort, Ctrl-D or Ctrl-C at a multi-page question; Ctrl-C during a one-shot command |
| 143 | Interrupted by SIGTERM | `kill`, a service manager or a container runtime stopped the command. Pages a scan already had were kept in `failed/` when they could be; the `Interrupted:` line says what was kept, or where the pages were left |

130 means someone chose to stop, so nothing was kept. 129 and 143 (128 plus the signal number)
mean the command was stopped from outside without anyone choosing to discard the scan, so a scan
that had pages keeps them in `failed/` when it can, and its `Interrupted:` line on stderr names
the path, or says where the pages were left when they could not be kept. No
path on that line means nothing was kept: the command was not a scan, or the scan was stopped
before its first page. So after 129 or 143, look for a kept file only where that line names one.
A signal that arrives once a scan's outcome is settled -- the document delivered, or a failure's
pages already being kept -- does not change it: the command exits with that outcome's own code.
`saneless serve` is the exception: it keeps the web server's own signal handling, where SIGTERM is
a graceful stop.

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
- **5**, **129**, **130** and **143** behave as they do for every other command.

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
    that is not confirmed within `operator_wait_timeout_seconds`, or a read error at the
    prompt (an I/O error, or input that cannot be decoded), fails the scan with code 1. See
    [Set Up ADF Duplex Scanning](set-up-adf-duplex.md#manual-duplex).

!!! warning "`--multi-page` cannot be scripted either"
    `saneless scan --multi-page` asks after every scan whether there is another page, so it is
    interactive only. When stdin is not a terminal it is refused with exit code 2 before the
    scanner is opened:

    ```
    --multi-page needs an interactive terminal: saneless asks after each scan whether there is another page. Run it from a terminal, or scan from the web UI.
    ```

    There is no scripted form with a fixed number of pages. See
    [Scan a Multi-Page Document](scan-a-multi-page-document.md#scan-from-the-command-line).

## Scripting examples

### Basic scan with error handling

`--title` may be omitted, in which case the profile's `title` is used; with neither, the title is the scan's start time in the server's local timezone with the zone named, for example `Scan 2026-03-22 09:30 CDT`. So a cron entry can rely on a profile title instead of building one.

```bash
#!/bin/bash
saneless scan --profile default --title "Automated Scan"
exit_code=$?
case $exit_code in
  0) echo "Scan uploaded successfully" ;;
  1) echo "Scan failed -- check the scanner is switched on and connected" ;;
  2) echo "Configuration error -- check the profile name and the config" ;;
  3) echo "Upload failed -- check paperless-ngx connection" ;;
  4) echo "PDF assembly failed -- check disk space and the output directory" ;;
  5) echo "Unexpected error -- see the log file and report a bug" ;;
  6) echo "Saved to the consume folder without its title, tags or correspondent -- do not rescan; fix the paperless-ngx connection" ;;
  7) echo "Uploaded with a warning -- check the document in paperless-ngx (see stderr)" ;;
  130) echo "Cancelled" ;;
esac
exit $exit_code
```

Only exit 0 is a clean success. Exits 6 and 7 both mean the document was delivered, so the
script must not scan the stack again on either, but neither is a success to ignore: 6 means
every scan is going to the consume folder until the connection to paperless-ngx is fixed, and
7 means a document needs checking. `saneless scan` is not recorded in the job history, so its
exit code and stderr are the only report a script gets.

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

Without the check, a scan that finds no scanner exits 1, the same code as any other scanner
failure, and so does `saneless auto-profiles`. The first stderr line then starts
`Scan error: No scanner found`, so a script that must tell the two apart can match on it:

```bash
#!/bin/bash
saneless scan --profile adf --title "Batch $(date +%Y-%m-%d)" 2>scan.err
exit_code=$?
if [ "$exit_code" -eq 1 ] && grep -q '^Scan error: No scanner found' scan.err; then
  echo "No scanner found, skipping"
  exit 0
fi
exit $exit_code
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
