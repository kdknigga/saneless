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
3. **Acquire pages** -- scan via the scanner backend. For a simplex or hardware duplex profile this is a single `scan_pages()` call. A profile with `duplex = "manual"` splits this stage into three (see [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md#manual-duplex)):
    1. **Pass A** -- scan the front sides through the feeder.
    2. **Flip wait** -- wait, for at most `flip_timeout_seconds`, for the operator to flip the stack and confirm. An abort or a timeout fails the job here, before pass B.
    3. **Pass B** -- scan the back sides, then reverse them and interleave them with the fronts. If the two passes disagree on page count, the fronts and backs are delivered as two separate PDFs instead.
4. **Filter empty pages** -- remove blank pages using the [dual-threshold algorithm](empty-page-detection.md).
5. **Assemble PDF** -- convert scanned PIL images to a PDF document.
6. **Upload to paperless-ngx** -- send the PDF with metadata via the REST API, or fall back to the [consume directory](consume-directory-fallback.md) if the API is unavailable.

Each stage emits a `PipelineEvent` (`SCANNING`, `AWAITING_FLIP`, `SCANNING_REVERSE`, `ASSEMBLING`, `UPLOADING`, `DONE`). `AWAITING_FLIP` marks the start of the flip wait and `SCANNING_REVERSE` the start of pass B, and each becomes a job state of the same name. The web UI uses these events to update the live status indicator via HTMX polling.

### PDF Assembly

PDF assembly uses `img2pdf`, which embeds scanned PIL images directly into the PDF without re-encoding. This is lossless -- the image data in the PDF is byte-for-byte identical to what the scanner produced. No quality is lost, and assembly is fast because there is no re-compression step.

### Paperless Upload

The `PaperlessClient` sends the PDF via multipart POST to the paperless-ngx REST API (`/api/documents/post_document/`) with metadata including title, tags, correspondent, and creation date. After upload, it polls the task endpoint with exponential backoff until paperless-ngx reports success or failure.

If the API is unreachable (network error, timeout, auth failure) and a consume directory is configured, the PDF is deposited there as a fallback. See [How Consume Directory Fallback Works](consume-directory-fallback.md) for details.

## Worker Thread Model

The web server runs a background worker thread that processes one scan job at a time. Jobs are submitted to a bounded `queue.Queue` and executed sequentially -- scanning is inherently serial because a physical scanner can only handle one job at a time. The queue holds at most 10 jobs waiting to start. Submitting never blocks: a scan submitted to a full queue is refused with `429`, and one submitted while the worker is down or degraded is refused with `503` (see the [Web API reference](../reference/web-api.md#post-apiscan)).

The worker communicates progress back to the web layer via state transitions on the `Job` object stored in SQLite. The UI polls the job status endpoint, and HTMX updates the status indicator when the state changes. The scan itself never runs on a request: it runs on the worker thread. The route handlers are plain (non-`async`) functions, which FastAPI runs on its threadpool, so a slow paperless-ngx call or a database call in one request does not stall `/health`, the status poll or any other request.

**Startup.** Before the worker thread starts, the app fails every job that a previous process left active (running or still queued) with "The server restarted before this scan finished", then prunes job history. A queued job is never picked up again after a restart, so a scan cannot start on whatever paper happens to be in the feeder by then. If the job store cannot be written at this point, the app still starts, but the worker starts degraded and carries out that recovery once the store accepts writes. The worker thread's first act, before it takes any job, is [startup profile generation](../how-to/configure-scan-profiles.md#auto-generated-profiles): the server is already answering requests while it runs, and a scan submitted meanwhile waits in the queue.

**Failure handling.** A scan that fails -- a scanner error, an upload error, an aborted flip -- fails its job and the worker moves on to the next one. A failure of the worker's own job-store writes, or of its hourly history prune, is different: three in a row mark the worker degraded. While degraded, `/health` returns `503` with `job store failing` and new scans are refused, because a job whose outcome cannot be recorded should not ask anyone to feed paper. Whenever the worker has no job, every 5 seconds it retries any job-failure records it could not write earlier, degraded or not -- including the rejection of a refused scan that the web request could not record -- so a job the store briefly refused to end still ends once writes succeed again. Degraded is not permanent either: while degraded, the worker also probes the job store on those same ticks and clears degraded on the first probe that succeeds.

**Shutdown.** Stopping sets a stop flag and shuts the queue down, so jobs still waiting are dropped rather than run, and a job parked at the flip prompt is answered with Abort. The server then waits at most 5 seconds for the worker thread to finish. A scan still inside the scanner at that point is abandoned: its job stays active and the next startup records it as "The server restarted before this scan finished", as it does for the dropped queued jobs. The job store and the paperless-ngx client are closed only once the worker thread has confirmed it stopped, so a thread still finishing a scan never writes to a closed database.

For manual duplex scanning, the pipeline waits between pass A (fronts) and pass B (backs) through a `FlipCoordinator`, a small interface with one method, `wait_for_flip(timeout)`, that returns one of three outcomes: continued, aborted or timed out. The pipeline does not know who answers. In the web server, the worker's coordinator is answered by the `/api/flip/continue` and `/api/flip/abort` routes, which the flip prompt's Continue and Abort scan buttons call. In the CLI, a coordinator asks the operator a yes/no question at the terminal. Whichever answer arrives first -- including the timeout -- is final, and later answers are dropped. In the web server the worker's coordinator is bound to its job and accepts an answer only once that job is waiting at the flip prompt, and the routes name the job they answer, so a stale or repeated click is dropped instead of reaching another job.

The wait is bounded by `flip_timeout_seconds` (600 seconds by default), so an abandoned flip prompt cannot hold the worker thread forever: when the timeout elapses the job fails and the next queued job runs. The scanner itself is not what the timeout frees. The backend opens and closes the device inside each `scan_pages()` call, so the scanner handle is already released between the two passes; what the timeout releases is the worker thread.

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

PicoCSS provides classless styling with automatic dark/light mode support. There is no application JavaScript. The server renders the Scan button, and every status response -- a scan submission, the status poll, a flip Continue or Abort -- re-renders it out of band, so its enabled or disabled state always comes from the server's view of the current job. htmx and Pico are served from the package itself, pinned with `integrity` hashes, so loading the web UI needs no internet access.

Every error the web layer produces goes through one rendering rule: the web UI receives a message shown in the page's message area, and other clients receive a JSON error body with the same status code (see [Errors](../reference/web-api.md#errors)). A middleware in front of every route rejects cross-site state-changing requests (see [Cross-site requests](../reference/web-api.md#cross-site-requests)).

The web UI is a thin layer over the same pipeline that the CLI uses. Both entry points call `run_pipeline()` with identical parameters -- the only difference is how status updates are delivered (HTMX polling vs. terminal output).
