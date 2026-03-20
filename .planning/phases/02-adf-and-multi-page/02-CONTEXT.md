# Phase 2: ADF and Multi-Page - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

Multi-page document scanning from ADF in all modes: simplex (single-sided ADF), hardware duplex (native two-sided), and manual duplex (two-pass with flip prompt). Includes automatic empty page detection and removal, first-page thumbnail generation, and specific empty-feeder error handling. Web UI, packaging, and consume directory fallback are separate phases.

</domain>

<decisions>
## Implementation Decisions

### ADF page acquisition
- Extend existing `scan_pages()` to yield multiple pages via `multi_scan()` when source is ADF — keeps the `ScannerBackend` interface unchanged, source field determines single vs multi-page behavior
- Wrap ADF iteration with a per-page timeout (not per-job) — if a single page takes longer than 2-3x expected duration, cancel the scan
- Validate each scanned image inline before yielding: check nonzero dimensions, minimum file size, not pure white/black
- Discard corrupt/zero-size trailing pages with logging (HP scanners known to feed N+1 pages)
- Use `sane.cancel()` explicitly after the iterator completes or on any error to reset scanner state
- Empty ADF feeder: catch `sane.error` on first `multi_scan()` iteration, inspect error string for out-of-paper indicators, translate to "No paper detected in feeder" user-facing error (SCAN-10)
- Any other `sane.error` caught and reported distinctly, not swallowed or surfaced as generic crash

### Hardware duplex
- Set source to `ADF Duplex` (or device-reported equivalent) — scanner handles two-sided output natively
- Pages arrive already interleaved from hardware — no post-processing interleave needed
- Same per-page timeout and validation as simplex ADF

### Manual duplex coordination
- Two-pass scan coordinated by worker thread blocking on `threading.Event` between passes
- Pass A: acquire all front-facing pages from ADF
- Worker sets job state to `awaiting_flip`, blocks on event
- Web layer detects `awaiting_flip` state, shows flip prompt to user
- User clicks Continue → web layer sets event → worker resumes for pass B
- User clicks Cancel → web layer signals abort → worker cancels cleanly
- Pass B pages stored in reverse order, then interleaved: A1, B1, A2, B2, ...
- Reverse-and-interleave assumes user returns stack with page N on top (natural result of flipping face-down stack)
- Raw page count validation: pass A count must equal pass B count BEFORE empty page detection — mismatch fails with clear error indicating the count difference (SCAN-07)
- Empty page detection applied to BOTH passes AFTER raw count check passes — legitimately blank reverse sides discarded without false mismatch

### Empty page detection
- Dual-threshold algorithm on grayscale conversion: page discarded only if mean luminance > T_mean AND stddev < T_std
- Both thresholds independently configurable per profile in TOML config
- Runs as inline pipeline filter between page acquisition and PDF assembly (not post-processing)
- Log computed values (mean, stddev) for each page with keep/discard decision — helps users tune thresholds
- Convert image to grayscale before computing statistics (operating on color would produce misleading results for pages with faint color tints)

### Thumbnail generation
- Generate JPEG thumbnail after first page acquired, before remaining ADF pages are scanned
- Long edge <= 300px, stored as base64-encoded string on job state record (SCAN-09)
- Served via job status endpoint — no separate image endpoint
- For flatbed scans: thumbnail generated as soon as the single page is acquired (existing pipeline, just add thumbnail step)

### Claude's Discretion
- Default empty page threshold values (T_mean, T_std) — reasonable defaults that work for typical office scanners
- Per-page timeout duration calculation (2-3x expected scan time based on resolution/DPI)
- Pillow MAX_IMAGE_PIXELS setting for high-DPI scans (600 DPI A4 = ~34.8M pixels; 1200 DPI = ~139M pixels)
- EXIF validation/stripping before PDF assembly (per Pitfall #7)
- Internal module organization for new page processing code
- Thumbnail JPEG quality setting
- How `ScanSettings` is extended (additional fields vs separate config)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Product requirements
- `docs/PRD.md` — Full product requirements document
- `docs/PRD.md` §7.3 — Scanning requirements: ADF multi-page, manual duplex two-pass coordination, empty page detection algorithm, thumbnail generation, empty feeder handling
- `docs/PRD.md` §7.2 — Profile configuration: source values (`ADF`, `ADF Duplex`, `ADF Manual Duplex`)
- `docs/PRD.md` §9.4 — TOML configuration schema (profile fields including empty page thresholds)

### Research findings
- `.planning/research/PITFALLS.md` §3 — ADF multi_scan() end-of-feed detection unreliability, per-page timeouts, image validation
- `.planning/research/PITFALLS.md` §5 — Scanner option name variance across backends (source naming differences)
- `.planning/research/PITFALLS.md` §7 — img2pdf seekable input, EXIF pitfalls, Pillow decompression bomb limits
- `.planning/research/PITFALLS.md` §9 — Manual duplex interleaving logic pitfalls (count matching, blank detection ordering)

### Prior phase context
- `.planning/phases/01-core-pipeline/01-CONTEXT.md` — Phase 1 decisions (scanner abstraction, pipeline, config patterns)

### Code conventions
- `.planning/codebase/CONVENTIONS.md` — Naming, style, imports, error handling, docstring patterns

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/saneless/scanner/base.py`: `ScannerBackend` ABC with `scan_pages()` iterator interface — extend for multi-page
- `src/saneless/scanner/base.py`: `ScanSettings` dataclass (source, resolution, mode) — may need additional fields for empty page thresholds
- `src/saneless/scanner/sane_backend.py`: `SaneBackend` with `_open_device()` context manager, device option validation — extend `scan_pages()` for `multi_scan()`
- `src/saneless/pipeline.py`: `run_pipeline()` — insert thumbnail generation and empty page filtering stages
- `src/saneless/worker.py`: `ScanWorker` with queue-based job processing — add `threading.Event` for manual duplex coordination
- `src/saneless/job.py`: `Job` model with `JobState` and `JobStore` — add `awaiting_flip` state and thumbnail field
- `src/saneless/config.py`: `ProfileConfig` — add empty page threshold fields
- `src/saneless/exceptions.py`: `ScanError` — reuse for ADF-specific errors
- `src/saneless/pdf.py`: PDF assembly via img2pdf — already handles page list

### Established Patterns
- Scanner abstraction: ABC interface → concrete SaneBackend implementation
- Device lifecycle: context manager with cancel() + close() on all paths
- Pipeline: scan → assemble → upload with status callbacks
- Config: pydantic-settings with TOML + env var override
- Error handling: custom exception hierarchy (SanelessError → ScanError, ConfigError, PaperlessError)
- Logging: `logging.getLogger(__name__)` at module level

### Integration Points
- `ScannerBackend.scan_pages()` — extend to handle multi-page via `multi_scan()`
- `run_pipeline()` — insert thumbnail + empty page filter between scan and PDF assembly
- `ScanWorker._run()` — add manual duplex event coordination
- `JobState` enum — add `awaiting_flip` state
- `Job` model — add thumbnail field
- `ProfileConfig` — add `empty_page_mean_threshold` and `empty_page_stddev_threshold` fields

</code_context>

<specifics>
## Specific Ideas

No specific requirements — the PRD is highly detailed and prescriptive for this phase. All scanning modes, empty page detection algorithm, thumbnail spec, and manual duplex coordination are fully specified in PRD §7.3.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 02-adf-and-multi-page*
*Context gathered: 2026-03-20*
