# Architecture Overview

saneless bridges SANE-compatible network scanners and paperless-ngx. It provides a web UI and CLI for triggering scans, assembling multi-page PDFs, and ingesting documents into paperless-ngx with metadata -- all from any device on the local network.

This page explains *how* saneless is structured and *why* it works the way it does.

## The Scan Pipeline

Every scan job follows the same flow, whether triggered from the web UI or the CLI:

```
Scanner -> [SaneBackend] -> PIL Images -> [Empty Page Filter] -> [img2pdf] -> PDF -> [PaperlessClient] -> paperless-ngx
                                                                                  \-> [Consume Dir] (fallback)
```

### Scanner Abstraction

The `ScannerBackend` abstract base class isolates SANE behind a clean interface with three methods: `get_devices()` for discovery, `get_capabilities()` for querying available options, and `scan_pages()` for acquiring images. The `SaneBackend` implementation wraps `python-sane` and handles low-level details like device opening, option setting, and the `multi_scan()` iterator for ADF feeders.

This abstraction means the core pipeline never touches SANE directly. If a future version adds support for driverless scanning (eSCL, WSD via `sane-airscan`), only a new backend implementation is needed -- the pipeline, web layer, and configuration remain unchanged.

### Pipeline Orchestration

The `run_pipeline()` function coordinates the full scan flow:

1. **Resolve scanner** -- find the configured device or auto-detect the first available one.
2. **Set parameters** -- apply the selected profile's source, resolution, and color mode.
3. **Acquire pages** -- scan via the scanner backend. For manual duplex, this involves two passes with a flip prompt between them (see [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md)).
4. **Filter empty pages** -- remove blank pages using the [dual-threshold algorithm](empty-page-detection.md).
5. **Assemble PDF** -- convert scanned PIL images to a PDF document.
6. **Upload to paperless-ngx** -- send the PDF with metadata via the REST API, or fall back to the [consume directory](consume-directory-fallback.md) if the API is unavailable.

Each stage emits a `PipelineEvent` (`SCANNING`, `AWAITING_FLIP`, `SCANNING_REVERSE`, `ASSEMBLING`, `UPLOADING`, `DONE`). The web UI uses these events to update the live status indicator via HTMX polling.

### PDF Assembly

PDF assembly uses `img2pdf`, which embeds scanned PIL images directly into the PDF without re-encoding. This is lossless -- the image data in the PDF is byte-for-byte identical to what the scanner produced. No quality is lost, and assembly is fast because there is no re-compression step.

### Paperless Upload

The `PaperlessClient` sends the PDF via multipart POST to the paperless-ngx REST API (`/api/documents/post_document/`) with metadata including title, tags, correspondent, and creation date. After upload, it polls the task endpoint with exponential backoff until paperless-ngx reports success or failure.

If the API is unreachable (network error, timeout, auth failure) and a consume directory is configured, the PDF is deposited there as a fallback. See [How Consume Directory Fallback Works](consume-directory-fallback.md) for details.

## Worker Thread Model

The web server runs a background worker thread that processes one scan job at a time. Jobs are submitted to a `queue.Queue` and executed sequentially -- scanning is inherently serial because a physical scanner can only handle one job at a time.

The worker communicates progress back to the web layer via state transitions on the `Job` object stored in SQLite. The UI polls the job status endpoint, and HTMX updates the status indicator when the state changes. This design keeps the web layer fully responsive during long-running scans.

For manual duplex scanning, the worker blocks between pass A (fronts) and pass B (backs) using a `threading.Event`. The web UI displays a flip prompt, and when the user clicks Continue, the event is set and the worker proceeds with the second pass.

## Job Storage

Job records live in a SQLite database under the configured `tmp_dir`, and a single `JobStore` instance owns the only connection to it. Nothing else in the process opens that file. Centralising the connection is what makes the rest of this section possible: there is exactly one place a transaction can begin, one place the schema can change, and one lock to take. The connection enables write-ahead logging before it switches to explicit transaction control, because SQLite refuses a journal-mode change once a transaction is open.

That single connection is driven by two kinds of caller. Web request threads read and write job rows while serving HTTP, and the background worker thread writes state transitions as a scan progresses. A `sqlite3` connection is not safe to drive from several threads at once, so every public `JobStore` method is serialised on a `threading.RLock` and each method opens its own transaction inside that lock. The lock guards the connection, not merely the data: without it, two threads can interleave statements inside one another's transaction and commit a mixture of both. Public methods deliberately never call other public methods, because `sqlite3` transaction contexts do not nest -- an inner one commits the outer -- and that rule is enforced by a test rather than left to convention.

The schema is versioned with `PRAGMA user_version` and evolved by an ordered ladder of migration steps, where step *N* produces version *N*. A fresh database reports version 0 with no `jobs` table, so the ladder runs from the start and builds the schema from nothing. A database written by an earlier release also reports version 0 but already has a `jobs` table, so it is taken to be at version 1 and joins the ladder partway. Each step stamps its version and commits before the next begins, so a ladder that fails at step *N* leaves a valid database at version *N*-1 rather than a half-applied one.

If a database does not have the shape the next step expects, the store raises `StorageError` naming the file and the columns it could not find, and closes the connection rather than opening. The loud refusal is the point. What this replaced was an `ALTER TABLE` wrapped in a bare `except: pass`, which made an unrecognised database look like a clean startup and then failed at the first write -- far from the cause, with an error that said nothing about the schema. Failing at open time puts the report where the problem is, and because the check runs before the first `ALTER`, a rejected database is left exactly as it was found and can still be inspected or restored.

Retention is a single `DELETE` that removes jobs past an age cutoff together with jobs outside a row cap, and it reports its count from that one statement rather than from reads taken either side of it, so a job inserted mid-prune cannot make the count wrong. Both limits are settings -- see `history_retention_days` and `history_max_rows` in the [Configuration Reference](../reference/configuration.md). Both select on creation time and neither consults whether the job finished, so retention removes in-flight and errored records on exactly the same terms as completed ones.

The table also carries columns held in reserve for later work: `outcome`, `pages_scanned`, `pages_removed`, `pages_uploaded`, `warning` and `owner_token` exist in the schema but nothing writes any of them yet, so they read back unset on every job and no part of the UI or CLI displays them. They were added in one migration so that the work which eventually populates them does not have to migrate the database again for each field.

## Configuration Layer

saneless uses `pydantic-settings` to load configuration from a TOML file with environment variable overrides. This gives operators two ways to configure the application:

- **TOML file** (`saneless.toml`) for persistent settings on bare-metal installs.
- **Environment variables** (prefixed `SANELESS_`, nested with `__`) for container deployments where secrets should not be baked into the image.

Configuration search paths follow XDG conventions: the current directory, `~/.config/saneless/`, and `/etc/saneless/`.

The **profile system** stores named scanner presets (source, resolution, color mode, default metadata) so users can switch between common scan types without reconfiguring each time. Profiles are defined in the TOML file under `[profiles.<name>]` sections.

## Web Layer

The web layer is built with FastAPI, Jinja2 templates, and HTMX for reactivity. There is no JavaScript framework and no JS build step -- the entire frontend is server-rendered HTML with declarative HTMX attributes for dynamic updates.

PicoCSS provides classless styling with automatic dark/light mode support. A single external `app.js` file handles HTMX event delegation for scan button state management and flip prompt interactions.

The web UI is a thin layer over the same pipeline that the CLI uses. Both entry points call `run_pipeline()` with identical parameters -- the only difference is how status updates are delivered (HTMX polling vs. terminal output).
