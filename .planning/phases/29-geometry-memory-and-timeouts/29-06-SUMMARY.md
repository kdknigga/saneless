---
phase: 29-geometry-memory-and-timeouts
plan: 06
subsystem: testing
tags: [pytest, sink, records, stubs, conftest, playwright, type-checking, green-gate]

# Dependency graph
requires:
  - phase: 29-geometry-memory-and-timeouts
    provides: "plan 02's switched scan_pages/ScanBatch contract and tests/conftest.py's StubScannerBackend"
  - phase: 29-geometry-memory-and-timeouts
    provides: "plans 04 and 05's migrations of the scanner, pipeline, PDF, pages and outcome suites"
provides:
  - "all eight remaining test files on the sink signature -- the last ScannerBackend subclasses in the tree"
  - "a fully green tree: 2038 tests pass, both type checkers are clean over src AND tests, both prek stages pass"
  - "the closed red interval plans 02 through 06 were one atomic contract migration inside"
  - "the measured suppression baseline: zero added across the whole phase"
affects: [29-07, 29-08, 29-09, 29-10, 29-11]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "A stub subclasses tests.conftest.StubScannerBackend and overrides only the member that differs"
    - "A stub's records come from collecting what sink.add returns, never from sink.records -- the PageSink ABC declares add and nothing else"

key-files:
  created: []
  modified:
    - tests/test_app_lifespan.py
    - tests/test_web.py
    - tests/test_web_errors.py
    - tests/test_cross_origin.py
    - tests/test_web_state_rendering.py
    - tests/test_worker.py
    - tests/test_cli.py
    - tests/test_browser.py

key-decisions:
  - "Stub records are collected from sink.add's return value, not read off sink.records: PageSink declares only add, so sink.records is a type error on the declared parameter type and both checkers reject it"
  - "Four of the five one-page web stubs disappear entirely; only test_web_state_rendering.py keeps a subclass, for its one-device get_devices"
  - "MockSaneBackend inherits the base's scan_pages outright -- its local copy drew the identical inked page"
  - "Task 3 found no residue: the three shapes the plan predicted were all absent, so it changed no files"

patterns-established:
  - "Every ScannerBackend subclass in the suite is a real subclass, never a MagicMock, so the type checkers catch the next contract change the way they caught this one"

requirements-completed: [HARD-01]

# Metrics
duration: ~45min
completed: 2026-09-15
---

# Phase 29 Plan 06: The Red Interval Closes Summary

**The eight remaining stub scanners now implement the sink signature, and the deliberate red interval plan 02 opened is closed: 2038 tests pass, `ty check` and `pyrefly check src tests` are both clean, both prek stages pass, and the phase added exactly zero suppressions to get there.**

## The Interval, Measured At Both Ends

| Gate | At this plan's base (`0edff8b`) | At `HEAD` |
|---|---|---|
| `uv run pytest -m "not browser and not sane_hardware"` | **36 failed**, 1929 passed | **0 failed, 1965 passed** |
| `uv run pytest -m browser` | not green (stub signature) | **68 passed** |
| `uv run pytest -m sane_hardware` | 5 passed | **5 passed** |
| `uv run ty check` (full tree) | red | **All checks passed** |
| `uv run pyrefly check src tests` | **22 errors** | **0 errors** |
| `uv run ruff check . && ruff format --check .` | clean | clean (55 files) |
| `uv run prek run --all-files` | — | **Passed** |
| `uv run prek run --stage pre-push --all-files` | — | **Passed** |

The 22 pyrefly errors at the base were distributed exactly across this plan's eight files — `test_cli.py` 6, `test_worker.py` 4, and 2 each in `test_web.py`, `test_web_errors.py`, `test_cross_origin.py`, `test_web_state_rendering.py`, `test_app_lifespan.py` and `test_browser.py`. Nothing outside the plan's file list contributed one, which is what made the interval genuinely atomic rather than merely long.

**2038 tests across the three marker sets, all passing.**

## The Suppression Baseline

The plan's T-29-20 mitigation is that the gate cannot be closed by suppression, so both counts were taken at the commit *before* phase 29's first commit (`f0610e1`, the parent of `9394829`) and again at `HEAD`.

| Directive | Phase 29 start (`f0610e1`) | `HEAD` |
|---|---|---|
| `git grep -c '# noqa' -- src tests` | config.py 2, scanner/\_\_init\_\_.py 1, sane_backend.py 2, test_cli.py 1, test_web.py 1 — **7 total** | **identical, 7 total** |
| `git grep -c 'type: ignore' -- src tests` | **0** | **0** |

**Zero added, in either direction, across the whole phase.** The two in `tests/` are both pre-existing and untouched by this plan: `tests/test_cli.py:1921` (`# noqa: PLC0415` on a deliberately function-local `fastapi` import) and `tests/test_web.py:859` (`# noqa: ANN202`, a dynamic callable's return type).

One caveat on the plan's literal criterion, recorded so a verifier does not read it as a regression: the criterion's `git grep -c 'noqa'` form (without the `# `) returns **8** at HEAD against **7** at the phase start. The eighth hit is `src/saneless/scanner/base.py:441`, which is **prose inside a docstring** — the sentence *"this project adds no ``noqa``"*, written by plan 02. It is not a directive, `ruff` does not read it as one, and it was not added by this plan. Counting `# noqa` instead, which is what the criterion means, the two numbers are equal. This is the fifth occurrence in the phase of a criterion that greps for an identifier's absence while the code legitimately mentions it in prose.

## Performance

- **Duration:** ~45 min
- **Completed:** 2026-09-15
- **Tasks:** 3
- **Files modified:** 8 (166 insertions, 251 deletions — the migration is a net deletion of 85 lines)

## Task Commits

| Task | Commit | What |
|---|---|---|
| 1 | `3f770a5` | `test(29-06): collapse the five one-page web stubs onto the conftest base` |
| 2 | `b3187e4` | `test(29-06): migrate the gated worker, CLI and browser stubs to the sink` |
| 3 | — | **No files changed.** Task 3 is a gate, and it found no residue; its product is the measurement above, recorded here and committed with this summary. |

## Accomplishments

- **Four of the five one-page web stubs are gone, not migrated.** `test_web.py`, `test_web_errors.py`, `test_cross_origin.py` and `test_app_lifespan.py` each carried a near-identical 20-line `ScannerBackend` subclass; all four now use `tests.conftest.StubScannerBackend` directly. `grep -c 'def get_capabilities'` returns **0** in all five files.
- **`test_web_state_rendering.py` keeps the one subclass that earns its place.** Its `_StubScanner` now overrides `get_devices` and nothing else — the single member that genuinely differs, because the profile dropdown and the worker's startup profile generation (D-14) both read it and these tests render against a device list of one. Its docstring says exactly that.
- **The gated stubs kept every bit of their gate semantics.** `_PassBGatedScanner` still holds pass B on a `threading.Event` and still counts `scan_calls`; `_GatedScanner` still has its per-call `gates` and `entered` pairs; `_JammingGatedScanner` still raises the same verbatim `_JAM_MESSAGE` after its gate releases; `_BrowserTestScanner`'s gate is still open by default with the same bounded wait. `grep -c 'release_pass_b' tests/test_worker.py` is **10**, unchanged from the base commit.
- **Not one gated class became a `MagicMock`.** Their docstrings already argued against it, and this phase is the proof: every stub that subclassed the ABC was caught by the type checkers when the contract changed, and that is precisely how the 22 errors above located themselves.
- **`MockSaneBackend` lost its `scan_pages` entirely.** Its local copy drew a 100x100 white page with a black rectangle at (10,10)-(90,90) — byte-identical in intent to `conftest._inked_page`, which `StubScannerBackend.scan_pages` already spools. The base's version is also more honest about resolution: it reports `settings.resolution` back where the local one hardcoded 300.
- **Six stubs shed a duplicated `get_devices` or `get_capabilities`.** `_PassBGatedScanner`, `_GatedScanner` and `FailScanner` inherit the base's `get_devices` (`[]`) — and each records in its docstring *why* `[]` matters there, so the reason did not go with the code. `_BrowserTestScanner` inherits the base's `get_capabilities`.
- **The browser suite is verified by running, not by collecting.** The plan asks only for collection (77 tests collect, exit 0), but chromium is installed here, so `uv run pytest -m browser` was run: **68 passed** in 21 s, behind the module's own offline egress gate. CLAUDE.md forbids marking browser checks as human-only, and there was nothing to stop this one being automated.
- **Nine imports and one PIL dependency dropped out.** `from PIL import Image` is gone from all five web files; `saneless.scanner.base` is gone entirely from four of them.

## Files Created/Modified

- `tests/test_app_lifespan.py` — `_StubScanner` deleted; `_build_app` uses `StubScannerBackend()`.
- `tests/test_web.py` — `StubScanner` deleted; the `web_scanner` fixture and three annotations retyped. The pre-existing `# noqa: ANN202` at line 859 is untouched.
- `tests/test_web_errors.py` — `StubScanner` deleted; fixture and annotation retyped.
- `tests/test_cross_origin.py` — `StubScanner` deleted; fixture and annotation retyped.
- `tests/test_web_state_rendering.py` — `_StubScanner` reduced to a `get_devices` override on the shared base.
- `tests/test_worker.py` — `_PassBGatedScanner` and `_GatedScanner` rebased on `StubScannerBackend` with sink-signature `scan_pages`; `_JammingGatedScanner` re-signed and its `super()` call updated.
- `tests/test_cli.py` — all four stubs migrated; a module-level `_inked_page()` added for `CountingScanner`; `MockSaneBackend`'s `scan_pages` deleted.
- `tests/test_browser.py` — `_BrowserTestScanner` rebased on `StubScannerBackend`, keeping its gate and its deliberately half-black page.

## Decisions Made

- **A stub's records come from `sink.add`, never from `sink.records`.** See deviation 1 — this is the only decision in the plan that changed what the instruction literally said, and it was forced by the contract itself.
- **`RaisingScanner` still subclasses `ScannerBackend` directly.** Every one of its three methods raises, so it inherits nothing; pointing it at `StubScannerBackend` would advertise a reuse that does not exist.
- **`FailScanner` keeps its empty-list `get_capabilities`.** The base reports a flatbed at 300 dpi; the local override reports nothing, and its docstring says the pipeline fails before capabilities load. Swapping in the base's answer would change what the test drives, which the plan explicitly forbids ("Where its capabilities differ, keep the local override").
- **`_JammingGatedScanner` spools its page before raising.** `super().scan_pages(device_id, settings, sink)` now runs the sink write, then the jam is raised — which is what a real backend jamming partway through a feed leaves behind, and its docstring records that. Plan 29-09 owns what the pipeline then *does* with those preserved pages; nothing here asserts on it.
- **`test_cli.py` grew its own `_inked_page()` rather than importing conftest's.** `conftest._inked_page` is private, and `tests/test_worker.py` already keeps a local copy for the same reason. One `CountingScanner` needed a page; a four-line module-level helper is the in-repo answer.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `ScanBatch(pages=sink.records, ...)` does not type-check against the declared `sink` parameter**

- **Found during:** Task 1 (and applied throughout task 2)
- **Issue:** Both tasks' `<action>` mandate returning `ScanBatch(pages=sink.records, ...)`. But the contract signature the same plan gives in `<interfaces>` is `scan_pages(self, device_id: str, settings: ScanSettings, sink: PageSink)`, and `PageSink` (`src/saneless/scanner/base.py:298`) declares exactly one member: the abstract `add`. `records` is a `SpooledPageSink` property, not part of the seam. A stub writing `sink.records` against a parameter annotated `PageSink` is an attribute error both `ty` and `pyrefly` reject — and the only ways to silence it are a `# type: ignore`, a cast, or narrowing the stub's parameter to the concrete `SpooledPageSink`, which would break the override and undo the very contract conformance this plan exists to prove.
- **Fix:** Collect what `sink.add` returns and hand that to `scan_batch`, e.g. `record = sink.add(_inked_page())` then `return scan_batch([record], resolution=settings.resolution)`. This satisfies the action's stated intent exactly — *"it must produce its pages through `sink.add`... never a hand-built tuple of records, because that would bypass the real write path and hide a spool regression"* — because the record is the one the real sink just wrote. It is also verbatim the form `tests/conftest.py`'s own `StubScannerBackend`, `spooling` and `spooling_in_turn` already use, all three written by plan 02 alongside the contract.
- **Files modified:** all six stubs with an overriding `scan_pages`.
- **Verification:** `uv run pyrefly check src tests` — **0 errors**; `uv run ty check` — **All checks passed**. No suppression, no cast, no narrowed parameter.

**2. [Rule 1 - Doc truth] Three inherited `get_devices` answers carried a reason the base does not state**

- **Found during:** Task 2
- **Issue:** `_PassBGatedScanner`, `_GatedScanner` and `FailScanner` each had `def get_devices(self) -> list[DeviceInfo]` returning `[]` with a docstring explaining *why* — "so startup profile generation keeps the settings" (D-14). Deleting the duplicate, as the action instructs, would have deleted the reason with it; `StubScannerBackend`'s own docstring says only "Report no devices."
- **Fix:** Each class docstring now states what it inherits and why that answer matters there, before the method is dropped. The behaviour is unchanged: `[]` either way.
- **Files modified:** `tests/test_worker.py`, `tests/test_cli.py`

### Observations, no change

**3. [Observation] Task 3 found no residue at all**

- **Found during:** Task 3
- **Issue:** The plan predicts residue "in exactly three shapes": an uncovered stub or call site, an import left dangling by a deleted helper, and a docstring still describing the old contract. All five whole-tree gates were run in the prescribed order and **every one passed on the first attempt**, with no file changed.
- **Fix:** None needed. The reason is worth recording: the file-scoped gates in plans 02-06 between them covered every file that contained a `scan_pages` call site or a `ScannerBackend` subclass, and the 22 pyrefly errors at this plan's base were *all* inside its own eight files. The partition was complete, so there was nothing left over for the whole-tree gate to find. That is the outcome the atomic-interval design was for, not a sign the gate was weak — the gate was run in full, and it is what proves the partition was complete.

**4. [Observation] `rtk`'s pytest summariser reports "No tests collected" for a `--collect-only` run**

- **Found during:** Task 2
- **Issue:** `uv run pytest tests/test_browser.py --collect-only -q` printed *"Pytest: No tests collected"* through the shell's `rtk` rewrite, while exiting 0. The raw output, captured by redirecting to a file, is **77 tests collected in 0.12s**.
- **Fix:** None to the repo. Every pytest result quoted in this summary was read from redirected raw output, not from the summarised line. Recorded because a later plan reading a `--collect-only` result through the same hook would otherwise conclude a suite had vanished.

---

**Total deviations:** 4 (2 auto-fixed, 2 observations)
**Impact on plan:** No scope change. One is a genuine conflict between the plan's `<action>` text and the `PageSink` ABC the same plan cites in `<interfaces>`, resolved in favour of the action's stated intent and the in-repo precedent; one is a doc-truth repair caused by deleting duplicated methods; two are recorded observations.

## Issues Encountered

- **`PageSink` is `add` and nothing else.** Deviation 1, stated generally because plans 07 and 09 both write stubs against this seam: anything a stub wants to know about what it spooled has to come from `add`'s return value. `sink.records` exists only on `SpooledPageSink`, and reaching for it from a stub is a type error by construction.
- **No plan's work was pulled forward.** The daemon-thread timeout and cancel semantics (29-07), bounded per-page assembly, the qpdf merge and `pdf.py`'s `if pdf_bytes is None` guard (29-08), partial-scan preservation and the `failed/` directory (29-09), the SANE init guard and entry-point `close()` (29-10), and every documentation correction (29-11) are all exactly as this plan found them. `tests/fake_sane.py` in particular was not touched, so 29-07 finds `start()`, `cancel()`, `close()` and `FakeSaneModule` as 29-04 left them.
- **`src/` was not touched by this plan at all.** `git diff --stat 0edff8b..HEAD` lists eight files, all under `tests/`.

## TDD Gate Compliance

Tasks 1 and 2 are marked `tdd="true"`; task 3 is not.

**Tasks 1 and 2 are migrations, and RED was measured at the base commit before any edit.** It is on the record above: `uv run pyrefly check src tests` reported **22 errors**, every one of them a `bad-override` or `bad-argument-type` in these eight files, and `uv run pytest -m "not browser and not sane_hardware"` reported **36 failed** (24 in `test_cli.py`, 12 in `test_worker.py`). A separate `test(...)` commit of already-failing tests would have committed nothing but a failure that was already there, which is the same judgement plans 04 and 05 recorded and for the same reason.

Both task commits are `test(...)` commits, because this plan's entire product is tests. There is no `feat(...)` commit and there should not be: `src/` was completed by plans 02 and 03 and is unmodified here.

**Task 3 produced no commit of its own**, because it changed no files. Its acceptance is the measured gate output, which is committed as part of this summary.

No `--no-verify`, no `SKIP=`, no `# type: ignore`, no `# noqa`, no disabled rule, no compatibility shim, no stub added to get a commit through. Both task commits passed the full commit-stage hook set including `ty` and `pyrefly` over `src`, and the whole tree passes the pre-push stage.

## Verification

Every gate, run at `HEAD` in the order the plan specifies, all exit 0:

| # | Gate | Result |
|---|---|---|
| 1 | `uv run ruff check .` | All checks passed |
| 1 | `uv run ruff format --check .` | 55 files already formatted |
| 2 | `uv run ty check` (full tree) | All checks passed |
| 3 | `uv run pyrefly check src tests` | **0 errors** |
| 4 | `uv run pytest -m "not browser and not sane_hardware"` | **1965 passed**, 73 deselected, 43.17 s |
| 5 | `uv run prek run --all-files` | Passed (24 hooks) |
| 5 | `uv run prek run --stage pre-push --all-files` | Passed, incl. `ty (full)` and `pyrefly (full)` |
| + | `uv run pytest -m browser` | **68 passed**, 21.03 s |
| + | `uv run pytest -m sane_hardware` | **5 passed** |

Per-task gates:

- `uv run pytest tests/test_app_lifespan.py tests/test_web.py tests/test_web_errors.py tests/test_cross_origin.py tests/test_web_state_rendering.py -q` — **278 passed**
- `uv run pytest tests/test_worker.py -q` — **130 passed**
- `uv run pytest tests/test_cli.py -q` — **129 passed**
- `uv run pytest tests/test_browser.py --collect-only -q` — **77 collected**, exit 0

Acceptance greps:

| Criterion | Required | Actual |
|---|---|---|
| `grep -l 'StubScannerBackend'` over the five web files | all 5 listed | all 5 listed |
| `grep -c 'def get_capabilities'` in the four non-lifespan web files | 0 each | 0, 0, 0, 0 (and 0 in lifespan) |
| `grep -c 'def scan_pages(self, device_id: str, settings: ScanSettings)'` in worker/cli/browser | 0 each | 0, 0, 0 |
| `grep -c 'release_pass_b' tests/test_worker.py` | unchanged | **10**, and 10 at `0edff8b` |
| `uv run ruff check tests/` | exit 0 | All checks passed |
| `git grep -c '# noqa' -- src tests` | ≤ phase-29 start | **7 = 7** |
| `git grep -c 'type: ignore' -- src tests` | ≤ phase-29 start | **0 = 0** |

## Known Stubs

None. Every class this plan touched is a test double by design, and each one now routes its pages through a real `PageSink`. No placeholder stands in for unwritten behaviour, and nothing was weakened to reach green.

## Threat Flags

None beyond the plan's `<threat_model>`.

- **T-29-19** (stub scanners diverging from the real contract) is **mitigated, and the mitigation was measured by this phase itself**: all eight files' stubs are real `ScannerBackend` subclasses, not `MagicMock`s, and that is exactly why the checkers located all 22 signature violations for this plan to fix. The next contract change will be caught the same way.
- **T-29-20** (suppressing a type error to close the gate) is **mitigated and counted**: both directive counts are identical to the phase's start, and the one apparent increase under the criterion's looser grep is a docstring sentence, evidenced above.
- **T-29-21** (full-suite runtime) accepted as registered: the non-browser suite is 43 s for 1965 tests, well inside the 60 s per-test `pytest-timeout`.

## User Setup Required

None. The browser suite needed chromium, which was already installed at `~/.cache/ms-playwright/chromium-1208`.

## Next Phase Readiness

- **The tree is green.** Plans 07 through 11 each start from a passing suite and two clean type checkers, so any red they see is their own.
- **Plan 29-07** finds `tests/fake_sane.py` untouched and `_PassBGatedScanner` / `_GatedScanner` still `threading.Event`-gated — the exact shape a daemon-thread timeout test wants. The number to compare the weakref high-water mark against is still **2**, per 29-04. Note deviation 1 before writing any new stub against `PageSink`.
- **Plan 29-08** finds `tests/test_pdf.py` as 29-05 left it, `test_convert_returning_none_becomes_pdf_error` intact and `_embedded_streams` available.
- **Plan 29-09** should note that `_JammingGatedScanner` now spools its page before raising, so a jammed pass leaves records in the sink — which is the state its preservation work reads. `tests/test_pipeline.py`'s `_AssemblingSomewhereDurable` and `_reading_the_pages` are still there.
- **Plan 29-11** should note that `tests/conftest.py`'s `StubScannerBackend` docstring already describes the finished contract, and that this plan added no new forward references.
- **HARD-01 is complete.** Its ordering half was proven in 29-05, its memory half in 29-04, and its gate — a green suite on the sink/record contract — is closed here. `requirements-completed` lists it. `REQUIREMENTS.md` was **not** edited by this executor: the orchestrator owns that write, along with `STATE.md` and `ROADMAP.md`.

## Self-Check: PASSED

- **Files:** all eight of `tests/test_app_lifespan.py`, `tests/test_web.py`, `tests/test_web_errors.py`, `tests/test_cross_origin.py`, `tests/test_web_state_rendering.py`, `tests/test_worker.py`, `tests/test_cli.py`, `tests/test_browser.py` — FOUND and modified.
- **Commits:** `3f770a5` and `b3187e4` — both FOUND on `worktree-agent-a9c1c19ba00e125e2`, each with `ty type checker (src)` and `pyrefly type checker (src)` Passed and no hook bypassed.
- **No file outside the plan's `files_modified` was touched:** `git diff --stat 0edff8b..HEAD` lists exactly those eight, and `git status --short` is empty.
- **No deletions of tracked files:** the two commits are 166 insertions and 251 deletions, all line-level within those eight files. The net deletion is the point — five stub classes and six duplicated methods are gone.
- **`STATE.md` and `ROADMAP.md` were not modified**, as instructed.

---
*Phase: 29-geometry-memory-and-timeouts*
*Completed: 2026-09-15*
