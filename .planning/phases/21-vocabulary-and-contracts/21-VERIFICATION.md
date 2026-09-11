---
phase: 21-vocabulary-and-contracts
verified: 2026-09-10T00:00:00Z
status: human_needed
score: 4/4 must-haves verified (all automated checks green; one visual item needs human confirmation)
overrides_applied: 0
human_verification:
  - test: "Start `saneless serve`, drive a manual-duplex scan, and watch the Scan button and status area across all five active states"
    expected: "The button reads \"Waiting for flip…\" with NO `aria-busy` attribute at the flip prompt, and \"Scanning…\" WITH a visible `aria-busy` spinner (Pico CSS) in every other active state"
    why_human: "The exact JobState -> disabled/aria-busy mapping is proven by an HTML-string assertion (`tests/test_web_state_rendering.py::test_scan_button_disabled_and_busy_split`, all 7 JobState members) and the 8-test Playwright browser suite passes, but no test asserts the *visual* effect Pico CSS applies to `aria-busy` (spinner appears/disappears correctly) in a rendered browser. Carried forward verbatim from 21-VALIDATION.md's own 'Manual-Only Verifications' table — the planner already flagged this as the one residual no automated check reaches."
---

# Phase 21: Vocabulary and Contracts Verification Report

**Phase Goal:** The words the system uses about itself exist exactly once and are enforceable by the type checkers — one `JobState` enum, one active-state list, one state-to-label map, one `ErrorCategory`, one `classify_source()`, and typed pipeline results — with no behaviour change and the docs that named the old `"fallback"` string updated in-phase
**Verified:** 2026-09-10
**Status:** human_needed
**Re-verification:** No — initial verification

## Method

This is goal-backward, adversarial verification: every claim below was re-derived from the current tree at commit `9cba737` (35 commits ahead of phase base `51daabc`, clean working tree), not taken from any SUMMARY.md. All type checkers, lint, and the full suite were re-run in this session rather than trusted from prior reports. All greps used `/usr/bin/grep` directly (never the `rtk`/ugrep wrapper's exit code) per the documented shell gotcha, and were scoped to tracked files or the phase's own diff (`git diff --name-only 51daabc..HEAD`) to avoid the stale `__pycache__/*.pyc` false positive.

## Goal Achievement

### Observable Truths

| # | Truth (roadmap success criterion) | Status | Evidence |
|---|---|---|---|
| 1 | One `JobState`/labels/active-list; worker, templates, CLI all read from one module | ✓ VERIFIED | `src/saneless/vocabulary.py` defines `JobState`, `ACTIVE_STATES`/`TERMINAL_STATES` (parametrised-XOR-tested) and `BUSY_STATES` (derived, annotated). `job.py:19` re-exports; `Job.is_active`/`is_busy` at `job.py:55-63` read the shared sets. `web/app.py:17,91-101` binds `state_label`/`progress_label` as Jinja filters and injects the `JobState` global — `_STATE_LABELS`/`humanize_state`/`_event_labels` are gone (`grep` returns nothing in `src/`, `tests/`). `cli.py:32,123` reads `progress_label` for scan progress prose. `saneless jobs` (`cli.py:262`) intentionally prints `j.state.value` raw — this is VALIDATION.md's documented "Open Interpretation" resolution (machine `--json` contract + human table both stay raw), not an oversight. `uv run pytest -q tests/test_vocabulary.py tests/test_web_state_rendering.py` → 44+ tests pass including the full `list(JobState)` parametrised completeness/XOR/label tests. |
| 2 | `classify_source()` correct for all named strings + vendor variants; only classification rule | ✓ VERIFIED (one pre-authorized, reviewed deferral noted) | `src/saneless/scanner/base.py:56-100`: `classify_source()` classifies "Automatic Document Feeder", "ADF Front", "ADF Duplex", "Flatbed", "Auto"/"auto"/"  Auto  ", plus vendor variants (Brother/epson/sharp/umax feeder spellings, Fujitsu "Card Duplex", "Manual Feed Tray") — all asserted in `tests/test_scanner.py::TestClassifySource` (22 tests, all pass). `sane_backend.py:492` and `auto_profiles.py:64` (`source_to_slug`) both delegate to it; the old `_is_adf_source` string-sniff is gone (`grep` confirms). **Residual, reviewed and accepted:** `auto_profiles.py:181-183,193` (`generate_profiles`) still hand-derives an `"auto"`/`"flatbed"` substring test for its own `auto_source_mode`-default and flatbed-default-profile heuristics — flagged as IN-03 in `21-REVIEW.md` and explicitly judged "not a scope violation... Phase 24 work" there. I independently confirm those two lines answer a different question (which source should the *generator* prefer) than `classify_source` (is this string a feeder), and Phase 21's own D-11/D-03 scope names exactly the two rules that were unified (`sane_backend` + `source_to_slug`) — both of which now delegate. |
| 3 | Typed `ScanResult`/`UploadResult`; `"fallback"` sentinel absent from `src/`, `tests/`, `docs/` | ✓ VERIFIED | `pipeline.py:107-115` `ScanResult` (outcome/pages_scanned/pages_removed/pages_uploaded/warning), `run_pipeline() -> ScanResult` (`pipeline.py:403`). `paperless.py:28-65` `UploadResult` with a `__post_init__` guard (added post-review, WR-05) rejecting the four contradictory `delivered_to_api`×payload combinations; `upload_document() -> UploadResult` (`paperless.py:112`). Sentinel: `git ls-files src tests \| xargs /usr/bin/grep -n '"fallback"'` returns only two English-prose comments describing the *old, removed* sentinel (`paperless.py:35`, `tests/test_paperless.py:534`) — no live reader. `docs/explanation/consume-directory-fallback.md:66` no longer claims a `FALLBACK` job status; diffed against base it now reads "The job status does not distinguish the two paths... final status is `DONE`" — the exact D-13 false-claim correction. The feature's *name* (filename, `docs/index.md:42`, `architecture.md`, `docker.md`) is correctly left intact. |
| 4 | `job.py` re-exports resolve; whole suite passes unchanged | ✓ VERIFIED | `uv run python -c "from saneless.job import Job, JobState, ErrorCategory; ..."` succeeds; `Job(state=SCANNING).is_active/is_busy == (True, True)`, `Job(state=AWAITING_FLIP) == (True, False)` — matches D-04 exactly. Full suite re-run this session: `uv run pytest -m "not browser"` → **507 passed, 8 deselected** (matches the phase's own measured baseline exactly); `uv run pytest -m browser` → **8 passed**. `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests` all green, re-run fresh in this session, not trusted from any report. |

**Score:** 4/4 truths verified (criterion 2 carries one reviewed, out-of-scope-by-design residual — see table).

### One authorized behaviour change (D-11, amended) — confirmed present, not drift

`scanner/base.py:91-92` tests `"duplex" in lower` **before** the feeder tokens, so `"Manual Duplex"` and `"Card Duplex"` (no feeder token) classify `FEEDER_DUPLEX` and route to `multi_scan()`, exactly as CR-01's finding described and as D-11 (AMENDED 2026-09-10) records the user accepting. `docs/how-to/set-up-adf-duplex.md` carries the compensating note added in this phase ("Manual duplex needs a document feeder... It is not a flatbed workflow"). This is the phase's single authorized deviation from "no behaviour change" and is documented in three independent places (CONTEXT.md D-11, REVIEW.md CR-01 resolution note, SECURITY.md W-01) plus a test that could not have passed before the phase (see below). Correctly not reported as a gap.

### Empirical proof of "could not have passed before" (roadmap discipline check)

I did not trust this claim from SUMMARY.md. I created a worktree at the phase base commit `51daabc`, copied `tests/test_scanner.py` from `HEAD` (`9cba737`) into it unmodified, and attempted to collect/run `TestSaneBackendAutomaticDocumentFeeder::test_automatic_document_feeder_yields_all_pages` and `TestClassifySource`:

```
ImportError: cannot import name 'SourceKind' from 'saneless.scanner.base'
```

The module fails to import at the base commit — `SourceKind`/`classify_source` do not exist there. This empirically confirms the phase's own discipline claim ("its own tests could not have passed before it") for at least this test, and by extension for the whole `vocabulary.py`/`classify_source` surface, which the base tree also lacks entirely.

### Required Artifacts

| Artifact | Expected | Status | Details |
|---|---|---|---|
| `src/saneless/vocabulary.py` | `JobState`, `ErrorCategory`, `ScanOutcome`, `ACTIVE_STATES`/`TERMINAL_STATES`/`BUSY_STATES`, `state_label`/`progress_label`/`error_message` (total, `match`+`assert_never`), `classify_error` | ✓ VERIFIED | 248 lines, imports only `saneless.exceptions` (leaf module, confirmed by `__future__`/stdlib-only import block) |
| `src/saneless/scanner/base.py` | `SourceKind`, `classify_source()` | ✓ VERIFIED | Present, single rule, `uses_feeder` property total-matched |
| `src/saneless/job.py` | Re-exports `JobState`/`ErrorCategory`; `Job.is_active`/`is_busy` | ✓ VERIFIED | `__all__` unchanged at `job.py:19`; both properties present and correct |
| `src/saneless/pipeline.py` | `ScanResult`, `PipelineEvent.job_state` | ✓ VERIFIED | Both present, total match, `-> ScanResult` return type |
| `src/saneless/paperless.py` | `UploadResult` | ✓ VERIFIED | Present with `__post_init__` contradiction guard (post-review strengthening, WR-05) |
| `src/saneless/web/app.py`, templates | Filters bound to shared vocabulary; no local label maps | ✓ VERIFIED | `_STATE_LABELS`/`humanize_state` deleted; `index.html`/`status.html`/`history.html` all read `job.is_active`/`is_busy`/`JobState.X` |
| `src/saneless/worker.py` | `_status_cb` collapsed to shared vocabulary; `classify_error` used (not `self._categorize_error`) | ✓ VERIFIED | `worker.py:23,253` |
| `src/saneless/cli.py` | `_event_labels` deleted; progress prose from `vocabulary.py` | ✓ VERIFIED | `_event_labels` absent; `progress_label` imported and used |
| `docs/explanation/consume-directory-fallback.md` | False `FALLBACK` status claim corrected | ✓ VERIFIED | Line 66 diff confirmed |
| `docs/how-to/set-up-adf-duplex.md` | Compensating note for the D-11-amended routing | ✓ VERIFIED | 6-line addition confirmed |

### Key Link Verification

| From | To | Via | Status | Details |
|---|---|---|---|---|
| `web/app.py` | `vocabulary.state_label`/`progress_label` | Jinja filters at `app.py:92-93` | WIRED | `history.html:8`, `status.html:9` invoke the filters |
| `web/templates/*` | `vocabulary.JobState` | Template global at `app.py:100-101` | WIRED | `index.html:58-59`, `status.html:1-16`, `history.html:7` all compare against `JobState.X`, no string literals |
| `cli.py` | `vocabulary.progress_label` | direct call at `cli.py:123` | WIRED | Confirmed |
| `worker.py` | `vocabulary.classify_error`, `ACTIVE_STATES`/`BUSY_STATES` | import at `worker.py:23` | WIRED | `_status_cb` and error handling both use the shared sets/function |
| `sane_backend.py` | `scanner.base.classify_source` | import + call at `sane_backend.py:33,492` | WIRED | `.uses_feeder` drives the `multi_scan()`/`snap()` branch |
| `auto_profiles.source_to_slug` | `scanner.base.classify_source` | import + call at `auto_profiles.py:17,64` | WIRED | Confirmed |
| `pipeline.run_pipeline` | `paperless.upload_document` → `UploadResult` | `pipeline.py:513-529` | WIRED (post-review fix) | `_handle_duplex_mismatch` (WR-01 fix, `pipeline.py:242`) now derives `delivered` from both `UploadResult`s instead of hard-coding `SUCCESS` |

### Requirements Coverage

| Requirement | Description | Status | Evidence |
|---|---|---|---|
| CTR-01 | One `JobState`, one active-state list, one label map; `job.py` re-exports | ✓ SATISFIED | Truth 1 |
| CTR-02 | Typed `ScanResult` from the pipeline | ✓ SATISFIED | Truth 3 |
| CTR-03 | Typed `UploadResult`; sentinel deleted everywhere | ✓ SATISFIED | Truth 3 |
| CTR-04 | Single `classify_source()`, correct for all named + vendor strings, only rule | ✓ SATISFIED (documented residual, see Truth 2) | Truth 2 |
| CTR-05 | `ErrorCategory` with `JobState`; single message map; no string-matching classification | ✓ SATISFIED | `classify_error` is an `isinstance` chain (by design — dispatches on exception type, not enum), module-level in `vocabulary.py`, no template/route/CLI string-matches errors (`grep` confirms) |

REQUIREMENTS.md's checkbox column (lines 18-22) and Traceability table (lines 217-221) still show CTR-01..05 as `[ ]`/"Pending" — this is a bookkeeping gap in the tracking document, not a code gap. Phase 20's equivalent CI-01 row was checked off on completion; Phase 21's was not. Flagged as an info item below, not a blocker.

### Anti-Patterns Found

None. Scanned every file in `git diff --name-only 51daabc..HEAD -- src/ tests/ docs/` (25 files) for `TBD|FIXME|XXX|TODO|HACK|PLACEHOLDER` and for new `# noqa`/`# type: ignore` — zero matches in both cases (the four pre-existing `# noqa` hits in `sane_backend.py`/`web/app.py` predate this phase's diff, confirmed by `git diff` showing no `+` line touching them).

### Code Review and Security Audit Cross-Check

`21-REVIEW.md`'s one CRITICAL (CR-01) was taken to the user and the shipped behaviour was kept — confirmed still shipped as described (duplex-before-feeder-token ordering, `scanner/base.py:91-92`). All five WARNINGs were independently re-verified as fixed in the current tree, not just trusted from the "resolved" frontmatter:
- WR-01 (duplex-mismatch outcome hard-coded to SUCCESS) — fixed, `pipeline.py:242` derives `delivered` from both `UploadResult`s.
- WR-02 (`_status_cb` widened signalling) — fixed, `worker.py:199-227` now explicitly special-cases `SCANNING_REVERSE`/already-persisted/`DONE` rather than acting on raw membership.
- WR-03 (no discriminating test for the clear-before/set-after ordering) — fixed; `SECURITY.md` cites a new `tests/test_worker.py:757 test_transition_event_is_clear_while_awaiting_flip`, confirmed present.
- WR-04 (three tests pinning JobState membership, one an anti-test) — fixed; `test_job_state_has_no_future_phase_members`, `test_scan_outcome_has_no_failed_member`, and the order-sensitive `test_job_state_member_names` are all absent from the current `tests/test_vocabulary.py`.
- WR-05 (`UploadResult` constructible in a contradictory state) — fixed, `__post_init__` guard confirmed.

`21-SECURITY.md`'s one WARNING (W-01, a false "bounded to one page" rationale for the accepted DoS risk) is a register-accuracy finding, explicitly not a code fix, and explicitly carried into Phase 24's ROADMAP entry — confirmed present in `.planning/ROADMAP.md`'s Phase 24 note. Correctly out of scope for this phase's code.

### Human Verification Required

See frontmatter `human_verification`. One item, carried forward from `21-VALIDATION.md`'s own "Manual-Only Verifications" table (the planner already identified this as the sole residual the automated suite does not reach — I independently confirmed the automated coverage is as strong as claimed: `tests/test_web_state_rendering.py::test_scan_button_disabled_and_busy_split` parametrises all 7 `JobState` members and asserts the exact `disabled`/`aria-busy` HTML attribute split, and the 8-test Playwright browser suite passes end to end). What remains unverified is purely the *rendered visual effect* of `aria-busy` (Pico CSS spinner) during a live manual-duplex flip, which no HTML-string assertion or CI browser test targets.

### Gaps Summary

No blocking gaps. One informational bookkeeping item (REQUIREMENTS.md checkboxes/traceability table not updated for CTR-01..05, unlike Phase 20's equivalent row) and one human-verification item (visual `aria-busy` rendering during a live flip) are outstanding. All four roadmap success criteria are met in the codebase with independently re-run evidence: static checks (ruff/ruff-format/ty/pyrefly) green, full suite at the exact measured baseline (507 passed/8 deselected + 8 browser), the sentinel and old duplication genuinely deleted (not just claimed), the one authorized behaviour change correctly scoped and documented, and a test that empirically could not have passed at the phase's base commit.

---

_Verified: 2026-09-10_
_Verifier: Claude (gsd-verifier)_
