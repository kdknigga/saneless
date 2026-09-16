---
phase: 29
slug: geometry-memory-and-timeouts
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-09-15
---

# Phase 29 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `29-RESEARCH.md` § Validation Architecture.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 + pytest-timeout 2.4.0 |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` (`timeout = 60`, `timeout_method = "signal"`, `filterwarnings = ["error"]`, `xfail_strict`) |
| **Quick run command** | `uv run pytest tests/test_scanner.py tests/test_spool.py -x -q` |
| **Full suite command** | `uv run pytest -m "not browser and not sane_hardware"` |
| **Hardware suite** | `uv run pytest -m sane_hardware` |
| **Estimated runtime** | quick ~15 s · full ~3 min |

---

## Sampling Rate

- **After every task commit:** `uv run pytest {files the task touches} -x -q`
- **After every plan wave:** `uv run pytest -m "not browser and not sane_hardware"` plus
  `uv run ruff check . && uv run ty check && uv run pyrefly check src tests`
- **Before `/gsd-verify-work`:** full suite green, plus `-m sane_hardware` and `-m browser`
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

Filled in by the planner as tasks are written. Every row below is a behaviour the phase must
prove; the planner maps each to the task that delivers it.

| Criterion / Req | Behaviour | Test Type | Automated Command | Plan | File Exists | Status |
|---|---|---|---|---|---|---|
| SC1 / HARD-01 | 12-page scan yields records `sequence` 1..12 and a PDF whose pages carry per-page content in order (pikepdf read-back) | integration | `uv run pytest tests/test_pipeline.py -k twelve_page_order -x` | 29-05 T3 | ✅ exists | ✅ passing |
| SC1 / HARD-01 | duplex interleave reorders **records** while spool file names sort differently | unit | `uv run pytest tests/test_pipeline.py -k interleave_records -x` | 29-05 T3 | ✅ exists | ✅ passing |
| SC1 / HARD-01 | live-image high-water ≤ 2 and identical for 3 and 12 pages | unit | `uv run pytest tests/test_scanner.py -k live_page_images -x` | 29-04 T1+T3 | ✅ exists | ✅ passing |
| SC1 / HARD-01 | one decoded page per sink call; the backend holds no page list | unit | `uv run pytest tests/test_scanner.py -k sink -x` | 29-04 T3 | ✅ exists | ✅ passing |
| SC1 / D-03 | assembly peak memory is flat in page count (per-page convert + qpdf merge), and the merged PDF matches a single-convert PDF page-for-page | integration | `uv run pytest tests/test_pdf.py -k merge -x` | 29-08 T1+T2 | ✅ exists | ✅ passing |
| SC2 / HARD-02 | error on page N+1 → job ERROR, original exception type, message names count and `failed/` path, N pages in the partial PDF | integration | `uv run pytest tests/test_outcomes_e2e.py -k partial_scan_preserved -x` | 29-09 T1+T3 | ✅ exists | ✅ passing |
| SC2 / HARD-02 | pass-B failure preserves fronts as `(fronts)` partial; flip timeout likewise | integration | `uv run pytest tests/test_pipeline.py -k pass_b_preserves_fronts -x` | 29-09 T2 | ✅ exists | ✅ passing |
| SC2 / HARD-02 | a cancel preserves **nothing** | integration | `uv run pytest tests/test_pipeline.py -k cancel_preserves_nothing -x` | 29-09 T1 | ✅ exists | ✅ passing |
| SC2 / D-10 | `PdfError` moves page files to `failed/<job>/`; the growth warning counts them | unit | `uv run pytest tests/test_pipeline.py -k failed_dir_counts_directories -x` | 29-09 T3 | ✅ exists | ✅ passing |
| SC3 / HARD-03 | `cancel()` called, `close()` **not** called while the read is blocked, `close()` called once it returns | unit | `uv run pytest tests/test_scanner.py -k close_not_called_while_blocked -x` | 29-07 T1+T2 | ✅ exists | ✅ passing |
| SC3 / HARD-03 | never-returning read → CRITICAL log, no `close()`, `ScanError` naming the unresponsive cancel | unit | `uv run pytest tests/test_scanner.py -k did_not_respond_to_cancel -x` | 29-07 T2 | ✅ exists | ✅ passing |
| SC3 / HARD-03 | the process still exits with a stuck read (subprocess, `subprocess.run(timeout=20)`) | integration | `uv run pytest tests/test_scanner.py -k process_exits -x` | 29-07 T3 | ✅ exists | ✅ passing |
| SC3 / D-12 | a page returned after the cancel is discarded, never spooled | unit | `uv run pytest tests/test_scanner.py -k post_cancel_page_discarded -x` | 29-07 T2 | ✅ exists | ✅ passing |
| SC3 / D-13 | a wedged backend refuses the next `scan_pages`/`get_capabilities` with no SANE call, and recovers when the read returns | unit | `uv run pytest tests/test_scanner.py -k wedge -x` | 29-07 T2 | ✅ exists | ✅ passing |
| SC3 / D-15 | `KeyboardInterrupt` mid-read cancels, waits, closes, re-raises; CLI still exits 130 | unit | `uv run pytest tests/test_scanner.py -k keyboard_interrupt_mid_read -x` | 29-07 T3 | ✅ exists | ✅ passing |
| SC4 / HARD-04 | flatbed `start()+snap()` times out with the ADF path's message shape | unit | `uv run pytest tests/test_scanner.py -k flatbed_timeout -x` | 29-07 T2 | ✅ exists | ✅ passing |
| SC4 / HARD-04 | flatbed validation is fatal and shares `_validate_page_image` | unit | `uv run pytest tests/test_scanner.py -k flatbed_unreadable -x` | 29-02 T2 / 29-07 T2 | ✅ exists | ✅ passing |
| SC5 / HARD-05 | two `SaneBackend()` constructions → one `init`; differing hosts → WARNING naming both | unit | `uv run pytest tests/test_scanner.py -k init_once -x` | 29-10 T1 | ✅ exists | ✅ passing |
| SC5 / HARD-05 | `TestClient` through startup, every route incl. scan submit + flip, shutdown: `init_call_count == 1` throughout, `exit_call_count` 0→1 only at shutdown | integration | `uv run pytest tests/test_app_lifespan.py -k sane_lifecycle -x` | 29-10 T3 | ✅ exists | ✅ passing |
| SC5 / D-18 | each CLI command closes the backend on success and on error | unit | `uv run pytest tests/test_cli.py -k closes_the_backend -x` | 29-10 T2 | ✅ exists | ✅ passing |
| D-03 / D-07 | per-page disk shortfall → `ScanError` naming page and path; no raw `OSError` | unit | `uv run pytest tests/test_spool.py -x` | 29-01 T2 | ✅ exists | ✅ passing |
| D-03 | `assemble_pdf` does not re-encode a second PNG per page | unit | `uv run pytest tests/test_pdf.py -k no_second_encode -x` | 29-05 T1 | ✅ exists | ✅ passing |
| D-20 | the architecture page states the memory, disk and timeout rules | doc-truth | `uv run pytest tests/test_deployment_config.py -k architecture -x` | 29-11 T3 | ✅ exists | ✅ passing |
| Hardware | cancel unblocks a genuinely slow `test`-backend read; `close()` then succeeds | integration (opt-in) | `uv run pytest -m sane_hardware -k cancel -x` | 29-07 T3 | ✅ exists | ✅ passing |
| Migration gate | the whole tree is green again after the atomic contract switch | gate | `uv run pytest -m "not browser and not sane_hardware" && uv run ty check && uv run pyrefly check src tests` | 29-06 T3 | ✅ exists | ✅ passing |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/test_spool.py` — new file, covers D-02/D-06/D-07
- [x] `tests/fake_sane.py` — Event-gated blocking read (unblock-by-return with a truncated page,
      unblock-by-raise, never-unblock), `close_while_blocked` / `exit_while_blocked` flags,
      weakref issue log for the live-image counter, lazily generated distinct page content
- [x] `tests/conftest.py` — `spooling()` / `spooling_in_turn()` `side_effect` factories, and a
      `StubScannerBackend` base for the existing stub scanner classes
- [x] subprocess child script for the process-exit proof (inline source constant written to
      `tmp_path`)
- [x] no framework install needed

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Behaviour against the operator's real network scanner over `net`/`hpaio` | HARD-03 | The `net` backend's blocking-cancel RPC cannot be reproduced without that hardware; the opt-in `test`-backend suite covers the rest | Run a long ADF scan, pull the network mid-read, confirm the job fails with the timeout message and `saneless serve` still stops on Ctrl-C / `docker stop` |

Everything else has automated verification, including the real-libsane cancel path behind the
`sane_hardware` marker.

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 30s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** complete — every row above was re-run at plan 29-11's HEAD and observed passing.
Phase gate at the same commit: 2033 passed (not browser, not sane_hardware), 68 browser, 6
sane_hardware, `ruff check` and `ruff format --check` clean, `ty check` and
`pyrefly check src tests` clean, both `prek` stages clean.

The one manual-only verification above stands: it needs the operator's own network scanner over
`net`/`hpaio`, which cannot be stubbed. Everything else in this phase, including the real-libsane
cancel sequence and the browser UI, is automated.
