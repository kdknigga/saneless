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

1. The API upload fails in one of the ways listed above -- a connection refused or reset, a timeout, a reverse proxy closing the connection, or a server error -- or `paperless.url` is malformed, with no usable `http://` or `https://` scheme.
2. All attempts are exhausted. A malformed URL is the exception: it is not retried, because retrying cannot help, but it still falls back so the scan is not lost.
3. A `consume_dir` is configured (non-empty string).

**A rejected upload never falls back.** When paperless-ngx answers with a 4xx -- a bad token, or a field it refuses, such as an invalid title -- the upload is not retried and nothing is copied to the consume directory. The scan fails at once with Paperless's reason, because the same request would only be rejected again.

If no `consume_dir` is configured, the upload error propagates and the scan job enters the ERROR state. The user sees the error in the web UI or CLI output.

**The scanned document is not lost when that happens.** Before the error propagates, saneless moves the assembled PDF into `failed/` inside its data directory -- durable storage, deliberately separate from the disposable scratch directory the scan was built in -- and appends the full path of the preserved file to the job's error message. The error text shown in the web UI therefore names the file to go and find. The same preservation happens when the upload reaches paperless-ngx but the consumption task then reports a failure, and when the task has not finished before `paperless_task_timeout` expires. See [Docker volumes](../reference/docker.md#volumes) for where that directory lives in a container and how to drain it.

### Network blips after the upload

Once paperless-ngx has accepted the upload, saneless waits for its consumption task to finish. A network error while checking on that task does not fail the scan: saneless keeps checking until `paperless_task_timeout` expires, and only then reports a timeout, naming the last network error it saw.

### Duplicates

A retry can create a duplicate document. If the connection drops after paperless-ngx has received the file but before its answer reaches saneless, saneless cannot tell the upload arrived and sends it again. On default paperless-ngx settings that stores a second copy of the document, which you can delete. saneless accepts this trade: a duplicate is easy to remove, a lost scan is not.

When paperless-ngx is set to reject duplicates, the second upload's task fails instead, and the failure says the document may already be in Paperless. Check paperless-ngx before scanning again.

## Limitations

**Metadata is not preserved.** When using the consume directory fallback, only the PDF file is saved. Title, tags, and correspondent metadata specified for the scan job are lost. Paperless-ngx applies its default processing rules (matching rules, ASN assignment, OCR) when it picks up the file from the consume directory.

This is a deliberate trade-off: saving the document without metadata is better than losing the document entirely. If metadata is critical, re-upload the document through paperless-ngx's web interface after it comes back online.

## How the Job Reports It

**The job status distinguishes the two paths.** A scan that fell back to the consume directory ends in the `FALLBACK` state -- terminal, and distinct from both `DONE` and `ERROR`. It is labelled **Saved to folder** everywhere a job state is rendered:

- The web UI status area shows `Saved to folder: <title>` in amber, visually distinct from the green Complete and the red Failed.
- The job history table renders the same amber label in its status column.
- `saneless jobs` prints `Saved to folder` in the Status column.
- `saneless jobs --json` reports `"state": "FALLBACK"` and `"outcome": "FALLBACK"`. The JSON output is deliberately not humanised: it stays the raw enum value, so scripts can compare against it.

So the job history does mark which documents arrived without metadata. You no longer have to spot them from the paperless-ngx side.

A job also carries a `warning` field, which the status area prints beneath the status line when it is set and `saneless jobs --json` reports as `"warning"`. Two situations fill it in.

A consume-directory fallback records where the file went and what that route cost:

> Saved to the paperless-ngx consume directory at `<path>` instead of uploading through the API, so the title, tags and correspondent chosen for this scan were not applied -- paperless-ngx will apply its own matching rules to the file instead.

The `FALLBACK` state says the document took the other route; the warning says what that route cost. The metadata consequence described under Limitations above is therefore also stated per job, on the job itself, rather than only in this document.

The second case is an ADF duplex scan whose front and back page counts did not match, which records the two counts and notes that partial PDFs were saved.
