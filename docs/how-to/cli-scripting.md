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

`saneless doctor` has no `--json` mode; it prints a table for a person to read, and its exit code says whether any check failed (see [`saneless doctor`'s exit code](#saneless-doctors-exit-code)). To check for a scanner before a scan, ask `saneless devices` instead (see [Checking readiness before a scan](#checking-readiness-before-a-scan)).

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
where a failed scan's PDF was kept.

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

All four are `null` when the job never recorded them, as for a failed, cancelled
or refused job. A measured zero is `0` and an empty list is `[]`, never `null`.

## Exit codes

A script that runs `saneless scan` needs to know which exit codes to treat differently. The
[basic scan example](#basic-scan-with-error-handling) branches on these codes and sends any
other code to a fallback that prints it. [CLI Commands](../reference/cli-commands.md#exit-codes)
defines every code, including 129 and 143, which the example leaves to its fallback, and 141,
which `saneless scan` never returns.

| Exit code | What a script should do |
|---|---|
| 0 | Nothing: the document was uploaded |
| 1 | Check that the scanner is switched on, connected and loaded, then scan again |
| 2 | Fix the configuration, the profile name or the job database first: scanning again unchanged fails the same way |
| 3 | Fix paperless-ngx or the connection to it (stderr says which), then scan again |
| 4 | Check that the output directory can be written, because the scanned pages could not be written as a PDF. A full disk is exit 10 |
| 5 | Report a bug and attach the log file |
| 6 | Do not rescan: the document was delivered to the consume folder. Fix the connection to paperless-ngx so the next scan keeps its title, tags and correspondent |
| 7 | Do not rescan the whole stack: the document was delivered. Check it in paperless-ngx; the warning is on stderr |
| 8 | Check the pages kept in `failed/` before scanning again: the scanner worked, but every page looked blank. If they are not blank, lower `empty_page_coverage_threshold` |
| 9 | Never rescan on 9: check paperless-ngx's document list first, and import the copy the error line names only if the document is not there |
| 10 | Free disk space on the server, then scan again |
| 130 | Nothing: someone cancelled the scan |

129 and 143 mean the command was stopped from outside, by a hangup or by SIGTERM. After either,
look for a kept file only where the `Interrupted:` line on stderr names one: a line that names no
path means nothing was kept. A signal that arrives once a scan's outcome is settled does not
change it, so the script sees that outcome's own code instead.

141 is 128 plus SIGPIPE, the shell's own code for a broken pipe. A pipeline reports the last
command's status unless `set -o pipefail` is set, so `saneless jobs --json | head` gives `head`'s
status, and the 141 shows only under `pipefail`.

A failure prints a line to stderr saying what failed, then a `Try:` line with the next step (a
configuration error prints a header naming the file and one line per problem, then the `Try:`
line). A script that parses the failure should read the first line: the `Try:` line is advice for
a person, and its wording may change. A cancel (130), an interruption (129, 143) and an
unexpected error (5) print one line and no `Try:` line, and a broken pipe (141) prints nothing.
[Troubleshoot a Failed Scan](troubleshoot-a-failed-scan.md) says what to check for each code.

### `saneless doctor`'s exit code

`saneless doctor` reports six health checks and collapses them to one code:

- **0** — every check came out OK, **or** came out as a warning. Most warnings are true
  statements about a deployment that still scans and files: no fallback folder is configured,
  or the generated profiles live only in memory. They are deliberately **not** failures, because
  a gate that goes red for tidiness is a gate people learn to ignore. One warning is different:
  a scanner host that does not answer, even when it is the only one configured. The check stops
  there without asking SANE for scanners, so it cannot say that none would be found.
- **2** — at least one check failed: scanner support is not installed or will not start, no
  usable scanner was found, the paperless-ngx API token is unset or rejected, paperless-ngx
  could not be used, no scan profiles are configured, or a folder saneless needs is not
  writable. A folder problem is a row like any other: `doctor` still prints all six rows, where
  the other commands refuse to start. A configuration file that cannot be loaded at all is
  also 2.
- **5**, **129**, **130** and **143** behave as they do for every other command.

So exit 0 means that nothing `doctor` could check is broken. It does not prove that a scan
will work, because the scanner may simply be switched off. `doctor` prints a human-readable
table and has no `--json` mode, so a script that runs it should read the exit code rather than
parse the output. Do not wire it to a container `HEALTHCHECK`: it probes
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
  4) echo "PDF assembly failed -- check the output directory" ;;
  5) echo "Unexpected error -- see the log file and report a bug" ;;
  6) echo "Saved to the consume folder without its title, tags or correspondent -- do not rescan; fix the paperless-ngx connection" ;;
  7) echo "Uploaded with a warning -- check the document in paperless-ngx (see stderr)" ;;
  8) echo "Every page looked blank -- check the pages kept in failed/ before scanning again" ;;
  9) echo "May already be in paperless-ngx -- do not rescan; check its document list first" ;;
  10) echo "Out of disk space -- free space on the server, then scan again" ;;
  130) echo "Cancelled" ;;
  *) echo "saneless exited with code $exit_code -- see stderr" ;;
esac
exit $exit_code
```

Only exit 0 is a clean success. Exits 6 and 7 both mean the document was delivered, and 9 means
it may have been, so the script must not scan the stack again on any of them. Neither 6 nor 7
is a success to ignore: 6 means every scan is going to the consume folder until the connection
to paperless-ngx is fixed, and 7 means a document needs checking. `saneless scan` is not recorded in the job history, so its
exit code and stderr are the only report a script gets.

### Checking readiness before a scan

`saneless doctor`'s exit code is not a readiness check: a scanner that is switched off is a
warning, and `doctor` exits 0. Gate a scan on the scanner itself instead, by asking which
devices SANE can see:

```bash
#!/bin/bash
if ! saneless devices --json | jq -e 'length > 0' >/dev/null; then
  echo "No scanner found -- run saneless doctor to see why"
  exit 1
fi
saneless scan --profile adf --title "Batch $(date +%Y-%m-%d)"
```

`jq -e` exits non-zero when the list is empty, and also when `saneless devices` failed and
printed nothing. When it does, run `saneless doctor` by hand: its rows say what is wrong.

### Scan only if scanner is available

```bash
#!/bin/bash
if saneless devices --json | jq -e 'length > 0' >/dev/null; then
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
0 9 * * 1-5 $HOME/.local/bin/saneless --config $HOME/.config/saneless/saneless.toml scan --profile adf --title "Morning batch $(date +\%Y-\%m-\%d)" >> $HOME/saneless-cron.log 2>&1
```

Cron runs a job with almost none of your login environment. Its `PATH` is `/usr/bin:/bin`, so
a `saneless` that `pipx` installed in `~/.local/bin` is not found by name: give the full path.
The job also starts in your home directory, so a `./saneless.toml` in the directory you
usually scan from is not the file it loads, and an `XDG_CONFIG_HOME` your shell sets is not set.
`--config` names the file, and it goes before the subcommand because it is an option of
`saneless` itself, not of `scan`. `%` is special in a crontab line, so the date format escapes
it as `\%`.

## Configuration in scripts and containers

A set `SANELESS_*` environment variable overrides the same setting in `saneless.toml`; see
[Where saneless reads settings](../reference/configuration.md#where-saneless-reads-settings).
In a script or a container, keep the paperless-ngx URL and API token in `saneless.toml`
rather than in the environment, so there is one place to change them and nothing overrides
the file without saying so. The variable a script or container usually sets is the scanner
host, which differs from one machine to the next:

```bash
export SANELESS_SCANNER__HOST="192.168.1.50"

saneless scan --profile default --title "Scripted scan"
```

## Regenerating profiles

If your scanner's capabilities change (e.g., after a firmware update or switching scanners), regenerate auto-profiles:

```bash
saneless auto-profiles --force
```

This refreshes the generated keys of profiles marked `auto_generated = true` from the scanner's current capabilities, and keeps their other keys, such as `default_tags` and `title`. Profiles you wrote yourself are never changed, even when one has the same name as a generated profile; it is reported as skipped. See [Auto-generated profiles](configure-scan-profiles.md#auto-generated-profiles).
