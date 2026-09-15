# Phase 29: Geometry, Memory, and Timeouts - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-09-15
**Phase:** 29-geometry-memory-and-timeouts
**Mode:** `--auto`. No questions were put to the user; each gray area took the recommended option.
**Areas discussed:** Page spool and records, Keeping pages on a mid-batch error, Timeout and cancellation, SANE lifecycle, Documentation

[--auto] Selected all gray areas: Page spool and records, Keeping pages on a mid-batch error, Timeout and cancellation, SANE lifecycle, Documentation.

---

## Page spool and ordered page records (HARD-01)

| Option | Description | Selected |
|--------|-------------|----------|
| Page sink passed to `scan_pages`; `ScanBatch.pages` = ordered record tuple | Backend hands each page to a pipeline-owned spool; the D-12 channel is kept | ✓ |
| Revert `scan_pages` to a generator | Streams naturally, but loses the DPI and rejection-count return value (why Phase 24 left it) | |
| Untyped callback | Minimal, but the type checkers cannot see the contract | |

[auto] Spool — Q: "How do pages leave the backend without accumulating?" → Selected: "Page sink + record tuple" (recommended default)

| Option | Description | Selected |
|--------|-------------|----------|
| PNG spool file fed straight to img2pdf, `outputstream=` | One encode per page, and the PDF streams to disk | ✓ |
| Raw/uncompressed spool, then encode at assembly | Faster write, 2.5x the disk, and a second encode | |

[auto] Spool — Q: "Spool file format?" → Selected: "PNG, embedded as-is" (recommended default)

| Option | Description | Selected |
|--------|-------------|----------|
| Record stores measured grey mean/stddev; pipeline applies thresholds | Facts at the bottom, policy at the top (Phase 24 D-05) | ✓ |
| Record stores an is-blank verdict | Moves policy into the spool | |
| Recompute stats at filter time | A second decode of every page | |

[auto] Spool — Q: "Where do blank-page statistics come from?" → Selected: "Stored stats, verdict in pipeline" (recommended default)

| Option | Description | Selected |
|--------|-------------|----------|
| weakref high-water mark of live page images + pikepdf readback | Deterministic; sees Pillow allocations that tracemalloc does not | ✓ |
| tracemalloc peak | Misses Pillow's C allocations | |
| RSS / `ru_maxrss` | Too noisy for CI | |

[auto] Spool — Q: "How is 'bounded by one page' proven?" → Selected: "weakref live-image count" (recommended default)

Also auto-selected: thumbnail generated when page 1 is spooled (D-05); M-08's cheap wins (D-06); a per-page disk check, with a spool `OSError` becoming a `ScanError` (D-07).

---

## Keeping pages on a mid-batch error (HARD-02)

| Option | Description | Selected |
|--------|-------------|----------|
| Fail the job, preserve the N pages as a partial PDF in `failed/`, and put the count and path in the message | Honest ERROR state, nothing incomplete in Paperless, matches OUTC-04 | ✓ |
| Upload the partial PDF to Paperless with a warning | N-02's alternative; an incomplete document shows as green | |
| Keep the pages only in the spool for the life of the process | Lost on restart; nothing tells the operator where they are | |

[auto] Partial — Q: "What does 'keeps the N pages' mean?" → Selected: "Fail + preserve in failed/" (recommended default)

| Option | Description | Selected |
|--------|-------------|----------|
| Uniform rule: any failure (not a cancel) after ≥1 spooled page preserves; pass-B failures keep fronts (+ partial backs); PdfError preserves the page files | One rule; closes `pipeline.py:1080`'s gap and Phase 28's PdfError deferral | ✓ |
| Scanner errors inside a single pass only | Narrowest reading; fronts are still lost when pass B fails | |

[auto] Partial — Q: "Which endings keep pages?" → Selected: "Uniform rule, cancels excluded" (recommended default)

---

## Timeout and cancellation (HARD-03, HARD-04)

| Option | Description | Selected |
|--------|-------------|----------|
| Fresh daemon thread per blocking acquisition, via one shared helper | A daemon never blocks exit, and a stuck call poisons nothing shared | ✓ |
| One long-lived executor per backend (M-12's sketch) | Its threads are non-daemon, so they still block exit, and one stuck read blocks every later call | |
| Keep the per-call ThreadPoolExecutor | The current bug | |

[auto] Timeout — Q: "Where does the blocking read run?" → Selected: "Daemon thread per acquisition" (recommended default)

| Option | Description | Selected |
|--------|-------------|----------|
| cancel → bounded grace wait → close only if the read returned; otherwise leave the handle, mark the backend wedged, refuse scans until the late read returns | Follows the SANE rule and refuses honestly | ✓ |
| cancel → close immediately | Today's close-while-reading (M-12) | |
| cancel → wait forever | Wedges the worker thread | |

[auto] Timeout — Q: "What happens after a timeout fires?" → Selected: "Cancel, wait, close-or-wedge" (recommended default)

| Option | Description | Selected |
|--------|-------------|----------|
| One 120 s constant for flatbed and ADF, no config key | Parity by construction; no new surface | ✓ |
| A configurable `scanner.page_timeout_seconds` | New config surface; deferred | |

[auto] Timeout — Q: "Flatbed timeout value and configurability?" → Selected: "Shared constant" (recommended default)

Also auto-selected: Ctrl-C mid-read takes the same cancel path, then re-raises (D-15). Event-gated blocking fake, with a subprocess test proving process exit (D-16).

---

## SANE lifecycle (HARD-05)

| Option | Description | Selected |
|--------|-------------|----------|
| Module-level locked guard; a second host logs a WARNING; resets after exit | Backends stay constructible; the host-ignored case announces itself | ✓ |
| Singleton `SaneBackend` | Harder to test; hides the host conflict | |

[auto] Lifecycle — Q: "Shape of the init guard?" → Selected: "Module-level guard" (recommended default)

| Option | Description | Selected |
|--------|-------------|----------|
| Explicit `close()` at each entry point (CLI command end, serve lifespan after a confirmed worker stop); skipped while wedged | Review principle 7; never races a read | ✓ |
| `atexit` hook | Can run while a daemon reader is inside `sane_read` | |

[auto] Lifecycle — Q: "Where is `sane.exit()` called?" → Selected: "Explicit at entry points" (recommended default)

| Option | Description | Selected |
|--------|-------------|----------|
| Behavioural TestClient test counting init/exit across all routes and the lifespan | Proves the property itself | ✓ |
| Grep or AST test for call sites | Proves only the spelling | |

[auto] Lifecycle — Q: "How is 'never from a request path' proven?" → Selected: "Behavioural count test" (recommended default)

---

## Documentation

| Option | Description | Selected |
|--------|-------------|----------|
| architecture.md: diagram, orchestration, PDF assembly (fix "byte-for-byte"), new "Memory, disk and timeouts" subsection, shutdown; plus troubleshooting, fallback, duplex how-to, `min_free_space_mb`, stale Phase-29 docstrings | Every sentence the phase changes, corrected in-phase | ✓ |
| architecture.md only | Leaves false sentences elsewhere | |

[auto] Docs — Q: "Which docs change in-phase?" → Selected: "Architecture page + every touched sentence" (recommended default)

---

## Scope calls made while analysing

- **M-06 (DPI in PDFs):** already delivered by OUTC-06 and Phase 24 D-12, so not re-opened.
- **Mid-pass abort:** Phase 25 and Phase 28 pointed here, but no requirement maps it. Deferred,
  with the D-11 seam noted.
- **PdfError page preservation:** explicitly deferred to this phase by Phase 28, and folded into
  D-10's uniform rule.

## Claude's Discretion

Names (`spool.py`, `PageSink`, `PageRecord`); PNG compression level; the `_CANCEL_GRACE_SECONDS`
value and the hand-off primitive; the exact live-image bound; a structured `pages_kept`
attribute; how the test_pipeline mocks migrate; exact message wording.

## Deferred Ideas

- Mid-pass abort during pass B
- A configurable page timeout or cancel grace
- Uploading partial scans to Paperless with a warning
- Wedge state on `/health` and the status strip (Phase 30)
- Page counts on failed jobs (Phase 30 APPL-03)
- A Paperless connect timeout separate from the upload timeout
