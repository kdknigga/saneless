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
| 2 | saneless could not start: configuration, profile or setup; or `paperless.url` is not set when a scan is started | [Configuration errors](#configuration-errors-exit-2), [python-sane is not installed](#python-sane-is-not-installed-exit-2) |
| 3 | paperless-ngx could not be reached or rejected the upload | [Paperless errors](#paperless-errors-exit-3) |
| 4 | The scanned pages could not be written as a PDF | [PDF assembly errors](#pdf-assembly-errors-exit-4) |
| 5 | An error saneless did not anticipate: a bug | [Unexpected errors](#unexpected-errors-exit-5) |
| 6 | The scan was saved to the consume folder, not uploaded: its title, tags and correspondent were not applied | [Saved to the consume folder](#saved-to-the-consume-folder-exit-6) |
| 7 | The scan was uploaded, but with a warning | [Uploaded with a warning](#uploaded-with-a-warning-exit-7) |
| 8 | Every page looked blank to empty-page detection, so nothing was uploaded | [Every page looked blank](#every-page-looked-blank-exit-8) |
| 129 | A SIGHUP interrupted the command, for example a dropped SSH session | [Interrupted by a signal](#interrupted-by-a-signal-exit-129-and-143) |
| 130 | You cancelled the scan | [Cancelled scans](#cancelled-scans-exit-130) |
| 143 | A SIGTERM interrupted the command | [Interrupted by a signal](#interrupted-by-a-signal-exit-129-and-143) |

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
  the profile's source matches where the paper is. In a manual duplex scan, an empty second pass
  says so and names how many front pages the first pass scanned; reload the flipped stack and
  scan both sides again.
- **Every page looked blank.** This is not a scanner error and does not exit 1: the scanner
  returned pages and empty-page detection removed all of them. It exits 8; see
  [Every page looked blank](#every-page-looked-blank-exit-8).
- **The flip wait timed out.** A manual duplex scan waited `flip_timeout_seconds` for someone to
  flip the stack and nobody answered. A timeout is a failure, not a cancel. The front sides the
  first pass already scanned are kept: saneless assembles them into a PDF under `failed/` in its
  data directory and names the path in the error. See
  [Manual Duplex](set-up-adf-duplex.md#manual-duplex).
- **The flip prompt failed.** Reading your answer failed while `saneless scan` was asking you to
  flip the stack, for example with an I/O error or input that could not be decoded. The cause is
  logged with its traceback, and the fronts are kept the same way a flip timeout keeps them. End
  of input is not a failure: Ctrl-D at the prompt cancels the scan (exit 130). A terminal or SSH
  session that closes at the prompt is not a cancel either: it is an interruption that keeps the
  fronts (exit 129).
- **The scan stopped part-way through the stack.** The scanner failed after some sheets had
  already been fed -- a jam, a misfeed, a page that took too long, or the working directory
  running out of room. Those sheets are not lost. saneless assembles them into a PDF under
  `failed/` in its data directory, and the error names both the count and the path:

    ```
    Scanner error on page 40: Document feeder jammed. The 39 page(s) scanned before the error were preserved at /var/lib/saneless/failed/20260916-013255-4c8627dd-invoice-partial.pdf
    ```

    Nothing is uploaded. Clear whatever stopped the feeder, then either scan the whole stack again
    and delete the preserved file, or keep it and scan only the sheets it is missing. Empty page
    detection is deliberately not applied to a preserved scan, so it shows exactly what the feeder
    picked up. saneless never deletes anything from `failed/`; draining it is your job (see
    [Docker volumes](../reference/docker.md#volumes)).
- **A page took too long.** saneless allows 120 seconds for each page, on the feeder and on the
  flatbed alike, and the line reads `Page 3 timed out after 120s`. On a network scanner this
  usually means the link dropped mid-page. Any sheets scanned before it are preserved as above.
- **saneless says to restart it.** After a page times out, saneless cancels the read and waits for
  the scanner to acknowledge. When it never does, the device cannot be reused safely, so the next
  scan is refused before saneless touches the scanner at all, with a line ending:

    ```
    The scan will be possible again as soon as the scanner releases it. Restart saneless if it does not.
    ```

    That wording is literal. A hang that clears itself -- a network scanner that comes back, a
    driver that finally returns -- releases the device on its own and the next scan works with no
    restart. If the message keeps appearing, check the link to the scanner first, because the read
    cannot return while that is down, and then restart saneless.

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
- **No scanner found.** saneless discovered no scanner to use: `scan` with `scanner.device` empty,
  or `auto-profiles`. Set `scanner.device`, or fix discovery with `saneless devices`.
- **`serve` cannot start.** The port is already in use, SANE could not be initialised, or the web
  server failed to start. Stop whatever holds the port or pass `--port`; when SANE failed, the line
  gives its reason (see [Scanner Host Discovery](scanner-host-discovery.md)); when the web server
  itself failed, the cause is in the preceding log lines: `serve` streams its log to stderr
  rather than writing a file.
- **The working directory cannot be prepared.** The line names `tmp_dir` (in
  [`[output]`](../reference/configuration.md#output)) and the reason: the directory was removed or
  cannot be created, or the disk is full. Check that it exists, that saneless can write to it, and
  that there is free space.
- **The working directory is not private.** The line names `output.tmp_dir`, its path and what is
  wrong with it: it is a symbolic link, it is not a directory, it belongs to another user, or its
  group or everyone can write to it. saneless keeps scanned pages there and will not use a
  directory someone else could change. Run `chmod 700` on a directory you own (the line gives the
  command), remove it so saneless creates it privately, or set `tmp_dir` to another directory.
- **The job database cannot be used.** The line starts with `Job database error:` and names the
  database path and the reason: the file cannot be opened or is not a SQLite database, or its jobs
  table has a shape this version of saneless does not recognise. Check that the path is right,
  that saneless can read and write it and its directory, and that the file really is saneless's
  job database. If it is damaged, move it aside: saneless then starts with an empty job history,
  and the moved file is kept for inspection or restoring.
- **`paperless.url` or `paperless.token` refused.** Spaces and line breaks around either value
  are ignored. What is left of `paperless.url` must be empty, or an `http://` or `https://`
  address that names a host and holds no user name or password. What is left of
  `paperless.token` must be visible ASCII, with no spaces, line breaks or control characters
  inside it. A value that breaks these rules is refused when the config loads, so every command
  that reads the config exits 2 and `serve` does not start. The line names the key and the rule,
  never the value, for example `[paperless] token: Value error, must contain only visible ASCII
  characters: no spaces, line breaks or control characters inside it`. See
  [`[paperless]`](../reference/configuration.md#paperless).
- **`paperless.url` not set.** An empty `paperless.url` loads, so `serve` can start and show what
  is missing: the status strip's Paperless row and `saneless doctor` say `The paperless-ngx
  address has not been set.` No scan starts while it is empty: `saneless scan` exits 2 before the
  scanner is opened, with a line ending `the paperless-ngx address in paperless.url has not been
  set`, and the web UI greys out the Scan button and refuses the scan. Should a scan still reach
  the upload with no address, it fails with a line saying `paperless.url is not set, or has no
  http or https scheme`. It is not retried, and nothing is copied to the consume folder even when
  one is configured: the PDF is kept in `failed/` in the data directory and the line ends with its
  path. Set the URL, then upload the kept PDF yourself or scan again.

**If an earlier version of saneless ever showed your API token in an error, rotate the token.**
Earlier versions sent a token with a trailing space or line break -- easy to get from a secret
file or a `.env` file with Windows line endings -- exactly as written, and the request then failed
with an error that quoted the token. That error could be printed, logged and stored on the job,
where the job history shows it. saneless now ignores those characters, and when a reply from
paperless-ngx or a proxy, or an HTTP library error, quotes the token, it is replaced by `***` in the
message, the log and any traceback. saneless does not rewrite job records or logs written before. Generate a new API token in paperless-ngx and
put it in `paperless.token`, so the copy left behind no longer works.

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
  configured, the PDF is saved there instead and the scan exits 6, not 3 -- see
  [Saved to the consume folder](#saved-to-the-consume-folder-exit-6). Without one, the scan
  fails. Check that
  `paperless.url` is reachable from where saneless runs. A `https://` certificate that this
  machine does not trust also arrives here, reported as unreachable in both the log and the web
  UI -- see **TLS certificate not trusted** below before you go looking at the network.
- **TLS certificate not trusted.** The line names an SSL failure, for example
  `Paperless error: Could not fetch tags from Paperless at https://paperless.example.com/: [SSL:
  CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate (_ssl.c:1081)`,
  and the scan exits 3 -- but only without a consume directory. A certificate that cannot be
  verified is classified as unreachable, so the upload is retried, the retries are exhausted, and
  with a consume directory configured the PDF is saved there instead: the scan then ends
  `FALLBACK`, shown as **Saved to folder**, and `saneless scan` prints `Saved to folder: <title>`
  on stdout, the `Not uploaded: ...` line and the warning on stderr, and exits 6 (see
  [Saved to the consume folder](#saved-to-the-consume-folder-exit-6)). That is the dangerous case
  rather than the benign one -- every scan still delivers a document, but into a folder that
  applies none of its title, tags or correspondent, so a TLS misconfiguration can run unnoticed
  for a long time. Watch for it where each kind of scan reports: for `saneless scan`, exit 6 and
  the stderr lines, which a script should treat as a problem to fix rather than a success; for a
  web scan, **Saved to folder** in the status area and the job history (`saneless jobs` lists
  web scans only, because a CLI scan is not recorded in the job store).
  **The same failure looks different in the web UI.** The status strip and
  `GET /api/paperless/test` report a bare `unreachable` with no TLS text anywhere, because a
  certificate that cannot be verified means the connection never established, and saneless
  classifies that as unreachable -- it is reported as the **Unreachable.** case above, with the
  SSL detail dropped. If your web UI says Unreachable, this bullet may be why. The message on the
  scan path is OpenSSL's own and has not changed; what
  changed is which certificate authorities are trusted. saneless now verifies against the
  operating system's trust store instead of a certificate bundle shipped inside a Python package.
  So a private or corporate CA installed on the machine now works where it used to fail, and one
  installed only by editing that Python bundle now gives you this error, which you may not have
  been getting before. There are two fixes. Install the CA into the operating system's trust
  store, which is the better option wherever it is available -- in the container, copy the
  certificate to `/usr/local/share/ca-certificates/my-ca.crt` and run `update-ca-certificates`.
  Or point OpenSSL at the certificate file directly with `SSL_CERT_FILE`, described in
  [Environment Variables](../reference/environment-variables.md#not-a-saneless-variable-ssl_cert_file-and-ssl_cert_dir).
  If the path you give does not exist, or is a directory rather than a PEM file, saneless refuses
  at startup with `Paperless error: Could not build the TLS trust store for Paperless at <url>:
  <OS error text>; check SSL_CERT_FILE and SSL_CERT_DIR` -- a different error from the
  certificate-verify failure above, and one that means the remedy itself is misconfigured rather
  than the certificate being untrusted. Under Docker the usual cause is a bind mount whose host
  file is missing, which gives the container a directory.
  Do not turn certificate verification off to make this go away: saneless sends the Paperless API
  token on every request, and an unverified connection hands that token to anyone in the path.
- **Malformed or unset URL.** This is a configuration error (exit 2), not a Paperless error,
  and it is never retried and never saved to the consume folder. See
  **`paperless.url` or `paperless.token` refused** and **`paperless.url` not set** under
  [Configuration errors](#configuration-errors-exit-2).
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

The scanned pages are not lost when this fails. saneless writes each page to disk as it arrives,
so when assembly is what failed it moves those page files into a job-keyed directory under
`failed/` in its data directory and names that directory in the error. The pages are ordinary PNG
files, one per sheet, named `a-0001.png`, `a-0002.png`, ... in the order the first pass fed them,
and `b-0001.png`, ... for a second pass. Fix the cause and scan again, or assemble them yourself if
rescanning the document is not practical.

**If it was a manual duplex job, the file names are not the page order.** You turned the stack over
between the passes, so the backs came back in reverse: the document is `a-0001`, `b-000N`,
`a-0002`, `b-000N-1`, and so on, with the *last* `b-` file behind the first `a-` file. Sorting the
directory by name would put every front page first and every back page last, in the wrong order.
A simplex scan has only `a-` files and sorts correctly.

## Saved to the consume folder (exit 6)

stdout reads `Saved to folder: <title>`, and stderr reads `Not uploaded: saved to the consume
folder without its title, tags or correspondent`, followed by the warning, which names where the
PDF was written.

The document was delivered, so do not scan the stack again. saneless could not reach paperless-ngx
through its API, even after retrying, and a consume directory is configured, so it wrote the PDF
there instead. paperless-ngx picks the file up from that folder and applies its own matching rules
to it, not the title, tags and correspondent chosen for this scan.

- In paperless-ngx, find the new document and set its title, tags and correspondent by hand.
- Then find out why the API upload failed: it is the **Unreachable.** or **TLS certificate not
  trusted.** case under [Paperless errors](#paperless-errors-exit-3), and every scan will keep
  going to the folder until it is fixed.

## Uploaded with a warning (exit 7)

stdout reads `Uploaded with a warning: <title>`, and the warning itself is on stderr. The document
reached paperless-ngx with its title, tags and correspondent, so do not scan the whole stack
again. The warning is one of these:

- **Pages could not be read by the scanner and were skipped.** The warning gives the count. Those
  sheets are missing from the document in paperless-ngx; open it, find the gaps, and scan just
  those sheets.
- **Page count mismatch.** A manual duplex scan got a different number of fronts from backs, so
  saneless could not interleave them. It uploaded the fronts and the backs as two separate
  documents, with `(fronts)` and `(backs)` after the title. In paperless-ngx, check both documents
  and look for a sheet that fed twice or not at all.
- **The scanner could not read N sheet(s), so the fronts and backs could not be paired
  reliably.** In a manual duplex scan, one of the passes skipped a sheet it could not read. A
  skipped sheet moves every later page of that pass by one, so saneless does not interleave the
  passes even when the two counts agree. It uploaded the fronts and the backs as two separate
  documents, with `(fronts)` and `(backs)` after the title, and the warning gives the number of
  sheets. The `(backs)` document is in sheet order, the same order as `(fronts)`, not the reversed
  order the second pass fed them in. In paperless-ngx, find the sheets missing from either
  document and scan them again, or scan the whole stack again and delete both documents.

A run that was saved to the consume folder *and* carries a warning exits 6, not 7: the missing
title, tags and correspondent are the larger problem.

## Every page looked blank (exit 8)

The line starts with `Empty-page detection:`. The scanner worked: it returned pages, and
empty-page detection judged every one of them blank, so nothing was uploaded. The scanner is not
the thing to check.

The pages are not lost. saneless assembles every page it scanned, before detection removed any,
into a PDF under `failed/` in its data directory, and the line names the path. Open it:

- **If the pages really are blank**, there is nothing to do. Delete the file.
- **If they are not blank** -- faint pencil, light print or a mostly empty form -- detection was too
  eager for this document. Lower `empty_page_coverage_threshold` for the profile, or turn detection
  off for it with `enable_empty_page_detection = false`, then scan again. Or keep the preserved
  PDF and upload it yourself.

[When Every Page Is Blank](../explanation/empty-page-detection.md#when-every-page-is-blank)
explains how detection decides.

## Interrupted by a signal (exit 129 and 143)

The line starts with `Interrupted:`. Something outside saneless stopped the command while it ran:

- **129** is SIGHUP: the terminal or SSH session running the command went away.
- **143** is SIGTERM: `kill`, a service manager or a container runtime stopped the command.

Each code is 128 plus the signal number, the shell's convention. Nobody chose to stop the scan, so
it is not treated as a cancel: the pages already scanned are kept as a PDF under `failed/` in the
data directory, and the line names the path. Nothing is uploaded. Scan the rest of the stack, or
the whole stack again, and delete or upload the kept file yourself.

Ctrl-C is different. It is a deliberate cancel, exits 130 and keeps nothing (see
[Cancelled scans](#cancelled-scans-exit-130)). To run a long scan over SSH without a dropped
connection interrupting it, start it under `tmux` or `screen`. `saneless serve` keeps the web
server's own signal handling, where SIGTERM is a graceful stop.

## Cancelled scans (exit 130)

Exit 130 means the scan was stopped on purpose, not that something broke:

- You answered no, pressed Ctrl-D or pressed Ctrl-C at the manual duplex flip prompt.
- You pressed Ctrl-C while a one-shot command (`scan`, `devices`, `auto-profiles`, `jobs`) was
  running, or while `serve` was still starting up.

Nothing is uploaded, and nothing is kept in `failed/` either. A failure keeps whatever it can,
because you cannot get those sheets back without feeding them again; a cancel keeps nothing,
because you chose to stop and saneless would only be leaving you files to delete. In the web UI, a
scan cancelled with **Abort scan** at the flip step is shown as Cancelled, in grey rather than as
an error. A flip wait that times out is not a cancel: it
fails with exit 1. Ctrl-C on `saneless serve` once the web server is running is a normal stop and
exits 0.

## Unexpected errors (exit 5)

Exit 5 means saneless hit an error it did not anticipate: a bug in saneless, not a problem with
your setup. Setup problems exit 2.

The line starts with `Unexpected error` and names the exception type. The full traceback is in
the log file, which the line names. If the line names no log file (the error happened before
logging started, or the log file could not be opened), it ends with a hint instead: run the
command again with `-v` to see the traceback on stderr.

`saneless serve` is the exception: it writes no log file, so the traceback is already on its
stream, printed directly above the line, and no hint is offered. Collect it from `docker logs`
or `journalctl` instead of restarting the service.

Please report it as a bug and attach the log file. Attach the log only, never your config file,
which holds your Paperless API token. saneless keeps the HTTP libraries' own debug output out of
the log whatever `log_level` says, because it can include the authorization header. A log that an
earlier version wrote with `log_level = "DEBUG"` may still hold it, so search such a log for your
token before sharing it.
