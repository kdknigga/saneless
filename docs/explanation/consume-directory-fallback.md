# How Consume Directory Fallback Works

## The Problem

If paperless-ngx is temporarily unavailable -- during maintenance, a restart, or a network interruption -- scanned documents should not be lost. The user has already fed pages through the scanner; discarding the result because of an upload failure would be unacceptable.

## The Solution

When a `consume_dir` is configured and the API upload cannot get through, saneless deposits the assembled PDF into the consume directory instead of raising an error.

The upload is attempted up to 3 times, with exponential backoff between attempts, whenever paperless-ngx cannot be reached or answers with a server error: a connection that is refused or reset, a timeout, a reverse proxy closing the connection, or a 5xx response. This means transient network blips are handled by retries, and only sustained outages trigger the fallback.

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

## When It Activates

The fallback activates only when **all** of these conditions are true:

1. The API upload fails in one of the ways listed above -- a connection refused or reset, a timeout, a reverse proxy closing the connection, or a server error.
2. All attempts are exhausted.
3. A `consume_dir` is configured (non-empty string).

**A configuration problem never falls back either.** A `paperless.url` without an `http://` or `https://` scheme and a host, or a `paperless.token` with a space, line break or control character inside it, is refused when the config loads, so saneless does not run at all (exit 2). An empty `paperless.url` loads, because it means "not set yet", but no scan starts while it is empty: `saneless scan` and the web UI refuse before the scanner is opened. Should a scan reach the upload with no address anyway, it fails at once as a configuration error: it is not retried, nothing is copied to the consume directory, and the PDF is kept in `failed/` as described below. The consume directory stands in for paperless-ngx while paperless-ngx is away; it is not a way to run saneless with no paperless-ngx address at all.

**A rejected or redirected upload never falls back.** When paperless-ngx answers with a 4xx -- a bad token, or a field it refuses, such as an invalid title -- the upload is not retried and nothing is copied to the consume directory. The scan fails at once with Paperless's reason, because the same request would only be rejected again. A redirect (a 3xx) is treated the same way: it almost always means `paperless.url` points at the wrong address, such as an `http://` URL behind a proxy that redirects to `https://`, so the scan fails at once and the error names where the upload was redirected to.

If no `consume_dir` is configured, the upload error propagates and the scan job enters the ERROR state. The user sees the error in the web UI or CLI output.

**The scanned document is not lost when that happens.** Before the error propagates, saneless moves the assembled PDF into `failed/` inside its data directory -- durable storage, deliberately separate from the disposable scratch directory the scan was built in -- and appends the full path of the preserved file to the job's error message. The error text shown in the web UI therefore names the file to go and find. The same preservation happens when the upload reaches paperless-ngx but the consumption task then reports a failure, and when the task has not finished before `paperless_task_timeout` expires. See [Docker volumes](../reference/docker.md#volumes) for where that directory lives in a container and how to drain it.

`failed/` is where every scan saneless could not deliver ends up, not only the ones an upload lost, so it holds three kinds of thing:

- **Complete PDFs** from a scan that was assembled but could not be delivered -- the case described above.
- **Partial PDFs** from a scan that stopped part-way. A scanner fault after some sheets had been fed keeps those sheets; a manual duplex job whose second pass or flip failed keeps the fronts. Both are PDFs, named so you can tell them apart from a complete one, and both are unfiltered -- empty page detection is not applied to them, because what the feeder actually picked up is the evidence.
- **Directories of page files** from a scan that could not be assembled into a PDF at all, or whose partial PDF could not be built either. Each is named after the job and holds one PNG per sheet, under the names the spool gave them: `a-0001.png`, `a-0002.png`, ... for the first pass and `b-0001.png`, ... for a second. **That is acquisition order, one pass at a time, and it is not always document order.** For a simplex scan the two are the same. For a [manual duplex](../how-to/set-up-adf-duplex.md) job they are not: you turned the stack over between the passes, so the backs came back reversed and the document reads `a-0001`, `b-000N`, `a-0002`, `b-000N-1`, and so on -- the last back page belongs behind the first front page. Interleave them that way before assembling, or rescan the stack.

The rule is the same for all three: a failure keeps everything it can, because you cannot get the paper back without feeding it again. An operator's cancel keeps nothing, because stopping was the decision. Nothing in `failed/` is ever deleted, moved or rotated by saneless.

### Network blips after the upload

Once paperless-ngx has accepted the upload, saneless waits for its consumption task to finish. A network error while checking on that task does not fail the scan: saneless keeps checking until `paperless_task_timeout` expires, and only then reports a timeout. When the last check failed with a network error, the timeout names that error; a blip that later checks got past is not blamed.

### Duplicates

A retry can create a duplicate document. If the connection drops after paperless-ngx has received the file but before its answer reaches saneless, saneless cannot tell the upload arrived and sends it again. On default paperless-ngx settings that stores a second copy of the document, which you can delete. saneless accepts this trade: a duplicate is easy to remove, a lost scan is not.

When paperless-ngx is set to reject duplicates, the second upload's task fails instead, and the failure says the document may already be in Paperless. Check paperless-ngx before scanning again.

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
