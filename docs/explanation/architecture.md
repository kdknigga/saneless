# Architecture Overview

saneless bridges SANE-compatible network scanners and paperless-ngx. It provides a web UI and CLI for triggering scans, assembling multi-page PDFs, and ingesting documents into paperless-ngx with metadata -- all from any device on the local network.

This page explains *how* saneless is structured and *why* it works the way it does.

## The Scan Pipeline

Every scan job follows the same flow, whether triggered from the web UI or the CLI:

```
Scanner -> [SaneBackend] -> [Page Spool] -> spooled PNGs + ordered page records
        -> [Empty Page Filter] -> [img2pdf + qpdf] -> PDF -> [PaperlessClient] -> paperless-ngx
                                                          \-> [Consume Dir] (fallback)
```

### Scanner Abstraction

The `ScannerBackend` abstract base class isolates SANE behind a clean interface with three methods: `get_devices()` for discovery, `get_capabilities()` for querying available options, and `scan_pages()` for acquiring images. The `SaneBackend` implementation wraps `python-sane` and handles low-level details like device opening, option setting, and the `multi_scan()` iterator for ADF feeders.

This abstraction means the core pipeline never touches SANE directly. If a future version adds support for driverless scanning (eSCL, WSD via `sane-airscan`), only a new backend implementation is needed -- the pipeline, web layer, and configuration remain unchanged.

### Pipeline Orchestration

The `run_pipeline()` function coordinates the full scan flow:

1. **Resolve scanner** -- find the configured device or auto-detect the first available one.
2. **Set parameters** -- apply the selected profile's source, resolution, and color mode.
3. **Acquire pages** -- scan via the scanner backend. Each page is written to the job's page spool as it arrives, as a PNG file with an ordered page record, rather than accumulated in memory: the backend hands one page to the spool and forgets it. Document order comes from the list of records, never from sorting or globbing the spool directory. For a simplex or hardware duplex profile this is a single `scan_pages()` call. A profile with `duplex = "manual"` splits this stage into three (see [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md#manual-duplex)):
    1. **Pass A** -- scan the front sides through the feeder.
    2. **Flip wait** -- wait, for at most `flip_timeout_seconds`, for the operator to flip the stack and confirm. An abort cancels the job here (it ends `CANCELLED`) and a timeout fails it, both before pass B.
    3. **Pass B** -- scan the back sides, then reverse them and interleave them with the fronts. If the two passes disagree on page count, the fronts and backs are delivered as two separate PDFs instead.
4. **Filter empty pages** -- remove blank pages using the [dual-threshold algorithm](empty-page-detection.md).
5. **Assemble PDF** -- embed the spooled page files into a PDF document.
6. **Upload to paperless-ngx** -- send the PDF with metadata via the REST API, or fall back to the [consume directory](consume-directory-fallback.md) if the API is unavailable.

Because the pages are already on disk, a scan that fails part-way through does not cost the operator the sheets that were already fed. What is kept depends on how the scan ended:

- **A failure after N pages** -- a scanner fault, a page timeout, the feeder page cap, or the spool running out of room -- keeps those N pages as a partial PDF in `failed/` inside the data directory, and the error names the count and the path. It is never uploaded, and blank pages are not removed from it.
- **A manual duplex scan whose pass B or flip fails** keeps the fronts pass A already scanned (and any backs pass B managed) as separately named PDFs in the same place. That covers a fault during pass B, an empty pass B, a flip wait that timed out, and a flip prompt that could not be read.
- **A failure to assemble the PDF at all** keeps the spooled page files themselves, moved into a job-keyed directory under `failed/`, named in the error.
- **An operator's cancel keeps nothing.** Aborting at the flip prompt, answering no, or Ctrl-C is a decision to stop, and saneless never prunes `failed/`, so a cancel that preserved pages would leave the operator files to clean up after choosing not to scan.

Each stage emits a `PipelineEvent` (`SCANNING`, `AWAITING_FLIP`, `SCANNING_REVERSE`, `ASSEMBLING`, `UPLOADING`, `DONE`). `AWAITING_FLIP` marks the start of the flip wait and `SCANNING_REVERSE` the start of pass B, and each becomes a job state of the same name. The web UI uses these events to update the live status indicator via HTMX polling.

### PDF Assembly

PDF assembly uses `img2pdf`, which embeds each spooled PNG directly into the PDF without re-encoding it. This is lossless: the page is encoded once, as the PNG the spool wrote, and that PNG's image data is what the PDF carries. It is not byte-identical to the raw data the scanner sent over the wire -- that data was never stored -- but no pixel changes between the spooled page and the page in the PDF, and assembly is fast because there is no second compression step.

Assembly converts one page at a time, writing each to its own single-page PDF, and then merges those with qpdf. That is why its memory does not grow with page count: at no point is more than one page's worth of PDF being built. Handing the whole set to `img2pdf` in one call instead would hold every page in memory until the document was finished, which is what a long scan used to do.

### Memory, disk and timeouts

These are the rules that decide how much machine a long scan needs, and how a scan that stalls ends.

- **Roughly one decoded page is in memory at a time while scanning.** A page is handed to the spool and written out as it arrives, so the ceiling is a small constant -- measured at two live page images, the same for a 3-page job as for a 12-page one -- rather than something that grows with the stack. At A4 300 DPI colour a page is about 26 MB, so a few hundred megabytes of RAM is enough for a scan of any length.
- **Disk is checked per page, not just once.** Before each page is written, saneless checks there is room for that page *plus* `min_free_space_mb`, which is held back so PDF assembly still has somewhere to work. A shortfall fails the scan naming the page number and the spool path, and the pages already spooled are kept.
- **One 120-second per-page timeout bounds both paths.** The same limit applies to one sheet through the feeder and to one sheet on the flatbed; it is an internal constant, not a setting. At 300 DPI a page typically takes 10-15 seconds, so it only fires on a scanner that has genuinely stopped answering.
- **On a timeout saneless cancels the read and waits for it to come back before closing the device.** The SANE rule is that no other operation may run while one is outstanding, so closing a device that is still mid-read is unsafe. saneless cancels, waits up to ten seconds, and closes only if the read returned. A page that arrives after the cancel is discarded rather than added to the document, because the failure has already been reported.
- **A read that never comes back does not block shutdown.** Every blocking scanner call runs on a daemon thread, so `saneless serve` still stops on Ctrl-C, a container still stops on `docker stop`, and nothing hangs waiting for a scanner that has gone away.
- **While such a read is outstanding, the next scan is refused.** saneless will not start another scan on a device it cannot safely close, so it refuses at once with a message asking you to restart saneless, without touching the scanner. If the stuck read does eventually return, the handle is closed and saneless recovers on its own -- a restart is the cure for a hang that never clears, not the only way out of a slow one.

### Paperless Upload

The `PaperlessClient` sends the PDF via multipart POST to the paperless-ngx REST API (`/api/documents/post_document/`) with metadata including title, tags, correspondent, and creation date. After upload, it polls the task endpoint with exponential backoff until paperless-ngx reports success or failure.

If a consume directory is configured, the PDF is deposited there as a fallback when the upload cannot get through: after the retries for a network error, a timeout or a server error (5xx), and at once, without retrying, for a `paperless.url` with no usable `http://` or `https://` scheme. An upload paperless-ngx rejects (a 4xx, such as an authentication failure) or redirects (a 3xx, usually a `paperless.url` pointing at the wrong address) never falls back: the scan fails with Paperless's reason or the redirect target. See [How Consume Directory Fallback Works](consume-directory-fallback.md) for details.

## Worker Thread Model

The web server runs a background worker thread that processes one scan job at a time. Jobs are submitted to a bounded `queue.Queue` and executed sequentially -- scanning is inherently serial because a physical scanner can only handle one job at a time. The queue holds at most 10 jobs waiting to start. Submitting never blocks: a scan submitted to a full queue is refused with `429`, and one submitted while the worker is down or degraded is refused with `503` (see the [Web API reference](../reference/web-api.md#post-apiscan)).

The worker communicates progress back to the web layer via state transitions on the `Job` object stored in SQLite. The UI polls the job status endpoint, and HTMX updates the status indicator when the state changes. The scan itself never runs on a request: it runs on the worker thread. The route handlers are plain (non-`async`) functions, which FastAPI runs on its threadpool, so a slow paperless-ngx call or a database call in one request does not stall `/health`, the status poll or any other request.

**Startup.** Before the worker thread starts, the app fails every job that a previous process left active (running or still queued) with "The server restarted before this scan finished", then prunes job history. A queued job is never picked up again after a restart, so a scan cannot start on whatever paper happens to be in the feeder by then. If the job store cannot be written at this point, the app still starts, but the worker starts degraded and carries out that recovery once the store accepts writes. The worker thread's first act, before it takes any job, is [startup profile generation](../how-to/configure-scan-profiles.md#auto-generated-profiles): the server is already answering requests while it runs, and a scan submitted meanwhile waits in the queue.

**Failure handling.** A scan that fails -- a scanner error, an upload error, a flip wait that timed out -- fails its job and the worker moves on to the next one. A scan that ends without delivering a document ends in one of three ways: an operator's abort at the flip prompt is recorded `CANCELLED` and logged at INFO, with no traceback, because nothing went wrong; a failure is recorded `ERROR` and logged with its traceback; and a shutdown during a flip wait is recorded as a restart (`ERROR`, "The server restarted before this scan finished"). The terminal job states are `DONE`, `FALLBACK`, `ERROR` and `CANCELLED`. A failure of the worker's own job-store writes, or of its hourly history prune, is different: three in a row mark the worker degraded. While degraded, `/health` returns `503` with `job store failing` and new scans are refused, because a job whose outcome cannot be recorded should not ask anyone to feed paper. Whenever the worker has no job, every 5 seconds it retries any job records it could not write earlier, degraded or not -- a finished scan's outcome, a job failure, or the rejection of a refused scan that the web request could not record -- so a job the store briefly refused to end still ends once writes succeed again. A scan that reached paperless-ngx is still recorded as done, not as an error, even when recording that outcome failed at first. If those retries keep failing for three idle ticks in a row (about 15 seconds), the worker marks itself degraded as well, so a job store that stays broken shows up on `/health` instead of only in the logs; a store that heals sooner never degrades it. Degraded is not permanent either: while degraded, the worker also probes the job store on those same ticks, and it clears degraded on the first tick on which the probe succeeds and the records it still owes are written.

**Shutdown.** Stopping sets a stop flag and shuts the queue down, so jobs still waiting are dropped rather than run, and a job parked at the flip prompt is answered with Abort. The server then waits at most 5 seconds for the worker thread to finish; before it exits, the thread tries once more to write any job records it still owes. A scan still inside the scanner at that point is abandoned: its job stays active and the next startup records it as "The server restarted before this scan finished", as it does for the dropped queued jobs. The job store, the paperless-ngx client and the scanner are closed only once the worker thread has confirmed it stopped, so a thread still finishing a scan never writes to a closed database.

SANE itself is initialised once per process, at the first scanner backend a command or the server builds, and shut down explicitly when that entry point ends -- at the end of a one-shot CLI command, and at the end of the web server's lifespan, after the worker has confirmed it stopped. It is never torn down from an interpreter-exit hook and never from a request. Shutting SANE down closes every open handle, so it is skipped and logged if a read is still outstanding, for the same reason a single device is not closed mid-read.

For manual duplex scanning, the pipeline waits between pass A (fronts) and pass B (backs) through a `FlipCoordinator`, a small interface with one method, `wait_for_flip(timeout)`, that returns one of three outcomes: continued, aborted or timed out. The pipeline does not know who answers. In the web server, the worker's coordinator is answered by the `/api/flip/continue` and `/api/flip/abort` routes, which the flip prompt's Continue and Abort scan buttons call. In the CLI, a coordinator asks the operator a yes/no question at the terminal. Whichever answer arrives first -- including the timeout -- is final, and later answers are dropped. In the web server the worker's coordinator is bound to its job and accepts an answer only once that job is waiting at the flip prompt, and the routes name the job they answer, so a stale or repeated click is dropped instead of reaching another job.

The wait is bounded by `flip_timeout_seconds` (600 seconds by default), so an abandoned flip prompt cannot hold the worker thread forever: when the timeout elapses the job fails and the next queued job runs. The scanner itself is not what the timeout frees. The backend opens and closes the device inside each `scan_pages()` call, so the scanner handle is already released between the two passes; what the timeout releases is the worker thread. The one exception is a pass A whose read was left outstanding after a timeout: that handle is deliberately not closed, and the flip prompt is never reached, because the scan has already failed (see [Memory, disk and timeouts](#memory-disk-and-timeouts)).

## Job Storage

Job records live in a SQLite database under the configured `data_dir` -- durable state, deliberately not the disposable `tmp_dir` the scan in progress is built in -- and a single `JobStore` instance owns the only connection to it. Nothing else in the process opens that file. Centralising the connection is what makes the rest of this section possible: there is exactly one place a transaction can begin, one place the schema can change, and one lock to take. The connection enables write-ahead logging before it switches to explicit transaction control, because SQLite refuses a journal-mode change once a transaction is open.

That single connection is driven by two kinds of caller. Web request threads read and write job rows while serving HTTP, and the background worker thread writes state transitions as a scan progresses. A `sqlite3` connection is not safe to drive from several threads at once, so every public `JobStore` method is serialised on a `threading.RLock` and each method opens its own transaction inside that lock. The lock guards the connection, not merely the data: without it, two threads can interleave statements inside one another's transaction and commit a mixture of both. Public methods deliberately never call other public methods, because `sqlite3` transaction contexts do not nest -- an inner one commits the outer -- and that rule is enforced by a test rather than left to convention.

The schema is versioned with `PRAGMA user_version` and evolved by an ordered ladder of migration steps, where step *N* produces version *N*. A fresh database reports version 0 with no `jobs` table, so the ladder runs from the start and builds the schema from nothing. A database written by an earlier release also reports version 0 but already has a `jobs` table, so it is taken to be at version 1 and joins the ladder partway. Each step stamps its version and commits before the next begins, so a ladder that fails at step *N* leaves a valid database at version *N*-1 rather than a half-applied one.

If a database does not have the shape the next step expects, the store raises `StorageError` naming the file and the columns it could not find, and closes the connection rather than opening. The loud refusal is the point. What this replaced was an `ALTER TABLE` wrapped in a bare `except: pass`, which made an unrecognised database look like a clean startup and then failed at the first write -- far from the cause, with an error that said nothing about the schema. Failing at open time puts the report where the problem is, and because the check runs before the first `ALTER`, a rejected database is left exactly as it was found and can still be inspected or restored.

Retention is a single `DELETE` that removes jobs past an age cutoff together with jobs outside a row cap, and it reports its count from that one statement rather than from reads taken either side of it, so a job inserted mid-prune cannot make the count wrong. Both limits are settings -- see `history_retention_days` and `history_max_rows` in the [Configuration Reference](../reference/configuration.md). Both select on creation time and neither consults whether the job finished, so retention removes in-flight and errored records on exactly the same terms as completed ones.

Six columns -- `outcome`, `pages_scanned`, `pages_removed`, `pages_uploaded`, `warning` and `owner_token` -- were added in a single migration ahead of the work that would use them, so that no later feature had to migrate the database again for one field. All six are now written:

- `pages_scanned`, `pages_removed` and `pages_uploaded` are written by the worker on the success path, and rendered as one sentence in the status area and as a second line in the history table's Title cell.
- `outcome` records how a finished scan resolved -- `SUCCESS` or `FALLBACK`, distinguishing a document that went through the API from one left in the consume directory. There is no `FAILED` member and there will not be one: a failure raises, so the column stays NULL on the error path and `ERROR` carries that fact alone rather than in two columns that could disagree. `warning` carries the sentence the status area prints beneath the status line, such as what a fallback cost in metadata. `saneless jobs --json` reports both.
- `owner_token` is written by the web layer when a scan is submitted, and identifies the browser that started it. It is what lets the flip prompt be shown to the person holding the paper and not to everyone with the page open. It is **not** a credential and grants nothing: a job predating this column reads back NULL, which means "nobody owns it", and the prompt is then shown to everyone -- otherwise an upgrade mid-scan would leave a job nobody could continue.

Every one of them can still read back unset. A job that never reached the success path has no page counts, and a job submitted before its column existed has no owner. Anything rendering them has to handle NULL, and the UI does: a job with no counts simply shows no counts line.

## Configuration Layer

saneless uses `pydantic-settings` to load configuration from a TOML file with environment variable overrides. This gives operators two ways to configure the application:

- **TOML file** (`saneless.toml`) for persistent settings on bare-metal installs.
- **Environment variables** (prefixed `SANELESS_`, nested with `__`) for container deployments where secrets should not be baked into the image.

Configuration search paths follow XDG conventions, and the file is named `saneless.toml` in every one of them: `./saneless.toml`, `$XDG_CONFIG_HOME/saneless/saneless.toml` (default `~/.config/saneless/saneless.toml`), and `/etc/saneless/saneless.toml`.

The **profile system** stores named scanner presets (source, resolution, color mode, default metadata) so users can switch between common scan types without reconfiguring each time. Profiles are defined in the TOML file under `[profiles.<name>]` sections.

## Web Layer

The web layer is built with FastAPI, Jinja2 templates, and HTMX for reactivity. There is no JavaScript framework and no JS build step -- the entire frontend is server-rendered HTML with declarative HTMX attributes for dynamic updates.

PicoCSS provides classless styling with automatic dark/light mode support. There is no application JavaScript. The server renders the Scan button, and every status response -- a scan submission, the status poll, a flip Continue or Abort -- re-renders it out of band, so its enabled or disabled state always comes from the server's view of the current job. htmx and Pico are served from the package itself, pinned with `integrity` hashes, so loading the web UI needs no internet access.

Every error the web layer produces goes through one rendering rule: the web UI receives a message shown in the page's message area, and other clients receive a JSON error body with the same status code (see [Errors](../reference/web-api.md#errors)). A middleware in front of every route rejects cross-site state-changing requests (see [Cross-site requests](../reference/web-api.md#cross-site-requests)).

The web UI is a thin layer over the same pipeline that the CLI uses. Both entry points call `run_pipeline()` with identical parameters -- the only difference is how status updates are delivered (HTMX polling vs. terminal output).
