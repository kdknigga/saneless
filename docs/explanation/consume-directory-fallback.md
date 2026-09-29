# How Consume Directory Fallback Works

## The Problem

If paperless-ngx is temporarily unavailable -- during maintenance, a restart, or a network interruption -- scanned documents should not be lost. The user has already fed pages through the scanner; discarding the result because of an upload failure would be unacceptable.

## The Solution

When a `consume_dir` is configured and the API upload cannot get through, saneless deposits the assembled PDF into the consume directory instead of raising an error.

An upload is sent again only when it cannot have reached paperless-ngx: the connection was refused or could not be made in time, no connection was free, a proxy refused to open the tunnel, or sending the file itself timed out, so paperless-ngx never had the whole document. Those failures are retried for about 60 seconds, with waits that double from 1 second up to 5 seconds. When saneless talks to paperless-ngx directly, that is long enough to ride out a paperless-ngx or container restart, so a short blip still delivers through the API, and only an outage that lasts longer falls back to the consume directory. Behind a reverse proxy (nginx, Traefik, Caddy) it is not: while paperless-ngx restarts, the proxy itself answers, usually with 502 or 503. saneless cannot tell that answer from one paperless-ngx sent after reading the upload, so it neither resends nor copies: the scan ends amber with the PDF in `failed/`, as described next.

Any other failure is never resent and never copied to the consume directory. When the whole file was sent and then no usable answer came back -- the connection dropped or timed out while saneless waited for the answer, a reverse proxy or paperless-ngx answered with a 5xx, or a 200 did not carry a task id -- paperless-ngx may already have stored the document, and a second upload or a copy in the folder could file it twice. The scan then ends amber, labelled **May be in paperless-ngx** in the web UI, with the PDF kept in `failed/`, and `saneless scan` exits 9; see [The document may already be in paperless-ngx](../how-to/troubleshoot-a-failed-scan.md#the-document-may-already-be-in-paperless-ngx-exit-9). saneless waits for that answer for 30 seconds plus 1 second per MiB of the PDF, at most 300 seconds, because paperless-ngx answers only once it has read and stored the whole file.

## How Paperless-ngx Picks It Up

paperless-ngx has a built-in "consumption" feature that watches a directory for new files and imports them automatically. By pointing saneless's `consume_dir` at paperless-ngx's consumption directory, documents are ingested when paperless comes back online -- no manual intervention required.

In a typical deployment, this consumption directory is the same path configured in paperless-ngx's `PAPERLESS_CONSUMPTION_DIR` environment variable.

## Configuration

Set the consume directory in your TOML config file:

```toml
[paperless]
url = "http://paperless.local:8000"
token = "abc123def456"
consume_dir = "/path/to/paperless/consume"
```

In Docker, mount the consume directory as a shared volume between the saneless and paperless-ngx containers:

```yaml
services:
  saneless:
    volumes:
      - consume:/consume
    environment:
      SANELESS_PAPERLESS__CONSUME_DIR: /consume

  paperless:
    volumes:
      - consume:/usr/src/paperless/consume
    environment:
      PAPERLESS_CONSUMPTION_DIR: /usr/src/paperless/consume

volumes:
  consume:
```

### Who can read the copy

saneless writes the copy with mode `0644`, whatever its umask, so paperless-ngx can read it whichever user it runs as. The hidden staging file is created readable and writable only by saneless, and gets `0644` before the first byte of the PDF is written, so nobody else can write to it at any point and the PDF never appears under its final name with any other mode. On a filesystem that has no Unix modes, such as some CIFS, vfat or FUSE mounts, the mode is left as it is and the copy is still delivered.

The consume folder's own directory mode decides who can reach the file, so restrict the folder rather than the file. For example, make it group-owned by a group that both saneless's user and paperless-ngx's user belong to, and give it mode `0770`:

```bash
chgrp paperless /path/to/paperless/consume
chmod 0770 /path/to/paperless/consume
```

Other local users then cannot list or open anything in it. The folder is a short-lived handoff: paperless-ngx imports each file and removes it, so a copy stays there only until paperless-ngx is back.

## When It Activates

The fallback activates only when **all** of these conditions are true:

1. The API upload fails in a way that proves it cannot have reached paperless-ngx -- one of the failures listed above that are sent again.
2. Those failures go on for about 60 seconds.
3. A `consume_dir` is configured (non-empty string).
4. That directory exists.

**saneless never creates the consume directory.** A configured directory that is missing almost always means the paperless-ngx volume is not mounted where saneless looks, and a directory made in its place would be one nothing watches, so the copy would sit there unseen. So when a fallback finds no directory, the scan fails with `consume directory <dir> does not exist — is the paperless-ngx volume mounted?` (exit 3), and the PDF is kept in `failed/` as described below. The Fallback row of `saneless doctor` and the web UI's checks is red for as long as the directory is missing. Mount paperless-ngx's consume volume at that path, or correct `consume_dir`, then import the kept PDF into paperless-ngx rather than scanning the stack again.

**An upload that may have arrived never falls back.** A dropped answer, a read timeout, a 5xx or a 200 without a task id ends amber and exits 9, as described above: the PDF goes to `failed/`, never to the consume directory.

**A configuration problem never falls back either.** A `paperless.url` without an `http://` or `https://` scheme and a host, or a `paperless.token` with a space, line break or control character inside it, is refused when the config loads, so saneless does not run at all (exit 2). An empty `paperless.url` loads, because it means "not set yet", but no scan starts while it is empty: `saneless scan` and the web UI refuse before the scanner is opened. Should a scan reach the upload with no address anyway, it fails at once as a configuration error: it is not retried, nothing is copied to the consume directory, and the PDF is kept in `failed/` as described below. The consume directory stands in for paperless-ngx while paperless-ngx is away; it is not a way to run saneless with no paperless-ngx address at all.

**A rejected or redirected upload never falls back.** When paperless-ngx answers with a 4xx -- a bad token, or a field it refuses, such as an invalid title -- the upload is not retried and nothing is copied to the consume directory. The scan fails at once with Paperless's reason, because the same request would only be rejected again. A redirect (a 3xx) is treated the same way: it almost always means `paperless.url` points at the wrong address, such as an `http://` URL behind a proxy that redirects to `https://`, so the scan fails at once and the error names where the upload was redirected to.

If no `consume_dir` is configured, the upload error propagates and the scan job enters the ERROR state. The user sees the error in the web UI or CLI output.

**The scanned document is not lost when that happens.** Before the error propagates, saneless moves the assembled PDF into `failed/` inside its data directory -- durable storage, deliberately separate from the disposable scratch directory the scan was built in -- and appends the full path of the preserved file to the job's error message. The error text shown in the web UI therefore names the file to go and find. The same preservation happens when the upload may have reached paperless-ngx without an answer, when it reaches paperless-ngx but the consumption task then reports a failure other than a duplicate, and when the task has not finished before `paperless_task_timeout` expires. Those three end amber rather than red, because the document may already be in paperless-ngx: check before you import the kept copy. See [Docker volumes](../reference/docker.md#volumes) for where that directory lives in a container and how to drain it.

`failed/` is where every scan saneless could not deliver ends up, not only the ones an upload lost, so it holds three kinds of thing:

- **Complete PDFs** from a scan that was assembled but could not be delivered -- the case described above.
- **Partial PDFs** from a scan that stopped part-way. A scanner fault after some sheets had been fed keeps those sheets; a manual duplex job whose second pass or flip failed keeps the fronts. Both are PDFs, named so you can tell them apart from a complete one, and both are unfiltered -- empty page detection is not applied to them, because what the feeder actually picked up is the evidence.
- **Directories of page files** from a scan that could not be assembled into a PDF at all, or whose partial PDF could not be built either. Each is named after the job and holds one PNG per sheet, under the names the spool gave them: `a-0001.png`, `a-0002.png`, ... for the first pass and `b-0001.png`, ... for a second. **That is acquisition order, one pass at a time, and it is not always document order.** For a simplex scan the two are the same. For a [manual duplex](../how-to/set-up-adf-duplex.md) job they are not: you turned the stack over between the passes, so the backs came back reversed and the document reads `a-0001`, `b-000N`, `a-0002`, `b-000N-1`, and so on -- the last back page belongs behind the first front page. Interleave them that way before assembling, or rescan the stack.

The rule is the same for all three: a failure keeps everything it can, because you cannot get the paper back without feeding it again. An operator's cancel keeps nothing, because stopping was the decision. Nothing in `failed/` is ever deleted, moved or rotated by saneless.

### Network blips after the upload

Once paperless-ngx has accepted the upload, saneless waits for its consumption task to finish. A network error while checking on that task does not fail the scan, and nor does a 5xx or a 429 from paperless-ngx or a proxy in front of it: saneless keeps checking, with waits of at most 5 seconds, until `paperless_task_timeout` expires. Only then does it report that paperless-ngx received the document but did not confirm filing it: amber, labelled **Received, not confirmed** in the web UI, with the PDF kept in `failed/`, and exit 9 from `saneless scan`. When the last check failed, the message names that error; a blip that later checks got past is not blamed.

### Duplicates

saneless's own retries never make a second document: an upload is sent again only when it cannot have reached paperless-ngx, and one that may have arrived is never resent or copied. If the connection drops after paperless-ngx has received the file but before its answer reaches saneless, the scan ends amber with exit 9 and a copy in `failed/`, and nothing is sent twice.

paperless-ngx recognises a duplicate by the file's checksum, so this concerns the same file arriving twice -- such as the copy in `failed/` imported after the original did arrive -- rather than a new scan of the same paper. What happens then depends on the paperless-ngx release:

- **paperless-ngx 2.x** always refuses a duplicate: its consumption task fails, naming the document it already holds.
- **paperless-ngx 3.x** stores a duplicate as a second document, unless `PAPERLESS_CONSUMER_DELETE_DUPLICATES` is enabled, in which case it refuses it as 2.x does.

When paperless-ngx refuses an upload as a duplicate, the document is already there, so saneless reports the scan as **Uploaded with a warning** rather than as a failure, and keeps nothing in `failed/` (exit 7 from `saneless scan`). The warning names the existing document -- *"paperless-ngx already holds this file as document #42; it was not stored again, and this scan's title and tags were not applied to it."* -- and says so when that document is in paperless-ngx's trash.

## Limitations

**Metadata is not preserved.** When using the consume directory fallback, only the PDF file is saved. Title, tags, and correspondent metadata specified for the scan job are lost. Paperless-ngx applies its default processing rules (matching rules, ASN assignment, OCR) when it picks up the file from the consume directory.

This is a deliberate trade-off: saving the document without metadata is better than losing the document entirely. If metadata is critical, re-upload the document through paperless-ngx's web interface after it comes back online.

## How the Job Reports It

**A web scan's job status distinguishes the two paths.** A scan that fell back to the consume directory ends in the `FALLBACK` state -- terminal, and distinct from both `DONE` and `ERROR`. It is labelled **Saved to folder** everywhere a job state is rendered:

- The web UI status area shows `Saved to folder: <title>` in amber, visually distinct from the green Complete and the red Failed.
- The job history table renders the same amber label in its status column.
- `saneless jobs` prints `Saved to folder` in the Status column.
- `saneless jobs --json` reports `"state": "FALLBACK"` and `"outcome": "FALLBACK"`. The JSON output is deliberately not humanised: it stays the raw enum value, so scripts can compare against it.

So the job history does mark which documents arrived without metadata. You no longer have to spot them from the paperless-ngx side.

**A CLI scan reports it on the terminal instead.** `saneless scan` does not write to the job store, so its scans never appear in the job history or in `saneless jobs`. A CLI scan that fell back prints `Saved to folder: <title>` on stdout, then two lines on stderr -- `Not uploaded: saved to the consume folder without its title, tags or correspondent`, and `Warning: ` followed by the sentence quoted below -- and exits with code 6. A script should treat exit 6 as "delivered, but fix the connection": the document is in paperless-ngx's hands, so scanning the stack again would only file it twice. See [Troubleshoot a Failed Scan](../how-to/troubleshoot-a-failed-scan.md#saved-to-the-consume-folder-exit-6).

A job also carries a `warning` field, which the status area prints beneath the status line when it is set and `saneless jobs --json` reports as `"warning"`. Two situations fill it in.

A consume-directory fallback records where the file went and what that route cost:

> Saved to the paperless-ngx consume directory at `<path>` instead of uploading through the API, so the title, tags and correspondent chosen for this scan were not applied -- paperless-ngx will apply its own matching rules to the file instead.

The `FALLBACK` state says the document took the other route; the warning says what that route cost. The metadata consequence described under Limitations above is therefore also stated per job, on the job itself, rather than only in this document.

The second case is a scan that was uploaded but with something missing or split: a sheet the scanner could not read and skipped, or a manual duplex scan whose front and back page counts did not match, uploaded as two documents. That job ends `DONE`, but it is not shown as a plain success: the status area, the job history and `saneless scan` all label it **Uploaded with a warning**, in amber, with the warning sentence beneath it (on stderr, for the CLI, which exits 7). `saneless jobs --json` keeps `"state": "DONE"` and carries the sentence in `"warning"`.
