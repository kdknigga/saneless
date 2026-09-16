---
phase: 30-appliance-layer
plan: 04
subsystem: scanning-core
tags: [threading-lock, callback-channel, dataclass-field, mutual-exclusion, tdd]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: ProfileStorage (plan 30-01)
  - phase: 25-manual-duplex
    provides: the manual-duplex two-pass path, SCANNING_REVERSE, WorkerFlipCoordinator
  - phase: 26-worker-robustness
    provides: _profiles_lock, _persist_generated_profiles, _process_job's finally
provides:
  - PipelineRequest.pass_count_callback, fired with the front count before SCANNING_REVERSE and the back count after pass B
  - SCAN_LABEL_FRONT / SCAN_LABEL_BACK, the shared pass labels
  - ScanWorker.front_pages, pass A's count for the job in flight, cleared on every exit path
  - ScanWorker.scanner_gate, real mutual exclusion on SANE for the status-strip refresher
  - ScanWorker.profile_storage, the recorded outcome of the one startup persist attempt
affects: [30-06, 30-07, 30-09, 30-11, 30-12]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "observer-callback channel: a mid-run fact that status_callback cannot carry gets its own optional Callable on PipelineRequest, mirroring thumbnail_callback (A-4)"
    - "non-blocking gate contract: the owner holds a threading.Lock with `with`; the observer does acquire(blocking=False) and skips on failure, never blocks (D-08)"
    - "recorded outcome over recomputed probe: a StrEnum member written at the decision point, because os.access() cannot see EBUSY on a single-file bind mount (A-2)"
    - "S105-safe constant naming: a string constant whose obvious name contains 'pass' is named SCAN_LABEL_* instead, matching the existing _SPOOL_LABEL_* precedent, rather than adding a suppression"

key-files:
  created: []
  modified:
    - src/saneless/pipeline.py
    - src/saneless/worker.py
    - tests/test_pipeline.py
    - tests/test_worker.py

key-decisions:
  - "scanner_gate IS held around _read_generated_profiles: that method enters SANE twice (get_devices is an enumeration RPC on the net backend's control wire, get_capabilities opens the device and reads its option list), so leaving it ungated would leave the exact window Pitfall 2 describes open during startup"
  - "generate_profiles(caps) stays outside the gate; it is pure computation over an already-read DeviceCapabilities and holding SANE across it would extend the exclusion for no reason"
  - "The gate wraps the whole run_pipeline call, not just the scan_pages calls: a manual-duplex job spends most of its life in the flip wait with the feeder loaded and the device open"
  - "A raising pass_count_callback is logged and swallowed, deliberately UNLIKE thumbnail_callback, which spool.py leaves unguarded on purpose -- see Deviations"
  - "The worker's pass-count handler ignores SCAN_LABEL_BACK: the back count arrives moments before the ScanResult that carries the real total, so storing it would replace the number the operator is reading with one about to be replaced again (D-33)"
  - "_front_pages gets its own lock rather than sharing _profiles_lock: they guard unrelated state, and sharing would make a status render queue behind a profile swap"
  - "front_pages is cleared in _process_job's finally, not at the start of the next job, so no observer can read the previous job's count against a row that has moved on"
  - "_profile_storage defaults to IN_MEMORY_NO_CONFIG_FILE rather than PERSISTED: claiming the profiles are on disk before any write has been attempted is the one answer that could mislead an operator"
  - "Both 'could not write' branches record the same IN_MEMORY_UNWRITABLE: two causes, one fact -- a file was loaded and the profiles did not reach it. The log says why; the row says what"
  - "status_callback, ScanResult and every existing notify are untouched, so the CLI's own status callback and every other call site compile unchanged"

patterns-established:
  - "Pass-count channel: an optional Callable[[str, int], None] fired at each pass boundary, with shared label constants so the producer and the consumer cannot drift"
  - "_CountedPassScanner: a test double that gives each manual-duplex pass its own page count, which is what makes 'front count' and 'back count' tellable apart"
  - "_gate_is_free(worker): the refresher's exact move (non-blocking acquire, release on success) as a test predicate, safe to poll"

requirements-completed: [APPL-02, APPL-03, APPL-06]

# Metrics
duration: 34min
completed: 2026-09-16
---

# Phase 30 Plan 04: Scanning-Core Channels Summary

**Three channels opened out of the scanning core with no rendering change and no signature change to anything that already existed: `pass_count_callback` carries pass A's page count out mid-run, `ScanWorker.scanner_gate` turns "skip the scanner check while a scan runs" from probable into true, and `ScanWorker.profile_storage` makes the amber Profiles row truthful instead of guessed.**

## Performance

- **Duration:** ~34 min
- **Tasks:** 3 (7 commits: 3 RED, 3 GREEN, 1 test-naming fix)
- **Files modified:** 4
- **Test suite:** 2235 passed, 74 deselected (`-m "not browser and not sane_hardware"`)

## Accomplishments

- **`PipelineRequest.pass_count_callback`** — `Callable[[str, int], None] | None = None`, sitting immediately after `thumbnail_callback`, with the Amendment A-4 reasoning recorded at the field: `status_callback` is `Callable[[PipelineEvent], None]` and carries no payload, widening it would touch every call site and hand the CLI an argument it does not want, and a `PipelineEvent` member carrying data is rejected because members are states, not payloads.
- **`SCAN_LABEL_FRONT` / `SCAN_LABEL_BACK`** — module-level `Final` constants, exported in `__all__`, so the pipeline and the worker share one spelling.
- **Two fire points** — the front count fires at the existing pass-A log site, *before* `AWAITING_FLIP` and therefore before `SCANNING_REVERSE`, so an observer re-rendering on either event already holds the number. The back count fires after pass B's own log line.
- **`ScanWorker.front_pages`** — read under its own `threading.Lock`, written only for `SCAN_LABEL_FRONT`, cleared in `_process_job`'s `finally` alongside `_flip_coordinator` and `_current_job_id`. The docstring states when the strip may read it (only while the rendered job is `SCANNING_REVERSE`) and why it is deliberately not a `Job` column (CONTEXT forbids a migration, and the value is meaningless the instant the job ends).
- **`ScanWorker.scanner_gate`** — a `threading.Lock` held with `with` around the whole `run_pipeline` call and around the whole of `_read_generated_profiles`'s SANE contact. The property docstring states the caller's contract in full: `acquire(blocking=False)`, skip on failure, never block, and use `with` if you win.
- **`ScanWorker.profile_storage`** — set to `IN_MEMORY_NO_CONFIG_FILE` on the no-config-file early return, `IN_MEMORY_UNWRITABLE` in both failure branches, `PERSISTED` on success. `ProfileStorage` imported from `saneless.vocabulary` (plan 30-01).
- **18 new tests**, all Event-handshake based. `grep -c "time.sleep" tests/test_worker.py` is 4 before and 4 after; the tree-wide count is unchanged at 17.

## Task Commits

RED before GREEN in every case:

1. **Task 1: `pass_count_callback` on `PipelineRequest`**
   - `5043837` (test — RED, fails on collection: `ImportError: cannot import name 'SCAN_LABEL_BACK'`)
   - `08f7456` (feat — GREEN)
   - `93478db` (test — rename two tests so the plan's own `-k pass_count` selector reaches all four; `pytest -k` is case-sensitive and `TestManualDuplexPassCounts` does not match `pass_count`)
2. **Task 2: `ScanWorker.front_pages`**
   - `f536ce5` (test — RED, 7 failures: `AttributeError: 'ScanWorker' object has no attribute 'front_pages'`)
   - `65584ff` (feat — GREEN)
3. **Task 3: `scanner_gate` and `profile_storage`**
   - `0309ecf` (test — RED, 11 failures)
   - `55eed71` (feat — GREEN)

No REFACTOR commits were needed.

## TDD Gate Compliance

Gate sequence verified in `git log`: each task shows `test(30-04):` immediately followed by `feat(30-04):`. Every RED was run and confirmed failing before the corresponding GREEN was written. No RED was forced through with `--no-verify`, `SKIP=` or a stub — the commit-stage hooks type-check `src/` only, which is the allowance the project documents for exactly this.

## The `scanner_gate` / `_read_generated_profiles` decision

**Decision: yes — the gate is held around `_read_generated_profiles`.**

The plan left this to the executor with the condition "if that method enters SANE". It does, twice:

- `self._scanner.get_devices()` → `SaneBackend.get_devices` (`sane_backend.py:2036-2078`) calls `sane.get_devices()`. Its own docstring records that on the `net` backend "enumeration is an RPC on the same control wire the stuck read is on", and that `_refuse_if_wedged` only refuses when a *previous* read is already wedged — not when one is merely running.
- `self._scanner.get_capabilities(device_id)` → `sane_backend.py:2080-2113` calls `_refuse_if_wedged` and then `self._open_device(device_id)` and `_read_options(dev, device_id)`. That is a device open plus an option read: unambiguously inside SANE.

So an ungated startup generation is the same hazard as an ungated refresher probe, just with the roles swapped — a status-strip probe landing during the worker's first act would be the second concurrent SANE call Pitfall 2 describes. There is no re-entrancy risk, because `_generate_profiles_at_startup` runs once as the worker thread's first act, strictly before any job, so the thread can never already hold the gate when it arrives. The gate is therefore held across *both* calls (a probe slipping between them is inside SANE just as surely as one during either), and `generate_profiles(caps)` is deliberately left **outside** it: that is pure computation over an already-read `DeviceCapabilities`, and holding SANE across it would extend the exclusion window for no benefit.

This is covered by `test_scanner_gate_is_held_while_startup_generation_reads_the_scanner`, which holds `get_devices` open on an Event and asserts the test thread's `acquire(blocking=False)` fails, then succeeds once released.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 — Blocking] `PASS_FRONT` / `PASS_BACK` trip ruff `S105`, which this project cannot suppress**

- **Found during:** Task 1, before writing any code.
- **Issue:** The plan specifies `PASS_FRONT: Final = "front"` and `PASS_BACK: Final = "back"`. Ruff's `S105` (`hardcoded-password-string`) reads any variable whose own *name* contains `pass` as a possible credential. `pipeline.py` already records this exact trap in the comment above `_SPOOL_LABEL_A`, which explains that the constants are *not* spelled `_PASS_*_LABEL` for precisely this reason. Verified empirically: `uv run ruff check --isolated --select S105` on a two-line probe file emits `S105 Possible hardcoded password assigned to: "PASS_FRONT"` and the same for `PASS_BACK`. `S105` is selected for `src/` (`pyproject.toml:92`) and only per-file-ignored for `tests/`, and CLAUDE.md forbids `# noqa`.
- **Fix:** Named them `SCAN_LABEL_FRONT` / `SCAN_LABEL_BACK`, following the file's own `_SPOOL_LABEL_*` precedent, and recorded the reason (with the verification) in the comment above them. Public rather than `_`-prefixed because `worker.py` compares against `SCAN_LABEL_FRONT`; added to `pipeline.__all__`.
- **Files modified:** `src/saneless/pipeline.py`, `tests/test_pipeline.py`, `src/saneless/worker.py`
- **Commit:** `08f7456`

**2. [Rule 1 — Contradiction between the plan's action text and its own behaviour spec] the raising-observer guard**

- **Found during:** Task 1.
- **Issue:** The plan's `<action>` says to guard the new call "the same way the existing `thumbnail_callback` call site guards its own — read that site and copy its None-check and its exception handling exactly, so an observer that raises cannot fail a scan." The thumbnail call site (`spool.py:167-182`) has a **deliberately unguarded** callback, with a long comment stating that a thumbnail is a visible part of what the operator sees so a failure there *should* surface. Copying it exactly would therefore make the plan's own `<behavior>` requirement — "a `pass_count_callback` that raises does not abort the scan" — false, and the plan's threat register entry T-30-14 unmitigated.
- **Fix:** Copied the None-check exactly, and gave the new channel a `try` / `except Exception` that logs with `logger.exception` and continues. The divergence is justified at the call site: the thumbnail is an artefact, while the pass count is a transient display value whose figure arrives again in `ScanResult.pages_scanned` moments later — so failing a run over it would turn a cosmetic fault into lost sheets. `test_a_raising_pass_count_callback_does_not_fail_the_scan` pins the behaviour.
- **Files modified:** `src/saneless/pipeline.py`
- **Commit:** `08f7456`

**3. [Rule 3 — Blocking] the plan's own `-k pass_count` acceptance selector matched only 2 of 4 tests**

- **Found during:** Task 1, verifying acceptance criteria.
- **Issue:** `pytest -k` is case-sensitive, so `TestManualDuplexPassCounts` does not match `pass_count`, and two of the four tests were deselected — failing the plan's "selects at least 3 tests" criterion through naming alone, not through missing coverage.
- **Fix:** Renamed `test_front_count_arrives_before_scanning_reverse` → `test_pass_count_front_arrives_before_scanning_reverse` and `test_status_callback_still_receives_a_bare_event` → `test_status_callback_beside_pass_count_takes_a_bare_event`. `-k pass_count` now selects all four.
- **Files modified:** `tests/test_pipeline.py`
- **Commit:** `93478db`

### Not deviations

- `create_job` was **not** touched — no new submission field was needed, so the five-parameter pin recorded in the wave-1 inheritance notes is untouched.
- No suppressions of any kind were added: no `# noqa`, no `# type: ignore`, no rule disabling, no `--no-verify`, no `SKIP=`.
- No dependency was added or installed.

## Authentication Gates

None. This plan touched no external service.

## Files Created/Modified

| File | Change |
|------|--------|
| `src/saneless/pipeline.py` | `SCAN_LABEL_FRONT` / `SCAN_LABEL_BACK`, `PipelineRequest.pass_count_callback`, `_note_pass_count`, two fire points in the manual-duplex path |
| `src/saneless/worker.py` | `_front_pages` + lock + `front_pages` property, `_scanner_gate` + `scanner_gate` property, `_profile_storage` + `profile_storage` property, `_pass_count_cb`, gate around `run_pipeline` and around `_read_generated_profiles`'s SANE contact, three outcome recordings in `_persist_generated_profiles`, clear in `_process_job`'s `finally` |
| `tests/test_pipeline.py` | `TestManualDuplexPassCounts` (4 tests) |
| `tests/test_worker.py` | `TestFrontPages` (7), `TestScannerGate` (6), `TestProfileStorage` (5), plus `_CountedPassScanner`, `_JammingPassBScanner`, `_GatedProfileScanner`, `_gate_is_free`, `_run_duplex_job` |

## Verification

| Gate | Result |
|------|--------|
| `uv run pytest tests/test_pipeline.py -q` | 139 passed |
| `uv run pytest tests/test_worker.py -q` | 148 passed |
| `uv run pytest -m "not browser and not sane_hardware" -q` | 2235 passed, 74 deselected |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 55 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `grep -rn "time.sleep" tests/ --include=*.py \| wc -l` | 17 — unchanged from the plan's base |

Acceptance greps:

- `grep -n "pass_count_callback: Callable\[\[str, int\], None\] | None = None" src/saneless/pipeline.py` — 1 match
- `grep -n "status_callback: Callable\[\[PipelineEvent\], None\]" src/saneless/pipeline.py` — still 1 match (signature unchanged)
- `grep -n "def front_pages(self) -> int | None:" src/saneless/worker.py` — 1 match, preceded by `@property`
- `grep -c "_front_pages" src/saneless/worker.py` — 8
- `grep -n "def scanner_gate(self) -> threading.Lock:" src/saneless/worker.py` — 1 match
- `grep -n "def profile_storage(self) -> ProfileStorage:" src/saneless/worker.py` — 1 match
- `grep -c "ProfileStorage\." src/saneless/worker.py` — 5
- `grep -n "with self._scanner_gate" src/saneless/worker.py` — 2 matches (lines 922, 1492)
- `pytest -k pass_count` — 4 selected, 4 passed
- `pytest -k front_pages` — 7 selected, 7 passed
- `pytest -k "scanner_gate or profile_storage"` — 11 selected, 11 passed

## Known Stubs

None. Every symbol this plan added is wired at both ends within this plan: the pipeline fires the callback and the worker stores it; the worker holds the gate and exposes it; the worker records the storage outcome and exposes it. The *consumers* (the status strip, the refresher, the Profiles row) are later plans' work by design — these are unwired-by-plan, not stubbed: none of them returns a hardcoded placeholder, and each is asserted against real behaviour by tests in this plan.

## Threat Flags

None. No new network endpoint, auth path, file-access pattern or schema change was introduced. `profile_storage` is a three-member enum and deliberately exposes no path (T-30-16); the threat register's T-30-13, T-30-14 and T-30-15 are all mitigated as planned.

## Self-Check: PASSED

- All four modified files exist on disk.
- All seven commit hashes are present in `git log` on `worktree-agent-af12e42236222f30a`, in the claimed RED-before-GREEN order, on top of the expected base `808b390`.
- `STATE.md` and `ROADMAP.md` were not modified — the orchestrator owns those writes.
