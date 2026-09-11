# How Consume Directory Fallback Works

## The Problem

If paperless-ngx is temporarily unavailable -- during maintenance, a restart, or a network interruption -- scanned documents should not be lost. The user has already fed pages through the scanner; discarding the result because of an upload failure would be unacceptable.

## The Solution

When a `consume_dir` is configured and the API upload fails after all retry attempts are exhausted, saneless deposits the assembled PDF into the consume directory instead of raising an error.

The pipeline retries uploads with exponential backoff (up to 3 attempts by default) before falling back. This means transient network blips are handled by retries, and only sustained outages trigger the fallback.

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

1. The API upload fails (connection refused, timeout, or server error).
2. All retry attempts are exhausted.
3. A `consume_dir` is configured (non-empty string).

If no `consume_dir` is configured, the upload error propagates and the scan job enters the ERROR state. The user sees the error in the web UI or CLI output.

**The scanned document is not lost when that happens.** Before the error propagates, saneless moves the assembled PDF into `failed/` inside its data directory -- durable storage, deliberately separate from the disposable scratch directory the scan was built in -- and appends the full path of the preserved file to the job's error message. The error text shown in the web UI therefore names the file to go and find. The same preservation happens when the upload reaches paperless-ngx but the consumption task then reports a failure, and when the task has not finished before `paperless_task_timeout` expires. See [Docker volumes](../reference/docker.md#volumes) for where that directory lives in a container and how to drain it.

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

A job also carries a `warning` field, which the status area prints beneath the status line when it is set and `saneless jobs --json` reports as `"warning"`. Only one situation fills it in today: an ADF duplex scan whose front and back page counts did not match. A plain consume-directory fallback leaves `warning` null -- the `FALLBACK` state is itself the signal, and the metadata consequence is the general one described under Limitations above rather than a per-job message.
