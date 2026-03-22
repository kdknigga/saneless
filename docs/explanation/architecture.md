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
