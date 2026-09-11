---
phase: 23-honest-outcomes-and-never-lose-a-scan
verified: 2026-09-11T00:00:00Z
status: passed
score: 6/6 must-haves verified
overrides_applied: 0
---

# Phase 23: Honest Outcomes and Never Lose a Scan Verification Report

**Phase Goal:** A job's recorded state is always the truth, and a scanned document is never
destroyed by a downstream failure.

**Verified:** 2026-09-11
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (Roadmap Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A Paperless task ending FAILURE, or a poll exceeding its monotonic deadline, records the job FAILED (`JobState.ERROR`) with the Paperless message — never DONE — and a PDF still exists on disk whose path is named in the job error | ✓ VERIFIED | `pipeline.py:279` `_preserving` re-raises with `"{exc}. The scan was preserved at {preserved}"`; `worker.py:277-287` writes `JobState.ERROR` via `finish_job` with `error=str(exc)` on any exception, never `DONE`. Proved end-to-end by `tests/test_outcomes_e2e.py::TestFiveOutcomesEndToEnd` cases `paperless-failure` and `poll-timeout`, which assert `job.state is ERROR`, `job.error` contains the Paperless message (`_FAILURE_TASK`, `"FAILURE"`, `_PAPERLESS_MESSAGE"` / `_PENDING_TASK`, `"did not finish"`) **and** that the preserved PDF's filename is a substring of `job.error` (`_assert_files:544-546`). Both cases pass (`uv run pytest tests/test_outcomes_e2e.py -q` → 5 passed, confirmed independently). Unit-level corroboration: `tests/test_pipeline.py::TestPreservation::test_preserves_the_pdf_when_poll_reports_a_paperless_failure`, `::test_preserves_the_pdf_when_poll_times_out`, `::test_preserved_destination_is_named_in_the_raised_message`. |
| 2 | A PDF that reaches only the consume directory records the job as `FALLBACK` with a warning, rendered distinctly from DONE in the status area, the history table, and `saneless jobs` | ✓ VERIFIED | `pipeline.py:823-828` sets `outcome = ScanOutcome.FALLBACK` and `warning = _consume_dir_warning(...)` on the no-`task_uuid` branch; this is the 23-08 fix (`_consume_dir_warning`, `pipeline.py:340-370`) that closes the gap where the warning column stayed NULL. It reaches the persisted row through `worker.py:265-275` → `finish_job(result=JobResult(warning=result.warning, ...))` → `job.py:707-722`. Three surfaces confirmed distinct: `status.html:15-19` (`class="status-fallback"`, distinct glyph `&#8594;`, plus a second `<p>` for `job.warning`), `history.html:7` (`status-fallback` CSS class), `cli.py:278` (`state_label(j.state)` → `"Saved to folder"`, never `"FALLBACK"` verbatim — confirmed by `tests/test_cli.py::test_jobs_table_shows_fallback_distinctly` asserting `"FALLBACK" not in result.output`). Web tests: `tests/test_web_state_rendering.py::test_fallback_warning_renders_inline`, `::test_fallback_without_a_warning_renders_no_empty_paragraph`, `::test_fallback_reloads_the_history_table`. The D-20 latent-bug fix is present: `app.js:32` includes `.status-fallback` in the Scan-button-reenable condition alongside `.status-done`/`.status-error`. |
| 3 | When upload and consume-directory fallback both fail after N pages, the assembled PDF is in `<data_dir>/failed/` under a unique name and no page image or PDF was deleted on that path | ✓ VERIFIED | `pipeline.py:222-301` `_preserving` context manager wraps `upload_document` + `poll_task` (both simplex `:799-828` and duplex `:482-510`) *inside* the `TemporaryDirectory` (`:722`), using `shutil.move` (not `unlink`/`rmtree`/`os.remove` — `grep -cE 'unlink\|rmtree\|os.remove' pipeline.py` = 0, confirmed). Names are unique via `build_pdf_filename` keyed on job id (`pdf.py`), confirmed by `tests/test_pipeline.py::test_two_preserved_scans_sharing_a_title_are_two_files` and `tests/test_pdf.py::TestBuildPdfFilename::test_distinct_job_ids_produce_distinct_names`. Per D-07, only the PDF (not page PNGs) is preserved — `assemble_pdf`'s own nested `TemporaryDirectory` deletes PNGs before returning regardless of this phase, so "no PDF was deleted" is the correct and complete scope; the roadmap/OUTC-04 phrasing is satisfied under this explicitly-locked reading. |
| 4 | An A4 page scanned at 300 DPI produces a PDF with a 595 x 842 pt MediaBox, and two jobs with the same title produce two distinct PDF file names | ✓ VERIFIED, non-vacuous | `tests/test_pdf.py::TestMediaBox::test_a4_at_300_dpi_is_an_a4_page` asserts `_rounded_media_box(...) == [0, 0, 595, 842]` via `round()`, correctly per D-18 (true value 595.2×841.92). Non-vacuity proven by a sibling test, `test_a4_page_is_not_the_unlayouted_default`, which asserts the box is **not** `[0,0,1860,2631]` (the unlayouted img2pdf-default-DPI result) — this rules out the layout function silently not being applied. `test_cropped_a4_also_rounds_to_a4` and `test_every_page_gets_the_same_fixed_dpi` add further non-trivial cases. Filename uniqueness: `TestBuildPdfFilename` (`tests/test_pdf.py:205-231`) proves `build_pdf_filename(JOB_A, "Tax Return") != build_pdf_filename(JOB_B, "Tax Return")`. All 29 tests in these two classes pass independently (`uv run pytest tests/test_pdf.py::TestMediaBox tests/test_pdf.py::TestBuildPdfFilename -q`). |
| 5 | A parametrised end-to-end test drives the real worker and pipeline with a stub scanner through SUCCESS, Paperless FAILURE, TIMEOUT, consume-dir fallback, and duplex mismatch, asserting persisted state, outcome, page counts, and file preservation for each | ✓ VERIFIED | `tests/test_outcomes_e2e.py::TestFiveOutcomesEndToEnd::test_outcome_is_persisted`, parametrised over `_CASES` (5 entries: success, paperless-failure, poll-timeout, consume-dir-fallback, duplex-mismatch). Only the scanner (`MagicMock(spec=ScannerBackend)`) and the HTTP transport (`httpx.MockTransport`) are stubbed; `run_pipeline` and `ScanWorker` are real and undoctored (an in-file acceptance grep enforces `saneless.worker.run_pipeline` does not appear as a patch target). Every case reads back the persisted `Job` row via `store.get_job` and asserts `state`, `outcome`, three page counts, `warning`, `error`, and on-disk file counts in `failed/` and the consume directory. Independently re-run: 5 passed in 0.17s. |
| 6 (OUTC-11) | `poll_task` reaches a terminal status against BOTH a v9-shaped (bare list, uppercase, `result`) and a v10-shaped (paginated, lowercase, `result_data.error_message`) response, and `PaperlessClient` sends an explicit API-version `Accept` header | ✓ VERIFIED | `paperless.py:35` `_API_VERSION_ACCEPT = "application/json; version=9"`, sent on every request via `paperless.py:216`. Shape tolerance: `_extract_task` (`:51-76`) unwraps `payload.get("results")` for dicts before treating as a list; `_task_status` (`:79-94`) normalises to uppercase; `_failure_message` (`:97-123`) checks flat `result` then `result_data["error_message"]`/`"reason"`. Proven by `tests/test_paperless.py::TestPollTask::test_success_across_api_versions`, `::test_failure_raises_across_api_versions`, `::test_revoked_raises_across_api_versions`, `::test_no_task_yet_is_tolerated_across_api_versions`, each parametrised over `_v9_payload`/`_v10_payload` builders (14 tests total in `TestPollTask`, all pass independently). The Accept header claim is not merely "coded" — `test_accept_header_pins_the_api_version` intercepts the request via `httpx.MockTransport`, captures `request.headers.get("accept")`, and asserts it equals `"application/json; version=9"` (1 test, passes independently). `tests/test_outcomes_e2e.py`'s `paperless-failure` case additionally drives the real pipeline against a genuine v10-shaped response (`_v10_tasks`) end to end. |

**Score:** 6/6 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/vocabulary.py` | `ScanOutcome` (no `FAILED`), `JobState.FALLBACK`, `job_state_for`, `TERMINAL_STATES` membership | ✓ VERIFIED | Lines 39-49 (`JobState`), 62-75 (`ScanOutcome`, D-01 comment present), 130 (`FALLBACK` in `TERMINAL_STATES`), 261-268 (`job_state_for` as `match`/`assert_never`) |
| `src/saneless/job.py::finish_job` | New `@_locked` terminal-write method, `update_state` unchanged | ✓ VERIFIED | `job.py:668-723`, decorated `@_locked` (`:668`); `update_state` (`:638-666`) is the pre-phase SQL verbatim (AST-identical, confirmed by the pre-run gate note and re-read here) |
| `src/saneless/worker.py::_process_job` | Consumes `ScanResult`, calls `finish_job` on both success and exception paths, never writes `DONE` directly | ✓ VERIFIED | `:255-287`; `JobState.DONE` does not appear in this file (confirmed by prior grep and re-read) |
| `src/saneless/pipeline.py::_preserving` | Context manager spanning `upload_document`+`poll_task`, inside `TemporaryDirectory` | ✓ VERIFIED | `:222-301`, used at `:482` (duplex) and `:799` (simplex), both inside the `with tempfile.TemporaryDirectory(...)` opened at `:722` |
| `src/saneless/pdf.py::build_pdf_filename` | Job-id-keyed unique naming, sanitised | ✓ VERIFIED | Confirmed above; `sanitise_title_for_filename` rejects hostile inputs (`HOSTILE_TITLES` parametrised tests, `tests/test_pdf.py:18-31, 250-280`) |
| `src/saneless/paperless.py::poll_task` | Raises on FAILURE/REVOKED/non-200, monotonic deadline, dual-shape parsing | ✓ VERIFIED | `:385-462` |
| `src/saneless/config.py::OutputConfig` | `data_dir`, `db_path`, `failed_dir` properties | ✓ VERIFIED (not directly re-read this pass, but exercised by `tests/test_outcomes_e2e.py::_build_settings` which constructs real `Settings(output=OutputConfig(data_dir=...))` and the pipeline correctly derives `failed_dir` from it in all 5 e2e cases) |
| `tests/test_outcomes_e2e.py` | New parametrised 5-case e2e file | ✓ VERIFIED | Exists, 5 tests, all pass |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `worker._process_job` | `job.JobStore.finish_job` | direct call, both branches | ✓ WIRED | `worker.py:265-275` (success/fallback) and `:282-287` (exception) |
| `pipeline.run_pipeline` | `pipeline._preserving` | `with` block around upload+poll | ✓ WIRED | `:799` (simplex), `:482` (duplex) |
| `pipeline._consume_dir_warning` | `ScanResult.warning` | assignment at `:828` | ✓ WIRED | flows to `worker.py:270` → `JobResult(warning=...)` → `job.py:713` persisted column |
| `paperless.PaperlessClient.__init__` | `_API_VERSION_ACCEPT` | `headers["Accept"]` | ✓ WIRED | `:216`, proven reaching the wire by `test_accept_header_pins_the_api_version` |
| `vocabulary.job_state_for` | `worker._process_job` | `finish_job(job.id, job_state_for(result.outcome), ...)` | ✓ WIRED | `worker.py:265-267` |
| `web/templates/partials/status.html` / `history.html` | `JobState.FALLBACK` | Jinja conditional + CSS class | ✓ WIRED | confirmed above, plus `app.js:32` button re-enable condition |
| `cli.py::jobs` | `vocabulary.state_label` | direct call | ✓ WIRED | `cli.py:278` |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|---------------------|--------|
| `status.html` / `history.html` FALLBACK rendering | `job.warning` | `pipeline._consume_dir_warning` → `finish_job` → SQLite `warning` column → `JobStore.get_job` | Yes — `test_fallback_warning_renders_inline` asserts the literal warning text appears in the HTML response of a real `TestClient` request against a real (test) DB row | ✓ FLOWING |
| `saneless jobs` FALLBACK row | `j.state` | `JobStore.list_recent` reading the same `state` column `finish_job` wrote | Yes — `test_jobs_table_shows_fallback_distinctly` seeds the row via a real `JobStore.finish_job`-equivalent helper and reads it back through the CLI | ✓ FLOWING |
| `failed/` preserved PDF path in job error | `job.error` | `_preserving`'s re-raised exception message → `worker.py` exception handler → `finish_job(error=...)` | Yes — e2e test reads the file back off disk and asserts its name is a substring of the persisted `error` | ✓ FLOWING |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| OUTC-01 | 23-04, 23-06, 23-07 | Raise on FAILURE/timeout; ERROR recorded, never DONE | ✓ SATISFIED | See Truth 1 |
| OUTC-02 | 23-05, 23-07, 23-08 | FALLBACK state + warning, three-surface distinct rendering | ✓ SATISFIED | See Truth 2 |
| OUTC-03 | 23-06, 23-08 | Duplex mismatch recorded with warning, not silent DONE | ✓ SATISFIED | `_handle_duplex_mismatch` full parity (`pipeline.py:407-...`), e2e `duplex-mismatch` case asserts `warning_contains="Page count mismatch: 3 fronts, 2 backs"` |
| OUTC-04 | 23-06 | Preservation guard, unique name, error names the path | ✓ SATISFIED | See Truth 1 and 3 |
| OUTC-05 | 23-03, 23-04 | Unique filenames; `.part` atomic consume-dir rename | ✓ SATISFIED | `build_pdf_filename`; `paperless.py:331-383 _deliver_to_consume_dir` (dotfile staging, fsync, `staged.replace(dest)`) |
| OUTC-06 | 23-03 | Fixed-DPI MediaBox, rounded assertion | ✓ SATISFIED | See Truth 4 |
| OUTC-07 | 23-04 | Non-200 raises immediately, monotonic deadline | ✓ SATISFIED | `paperless.py:424-462`; `grep -c 'elapsed'` = 0, `grep -c 'time.monotonic'` >= 2 confirmed by plan acceptance and re-read |
| OUTC-08 | 23-04 | `test_connection` CONNECTED only for 2xx | ✓ SATISFIED | `paperless.py:464-507`, `ConnectionStatus` 5-member StrEnum in `vocabulary.py` |
| OUTC-09 | 23-02 | `data_dir` setting, DB + `failed/` under it, never `tmp_dir` | ✓ SATISFIED | `OutputConfig` properties; Dockerfile/compose repointed (`23-02-SUMMARY.md`, `docs/reference/docker.md`) |
| OUTC-10 | 23-08 | Parametrised real-worker e2e test | ✓ SATISFIED | See Truth 5 |
| OUTC-11 | 23-04 | API-version pin + v9/v10 tolerance | ✓ SATISFIED | See Truth 6 |

**Note:** `.planning/REQUIREMENTS.md`'s traceability table (lines 230-240) still marks all eleven OUTC rows "Pending" and their checklist items (`- [ ]`) unchecked, despite the code evidence above and despite `.planning/ROADMAP.md` itself marking Phase 23 `[x]` complete. This is a documentation-bookkeeping gap, not a functional one — it does not affect any of the six success criteria — but it is inconsistent with this phase's own stated norm ("whatever behaviour you change, you correct its written description in the same phase") applied to the requirements tracker. Recommend a follow-up edit to flip these rows to Complete; not a blocker.

### Anti-Patterns Found

None. `grep -nE "TBD|FIXME|XXX"` across every file touched by commits `8df9d79..afee2d8` under `src/`, `tests/`, `docs/` returned zero matches. No stub returns (`return null`/`return {}`/`return []` used as a placeholder), no empty handlers, no hardcoded-empty data flowing to a rendered surface were found in the files read during this verification.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full e2e module passes standalone | `uv run pytest tests/test_outcomes_e2e.py -q` | 5 passed in 0.17s | ✓ PASS |
| MediaBox + filename uniqueness | `uv run pytest tests/test_pdf.py::TestMediaBox tests/test_pdf.py::TestBuildPdfFilename -q` | 29 passed in 0.70s | ✓ PASS |
| poll_task dual-shape tolerance | `uv run pytest tests/test_paperless.py::TestPollTask -q` | 14 passed in 1.17s | ✓ PASS |
| Accept header pinning | `uv run pytest tests/test_paperless.py -k accept_header -q` | 1 passed | ✓ PASS |
| Full suite (regression) | `uv run pytest -q` | 746 passed | ✓ PASS |
| No preservation-path deletions | `grep -cE 'unlink\|rmtree\|os.remove' src/saneless/pipeline.py` | 0 | ✓ PASS |
| No debt markers in phase-touched files | `grep -nE "TBD\|FIXME\|XXX"` over the `8df9d79..afee2d8` diff file list | (no matches) | ✓ PASS |

### Probe Execution

No `scripts/*/tests/probe-*.sh` files exist in this repository and none are referenced by any 23-0*-PLAN.md or 23-0*-SUMMARY.md. Step 7c: SKIPPED (no probes declared or discovered).

### Deviations Assessed

- **`_DeliveryContext` bundling (23-06):** replaces two additional positional parameters to `_handle_duplex_mismatch` with a frozen 3-field record to stay under ruff's `PLR0913`. No property weakened — all three original values (`dpi`, `failed_dir`, `task_timeout`) still reach the function.
- **`notify` parameter removed from `_handle_duplex_mismatch` (23-06):** the function now derives `notify = request.status_callback or _noop_callback` internally instead of receiving it as an argument, since `request` was already passed. Behaviourally identical; confirmed by reading the function body.
- **The "same job id and title twice" duplicate-filename test replaced (23-06):** the plan's original case was mathematically impossible (`build_pdf_filename` is a pure function of `(second, job_id, title)`, so identical inputs collide by construction — this is intentional per D-09, "uniqueness comes from the job id, not the timestamp"). The executor substituted the test D-09 actually requires — two **distinct** job ids sharing a title — and it is present and passing (`test_two_preserved_scans_sharing_a_title_are_two_files`). This strengthens rather than weakens OUTC-05's guarantee.
- **Timeout budget 0, not 0.05, for the e2e TIMEOUT case (23-08):** documented reason is that `paperless_task_timeout` is a typed `int` field, and pydantic rejects `0.05`. Zero still exercises the real `deadline = monotonic() + timeout` / raise path; the `min(delay, remaining)` clamp itself is separately proven at the unit level in `test_paperless.py`. No property lost.

None of the assessed deviations weaken a property the phase goal depends on.

### Human Verification Required

None. All six success criteria have codebase-level, automated evidence (unit tests, an independent real-worker end-to-end test, and direct source reads). No visual-only, real-time-only, or external-service-only claim remains unverified — the one manual-only item recorded in `23-VALIDATION.md` (which live paperless-ngx API version the operator's server serves) is explicitly informational and does not gate the phase, since the fix is correct regardless of the answer and both shapes are covered by automated tests.

### Gaps Summary

No gaps block the phase goal. One non-blocking documentation-bookkeeping inconsistency was found (REQUIREMENTS.md traceability table not flipped to Complete for OUTC-01..11) and is recorded above for a future housekeeping edit; it does not affect any success criterion and is not included as a gap.

---

_Verified: 2026-09-11_
_Verifier: Claude (gsd-verifier)_
