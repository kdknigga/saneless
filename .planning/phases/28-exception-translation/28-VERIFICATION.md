---
phase: 28-exception-translation
verified: 2026-09-15T21:50:12Z
status: passed
score: 9/9 must-haves verified
overrides_applied: 0
---

# Phase 28: Exception Translation Verification Report

**Phase Goal:** No third-party exception type escapes a module boundary and no user ever sees a
traceback — SANE, httpx, all seven img2pdf error classes, and tomllib errors are wrapped at their
call sites with their original messages, and the CLI prints one line with a non-zero exit code —
with the troubleshooting docs updated in-phase

**Verified:** 2026-09-15T21:50:12Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A parametrised test per third-party library proves each boundary raises the saneless exception type carrying the original message, never the third-party type | ✓ VERIFIED | `tests/test_pdf.py::TestPdfBoundary` parametrises all 7 `IMG2PDF_ERROR_CLASSES` (`AlphaChannelError`, `ExifOrientationError`, `ImageOpenError`, `JpegColorspaceError`, `NegativeDimensionError`, `PdfTooLargeError`, `UnsupportedColorspaceError`) plus untyped `Exception`/`TypeError`/`ValueError`, each asserting `PdfError`, message substring and `__cause__`. `tests/test_scanner.py::TestSaneBoundary` covers `require_sane()`, `sane.open`, `get_devices`, `get_options`, option assignment and `start()/snap()` failures as `ScanError`/`ConfigError` with `__cause__`. `tests/test_paperless.py` parametrises `_TRANSIENT_TRANSPORT_CASES` / `_POLL_TRANSPORT_CASES` (httpx `ConnectError`, `ReadError`, `ReadTimeout`, `RemoteProtocolError`, `DecodingError`, etc.) against `PaperlessError`. `tests/test_config.py` covers `tomllib.TOMLDecodeError` → `ConfigError` with line/column, chained via `__cause__`. All ran green (`pytest -m "not browser and not sane_hardware"`: 1930 passed). |
| 2 | `saneless scan` against a bad config, a broken scanner, an unreachable Paperless, and an unassemblable PDF each print one line and exit non-zero; a missing `python-sane` import prints an install hint | ✓ VERIFIED | `tests/test_cli.py::TestExitCodes` — `test_bad_config_exits_2_with_header_and_one_problem_line` (exit 2), `test_broken_scanner_exits_1_with_one_line` (exit 1), `test_unreachable_paperless_exits_3_with_one_line` (exit 3), `test_unassemblable_pdf_exits_4_with_one_line` (exit 4); each asserts `"Traceback" not in result.output`. `test_require_sane_missing_module_raises_config_error` / CLI-level tests assert the install hint (`libsane-dev`, `sane-backends-devel`, `Install on Bare Metal`) and exit 2. Ran green (`TestExitCodes`: 23 passed). |
| 3 | A scan that produces zero pages says "No pages were scanned", and says "All pages were blank" only when detection actually removed them — never a bare `ValueError` | ✓ VERIFIED | `src/saneless/pipeline.py:402-425` `_require_pages` raises `ScanError("No pages were scanned")` before `assemble_pdf` can be reached; line 596 raises `"All pages were blank"` only when detection removed a non-empty batch. Called from `_scan_simplex`, `_scan_manual_duplex` (front/back), matching plan 28-03 must-haves. `assemble_pdf([])` also independently refused in `pdf.py` (plan 28-04) as defence in depth. Covered by `tests/test_pipeline.py`, `tests/test_outcomes_e2e.py`, `tests/test_pdf.py`. |
| 4 | A user abort at the flip prompt is recorded as a cancelled job, not a scanner failure, and every job failure is logged with `exc_info` | ✓ VERIFIED | `src/saneless/vocabulary.py` `JobState.CANCELLED` is a distinct terminal state. `src/saneless/worker.py::_scan_job` has three explicit branches: shutdown-claimed (`_ended_by_shutdown`) → ERROR/RESTART_REASON/INFO; `ScanCancelledError` → `JobState.CANCELLED`, `logger.info`, no `exc_info`; everything else → ERROR with `classify_error` category and `logger.exception(...)` (carries `exc_info`). All other job-failure logging sites (`_best_effort_fail` path, idle-prune, auto-profiles startup) also use `logger.exception`. CLI: `ClickFlipCoordinator`/`FlipCoordinator.abort_cause` (pipeline.py) distinguish a real abort (`ScanCancelledError`, exit 130) from a broken prompt or timeout (`ScanError`, exit 1). Verified by `tests/test_worker.py` (shutdown/cancel/failure branch tests, 5 passed) and `tests/test_cli.py` (`test_scan_cancelled_exits_130_with_one_line`). |

**Score:** 4/4 roadmap success criteria verified

### Required Artifacts (PLAN must_haves, sampled across all 14 plans)

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/exceptions.py` | `PdfError`, `ScanCancelledError` direct `SanelessError` subclasses; `describe()` | ✓ VERIFIED | Class hierarchy confirmed: `ConfigError`, `ScanError`→`FeederEmptyError`, `ScanCancelledError`, `PdfError`, `PaperlessError`→`PaperlessTimeoutError`, `StorageError` all direct/indirect `SanelessError` subclasses per D-01/D-04 discretion (cancel and PDF are NOT `ScanError` subclasses). |
| `src/saneless/vocabulary.py` | `JobState.CANCELLED`, `ErrorCategory.ASSEMBLY`, `ExitCode`, `exit_code_for`, `classify_error` | ✓ VERIFIED | `ExitCode(IntEnum)` has exactly SUCCESS=0, SCAN=1, CONFIG=2, PAPERLESS=3, PDF=4, UNEXPECTED=5, CANCELLED=130. `exit_code_for` is a total `match`+`assert_never`. `classify_error` keeps `StorageError`/`ScanCancelledError` as `UNKNOWN` per D-07 amendment; CLI guard maps `StorageError` to exit 2 by type. |
| `src/saneless/cli.py` | `_GuardedGroup` guard, ExitCode-only exits | ✓ VERIFIED | `_GuardedGroup.invoke` implements the documented clause order: click control-flow → KeyboardInterrupt/ScanCancelledError (130) → StorageError (2) → SanelessError (classified) → Exception (5, `_report_unexpected`). |
| `src/saneless/logging_config.py` | stderr fallback never renders a traceback without `-v` | ✓ VERIFIED | `_TracebackFreeFormatter` strips `exc_info`/`exc_text`/`stack_info` before rendering when the file handler could not attach; `configure_logging` returns whether the file handler attached, consumed by `cli.py` to build the "Full details in <log_file>" / "-v" hint. |
| `src/saneless/paperless.py` | ctor InvalidURL wrap, widened retry set, one body renderer, poll survives transport errors | ✓ VERIFIED | `upload_document` retries only `response.is_server_error` (WR-03 fix); `UnsupportedProtocol` fast-fails and logs before falling back (WR-04 fix); `poll_task` catches `httpx.RequestError` (covers `DecodingError`, WR-05 fix) and resets `last_transport_error = None` on a successful response (IN-02 fix); `_post_document` wraps `pdf_path.open` `OSError` (IN-08 fix). |
| `src/saneless/pdf.py` | boundary catch → `PdfError`; empty-list refusal | ✓ VERIFIED | `TestPdfBoundary` parametrised over all 7 img2pdf classes + untyped raises + Pillow OSError, all chained via `__cause__`. |
| `src/saneless/scanner/sane_backend.py` | `require_sane()` and wrapped SANE call sites | ✓ VERIFIED | `require_sane()` not run at import (`test_require_sane_is_not_run_at_import`); wraps `ModuleNotFoundError`/`ImportError` as `ConfigError` with install hint. |
| `src/saneless/worker.py` | three explicit job endings with exc_info failure logging | ✓ VERIFIED | See truth #4 above; `_ended_by_shutdown` correctly narrows the shutdown branch to only `ScanCancelledError` instances, fixing WR-02. |
| `docs/how-to/troubleshoot-a-failed-scan.md` | symptom-organised troubleshooting how-to, in nav | ✓ VERIFIED | File exists (10.2K), `mkdocs.yml:51` lists it under How-To Guides nav; `uv run mkdocs build --strict` succeeds. |
| `tests/test_deployment_config.py` | exit-code doc-truth tests | ✓ VERIFIED | 6 doc-truth tests pass, pinning documented exit-code tables to `ExitCode`. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `cli.py _GuardedGroup.invoke` | `exceptions.StorageError` | isinstance clause before `SanelessError` | ✓ WIRED | Confirmed in source; exits `ExitCode.CONFIG`. |
| `worker.py _scan_job` | `exceptions.ScanCancelledError` | isinstance branch after shutdown check | ✓ WIRED | Confirmed; shutdown check (`_ended_by_shutdown`) evaluated first, cancel second, else-failure third. |
| `worker.py _scan_job` failure branch | `logging exc_info` | `logger.exception(...)` inside except block | ✓ WIRED | Confirmed at `worker.py:1391`. |
| `pipeline.py _scan_manual_duplex` | `FlipCoordinator.abort_cause` | `case FlipOutcome.ABORTED` | ✓ WIRED | Distinguishes real abort (`ScanCancelledError`) from broken-prompt cause (`ScanError`). |
| `paperless.py upload_document` | `_not_accepted_message` | 4xx/3xx/1xx branch (`not is_server_error`) | ✓ WIRED | Confirmed; only ≥500 retried. |
| `paperless.py poll_task` | monotonic deadline | `except httpx.RequestError as exc` then fall through to sleep/deadline check | ✓ WIRED | Confirmed; every loop path reaches the deadline check. |

### Requirements Coverage

| Requirement | Source Plan(s) | Description | Status | Evidence |
|-------------|----------------|-------------|--------|----------|
| EXC-01 | 28-01,02,04,05,06,09,10,14 | Every SANE, httpx, img2pdf (7 classes), tomllib exception caught and re-raised as saneless type with original message | ✓ SATISFIED | See truth #1, and boundary code in pdf.py/sane_backend.py/paperless.py/config.py/auto_profiles.py |
| EXC-02 | 28-01,02,05,09,11,12,13 | CLI catches ConfigError/ScanError/PaperlessError/PdfError, prints one line + non-zero exit; missing python-sane prints install hint | ✓ SATISFIED | See truth #2, `_GuardedGroup`, `require_sane()` |
| EXC-03 | 28-03,04,14 | Zero pages → "No pages were scanned"; "All pages were blank" only when detection removed pages; never bare ValueError | ✓ SATISFIED | See truth #3, `_require_pages` |
| EXC-04 | 28-01,07,08,11,14 | User abort at flip prompt recorded as cancelled job, not scanner failure | ✓ SATISFIED | See truth #4, `JobState.CANCELLED`, worker three-branch logic |
| EXC-05 | 28-07 | Worker logs every job failure with exc_info | ✓ SATISFIED | See truth #4, `logger.exception` at every failure site |

**Note:** `.planning/REQUIREMENTS.md` lines 113-117 still show EXC-01..EXC-05 as `[ ]` unchecked and the traceability table (lines 298-302) shows "Pending" rather than "Complete", unlike prior completed phases (e.g. CFG-10/CFG-11 are `[x]` / "Complete"). This is a documentation bookkeeping gap in `REQUIREMENTS.md` itself — the underlying code and tests fully satisfy all five requirements, and `ROADMAP.md` already marks Phase 28 `[x]` complete. Flagged as informational; does not block the phase goal.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| — | — | No `TBD`/`FIXME`/`XXX`/`TODO`/`HACK`/`PLACEHOLDER` markers found in any file touched by the phase diff (`45d1aaf..HEAD`) | — | None |

### Code Review Fix Verification (independent re-check, not trusted from REVIEW-FIX.md)

All 17 findings from `28-REVIEW.md` (CR-01, WR-01..08, IN-01..08) were fixed per `28-REVIEW-FIX.md`. The five items the fixer flagged "requires human verification" were independently re-verified against source and by running their targeted tests (not just trusting the narrative):

| Finding | Independent check | Result |
|---------|-------------------|--------|
| CR-01 (traceback without `-v`) | Read `logging_config.py` `_TracebackFreeFormatter` and `cli.py` `_log_failure`/`_report_unexpected`; ran `pytest -k "traceback" tests/test_cli.py tests/test_logging.py` | ✓ 9 passed. Stderr fallback handler strips `exc_info`/`exc_text`/`stack_info` before formatting; traceback only reaches stderr via the separate `-v` mirror handler. |
| WR-02 (shutdown masking pass-A failure) | Read `worker.py` `_ended_by_shutdown`, `_scan_job`; ran `pytest -k "shutdown or pass_a or restart" tests/test_worker.py` | ✓ 5 passed. Shutdown branch requires `isinstance(exc, ScanCancelledError)`, so a jam/`ScanError` during pass A after `stop()` correctly falls to the ERROR branch with `exc_info`. |
| WR-03 (3xx/1xx retried as transient) | Read `paperless.py:550-590`; ran `pytest -k "redirect or 3xx or status_code" tests/test_paperless.py` | ✓ Included in 16 passed. Only `exc.response.is_server_error` (≥500) is retried; anything else raises `PaperlessError` immediately via `_not_accepted_message`. |
| WR-05 (`DecodingError` escapes `poll_task`) | Read `paperless.py:844-889`; ran `pytest -k "decoding or DecodingError" tests/test_paperless.py` | ✓ Included in 16 passed. Poll loop catches `httpx.RequestError` (base of both `TransportError` and `DecodingError`), so no httpx type escapes. |
| IN-02 (stale transport error in timeout message) | Read `paperless.py:866-869`; ran `pytest -k "last_transport or stale" tests/test_paperless.py` | ✓ Included in 16 passed. `last_transport_error = None` set in the success (`else:`) branch before the deadline check. |

No discrepancies found between the REVIEW-FIX.md narrative and the actual code/tests for any of the five flagged items.

### Independent Gate Re-Run

| Check | Command | Result |
|-------|---------|--------|
| Non-browser/hardware suite | `uv run pytest -m "not browser and not sane_hardware"` | 1930 passed |
| Browser + SANE hardware suite | `uv run pytest -m "browser or sane_hardware"` | 73 passed (includes CANCELLED muted-colour/contrast/terminal-state Playwright tests) |
| Lint | `uv run ruff check .` | No issues found |
| Format | `uv run ruff format --check .` | Clean |
| Type check (ty) | `uv run ty check` | All checks passed |
| Type check (pyrefly) | `uv run pyrefly check src tests` | 0 errors |
| Docs build | `uv run mkdocs build --strict` | Succeeds |

### Human Verification Required

None. Per project CLAUDE.md, browser-based checks (CANCELLED styling, contrast, terminal polling behaviour) are automated via `pytest -m browser` and were independently re-run above (73 passed), not deferred to a human. No physical-hardware-only or unstubbed external-service items remain in this phase's scope.

### Gaps Summary

No gaps found. All four ROADMAP success criteria, all five EXC-01..EXC-05 requirements, and all 14 plans' must-haves are verified directly against the codebase and passing tests — not merely SUMMARY.md claims. The five review findings the fixer marked "requires human verification" were independently re-derived from source code and re-tested rather than trusted at face value; all five hold up. The only non-blocking observation is that `.planning/REQUIREMENTS.md`'s own checkbox/traceability rows for EXC-01..EXC-05 were not flipped to `[x]`/"Complete" (a bookkeeping omission, not a functional gap).

---

_Verified: 2026-09-15T21:50:12Z_
_Verifier: Claude (gsd-verifier)_
