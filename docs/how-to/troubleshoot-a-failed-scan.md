# Troubleshoot a Failed Scan

Find out why a saneless command failed, starting from its exit code, and what to check next.

When a command fails it prints one line to stderr and exits with a non-zero code. The web UI shows
the same failure on the job. The exact wording of the messages may change between releases, so
this page describes what you see rather than quoting each message in full.

## Start with the exit code

In a shell, `echo $?` right after the command prints its exit code.

| Exit code | What happened | Where to look |
|---|---|---|
| 0 | The command succeeded | -- |
| 1 | The scanner failed, or the scan produced no usable pages | [Scanner errors](#scanner-errors-exit-1) |
| 2 | saneless could not start: configuration, profile or setup | [Configuration errors](#configuration-errors-exit-2), [python-sane is not installed](#python-sane-is-not-installed-exit-2) |
| 3 | paperless-ngx could not be reached or rejected the upload | [Paperless errors](#paperless-errors-exit-3) |
| 4 | The scanned pages could not be written as a PDF | [PDF assembly errors](#pdf-assembly-errors-exit-4) |
| 5 | An error saneless did not anticipate: a bug | [Unexpected errors](#unexpected-errors-exit-5) |
| 130 | You cancelled the scan | [Cancelled scans](#cancelled-scans-exit-130) |

[Use the CLI for Scripting](cli-scripting.md#exit-codes) shows how to branch on these codes in a
script, and [CLI Commands](../reference/cli-commands.md#exit-codes) lists the codes each command
can return.

## Scanner errors (exit 1)

The line starts with `Scan error:`. It says what saneless was doing and names the device, then
gives the scanner's own words. Those last words come from the scanner driver, so they are the part
to search for in your scanner's documentation.

What the common cases mean:

- **No paper detected in the feeder.** The document feeder was empty when the scan started, or
  the source is set to the feeder on a scanner that has nothing loaded. Load the stack and scan
  again, or pick a flatbed profile.
- **Every sheet fed was unreadable.** The feeder pulled paper, but the scanner returned no page
  saneless could use. Check for a jam, a misfeed or a dirty feeder, then scan again.
- **No pages were scanned.** The scanner finished without returning any page at all. Check that
  the profile's source matches where the paper is.
- **All pages were blank.** Pages were scanned, but empty-page detection removed every one of
  them. If the pages were not blank, make detection more conservative or turn it off; see
  [When Every Page Is Blank](../explanation/empty-page-detection.md#when-every-page-is-blank).
- **The flip wait timed out.** A manual duplex scan waited `flip_timeout_seconds` for someone to
  flip the stack and nobody answered. A timeout is a failure, not a cancel. See
  [Manual Duplex](set-up-adf-duplex.md#manual-duplex).
- **The flip prompt failed.** The terminal broke while `saneless scan` was asking you to flip the
  stack. The cause is logged with its traceback.

To check that saneless can see the scanner at all, run:

```bash
saneless devices
```

If the scanner is missing from that list, or saneless runs in a container, work through
[Scanner Host Discovery](scanner-host-discovery.md).

## Configuration errors (exit 2)

A problem with the config file prints a header naming the file, then one line per problem. A TOML
syntax error names the line and column where parsing stopped. Fix each line listed and run the
command again; [Validation](../reference/configuration.md#validation) describes the rules.

Other causes of exit 2, each on one line:

- **Unknown profile.** The `--profile` name is not a profile in the loaded config. Check the
  spelling against the `[profiles.NAME]` tables.
- **Manual duplex without a terminal.** A profile with `duplex = "manual"` needs someone to flip
  the stack, so `saneless scan` refuses it when stdin is not a terminal (cron, a pipe, CI). Run it
  from a terminal or scan from the web UI.
- **No scanner found.** `scanner.device` is empty and saneless discovered no scanner to use. Set
  `scanner.device`, or fix discovery with `saneless devices`.
- **`serve` cannot start.** The port is already in use, or the web server failed to start. Stop
  whatever holds the port or pass `--port`; when the web server itself failed, the cause is in the
  log file.
- **The job database cannot be used.** The line starts with `Job database error:` and names the
  database path and the reason: the file cannot be opened or is not a SQLite database, or its jobs
  table has a shape this version of saneless does not recognise. Check that the path is right,
  that saneless can read and write it and its directory, and that the file really is saneless's
  job database. If it is damaged, move it aside: saneless then starts with an empty job history,
  and the moved file is kept for inspection or restoring.

## python-sane is not installed (exit 2)

saneless needs python-sane, which in turn needs the SANE library. When it cannot be imported,
`scan`, `devices`, `auto-profiles` and `serve` each refuse at once, before touching the config or
the scanner. The line gives the import's own reason and names the package to install.

1. Install the SANE development package: `libsane-dev` on Debian or Ubuntu,
   `sane-backends-devel` on Fedora, RHEL or Rocky.
2. Reinstall saneless so python-sane is built against it.

`saneless jobs` and `--help` on any command do not need python-sane and keep working. See
[Install on Bare Metal](install-bare-metal.md#step-1-install-sane-development-headers).

## Paperless errors (exit 3)

The line starts with `Paperless error:`.

- **Unreachable.** saneless tries the upload three times, with a growing pause between attempts,
  when the connection is refused or reset, times out, is closed by a reverse proxy, or
  paperless-ngx answers with a server error. If every attempt fails and a consume directory is
  configured, the PDF is saved there instead. Without one, the scan fails. Check that
  `paperless.url` is reachable from where saneless runs.
- **Malformed URL.** A `paperless.url` without a usable `http://` or `https://` scheme is not
  retried, because retrying cannot help. With a consume directory configured the scan is still
  saved there, so it does not fail, but every scan goes to the folder and the log says the URL
  cannot be used. Fix the URL in the config.
- **Upload rejected.** paperless-ngx answered with a 4xx, such as a bad API token or a field it
  refuses. The line gives Paperless's own reason. A rejected upload is never retried and never
  falls back to the consume directory; fix what the reason names and scan again.
- **Upload redirected.** paperless-ngx, or a proxy in front of it, answered with a redirect. The
  line names where the upload was sent. It is not retried and never falls back; set
  `paperless.url` to the base of that address (often the `https://` form of the same host) and
  scan again.
- **The document may already be in Paperless.** A retry after a lost response can reach
  paperless-ngx twice. On default paperless-ngx settings that stores a second copy you can delete;
  when paperless-ngx rejects duplicates, the failure says so. Check paperless-ngx before scanning
  again.

When the upload fails, the assembled PDF is kept and its path is added to the error.
[How Consume Directory Fallback Works](../explanation/consume-directory-fallback.md) explains
which failures retry, when the consume directory is used, and where a kept PDF goes.

## PDF assembly errors (exit 4)

The line starts with `PDF error:`. saneless scanned the pages but could not write them as a PDF.

- Check the free disk space where saneless writes its working files (`tmp_dir` in
  [`[output]`](../reference/configuration.md#output)).
- Check that saneless can write to that directory.
- If both are fine, the PDF library refused one of the scanned images, and the line gives its
  reason. Try the scan again with a different `mode` or `resolution` in the profile.

The scanned pages are only held in memory while the PDF is built, so they are not kept when this
fails: scan the document again once the cause is fixed.

## Cancelled scans (exit 130)

Exit 130 means the scan was stopped on purpose, not that something broke:

- You answered no, pressed Ctrl-D or pressed Ctrl-C at the manual duplex flip prompt.
- You pressed Ctrl-C while a one-shot command (`scan`, `devices`, `auto-profiles`, `jobs`) was
  running.

Nothing is uploaded. In the web UI, a scan cancelled with **Abort scan** at the flip step is shown
as Cancelled, in grey rather than as an error. A flip wait that times out is not a cancel: it
fails with exit 1. Ctrl-C on `saneless serve` is a normal stop and exits 0.

## Unexpected errors (exit 5)

Exit 5 means saneless hit an error it did not anticipate: a bug in saneless, not a problem with
your setup. Setup problems exit 2.

The line starts with `Unexpected error` and names the exception type. The full traceback is in
the log file, which the line names. If the line names no log file (the error happened before
logging started, or the log file could not be opened), it ends with a hint instead: run the
command again with `-v` to see the traceback on stderr.

Please report it as a bug and attach the log file. Attach the log only, never your config file,
which holds your Paperless API token. If the log was recorded with `log_level = "DEBUG"`, search it
for your token before sharing it: debug output from the HTTP client can include the authorization
header.
