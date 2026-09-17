---
phase: 30-appliance-layer
plan: 30
subsystem: checks
tags: [threading, locks, health-checks, dns, sane, documentation]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "the scanner gate as a run_checks parameter and skip_scanner taken from the worker's job (30-25), the deadline-based saned pre-probe (30-28), the refresher's probe-report boolean (30-29)"
provides:
  - "_scanner_busy(): a neutral gate-contention row that names no scan"
  - "_scanner_preflight / _scanner_enumeration / _scanner_support_missing: the scanner check split at the libsane boundary"
  - "a gated region that is the enumeration alone, with name resolution outside it"
  - "docs/reference/web-api.md telling the truth about when the scanner row names a scan"
affects: [status strip, saneless doctor, ScanWorker scan-start latency, any future caller of the scanner gate]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "one row, one fact: a lost lock and a running scan are different sentences because they are different facts"
    - "split a check at the lock boundary, so the lock covers the region that needs it and nothing adjacent"
    - "a refactor that removes a structural guarantee names the test that replaces it, in the docstring"
    - "assert the rendered row, not the private constructor that built it"

key-files:
  created: []
  modified:
    - src/saneless/checks.py
    - src/saneless/worker.py
    - tests/test_checks.py
    - tests/test_refresher.py
    - docs/reference/web-api.md

key-decisions:
  - "_scanner_busy is CheckState.OK with skipped true and no next step: 'we did not look' is a fact about the probe, and the next tick clears it without anybody acting"
  - "_scanner_result no longer routes through _dispatch, so test_a_gated_run_returns_what_an_ungated_run_returns is now the only guarantee that doctor and the strip agree -- said so in the docstring rather than left for a reader to discover"
  - "the preflight returns CheckResult | None and is tested with an explicit 'is not None', because a frozen dataclass is always truthy and an 'or' shortcut would read as a bug"
  - "_scanner_enumeration re-narrows context.scanner and returns the shared support-missing row for a branch unreachable through both real callers: that satisfies ty and pyrefly with no cast and no suppression"
  - "the docs keep the one-interval staleness rather than hiding it: a skipped row stored during a real scan was true when written, which is a different defect class from a row produced by contention"

patterns-established:
  - "Row-per-fact: when one message is reachable from two causes, give the second cause its own constructor rather than overloading the first"
  - "Lock-boundary split: a check that mixes lock-free I/O with lock-required work is split into two functions so the acquire can sit between them"
  - "Name the replacement guarantee: a refactor that drops a uniform seam records, at the call site, the test that now pins what the seam used to"

requirements-completed: [APPL-01, APPL-02]

# Metrics
duration: 21min
completed: 2026-09-17
---

# Phase 30 Plan 30: Gate Contention and the Ungated Pre-Probe Summary

**The status strip can no longer tell a household member a scan is running when a health check merely lost a lock, and the scanner gate is no longer held across a name resolution that has no timeout.**

## Performance

- **Duration:** ~21 min (first commit 13:30, last 13:42, plus the full-suite and browser gates)
- **Completed:** 2026-09-17
- **Tasks:** 2 (each a RED/GREEN pair)
- **Files modified:** 5

## Accomplishments

- A failed non-blocking acquire on the scanner gate now renders its own row. `run_checks` handles a real scan *before* the gate is consulted, so a lost gate proves the holder is not a scan — and the row no longer claims one.
- The one known contender is named where a reader will find it: `ScanWorker._read_generated_profiles` takes the gate as the worker thread's first act at startup, with `_current_job_id` still `None`, in the window that coincides exactly with the cold-start poll.
- The saned pre-probe runs with the gate free. `ScanWorker._scan_job` can no longer park behind `getaddrinfo` with the job row already written `SCANNING`, and `POST /api/checks/refresh` can no longer re-arm that parking.
- A pre-probe that settles the row — no python-sane, or every configured host refusing — takes the gate **zero** times.
- `saneless doctor` is unchanged: `test_a_gated_run_returns_what_an_ungated_run_returns` still passes unmodified, and `tests/test_doctor.py` is green with no edit.
- `docs/reference/web-api.md` no longer promises a property the code does not have, and now states the residual it used to deny.

## Task Commits

1. **Task 1: a lost gate stops claiming a scan is running**
   - `d0aa0b7` (test — RED: 2 failures, both `AssertionError: Not checked while a scan is running.`)
   - `5f1a214` (fix — GREEN)
2. **Task 2: the pre-probe runs before the gate, not inside it**
   - `8d3984e` (test — RED: 3 failures — `free_during["pre-probe"] assert False is True`, and `gate.acquires` 1 where 0 was required, twice)
   - `75ba56b` (fix — GREEN)

No REFACTOR commit: the GREEN of Task 2 *is* the refactor, and there was nothing left to tidy after it.

## The exact new row text

```python
CheckResult(
    key=CheckKey.SCANNER,
    state=CheckState.OK,
    message="The scanner was busy, so it was not checked this time.",
    skipped=True,
)
```

No next step, deliberately — `_scanner_skipped()` carries none either, and the next probe clears this condition within one refresh interval, so a next step would ask a household member to act on something already clearing. `CheckState.OK` for the reason `_scanner_skipped()` is OK: a scripted health gate keyed on red (D-01) must not fail because two threads wanted the scanner in the same instant. The message names no scan, and no host, address, port, path or exception either (ASVS V7).

`grep -c "Not checked while a scan is running" src/saneless/checks.py` is **1** — exactly one row in the module makes that claim, and it is reachable only from `run_checks`' `context.skip_scanner` branch. The `_scanner_busy` docstring refers to that sentence as "`_scanner_skipped`'s sentence" rather than quoting it, precisely so the grep stays a usable test.

## The documentation sentence that was removed

**Removed** (`docs/reference/web-api.md`, `POST /api/checks/refresh`):

> That message appears only while a scan really is in progress: the decision is taken from the job the scan worker reports it is running, not from whether some other health check happened to be busy at the same moment, so it cannot show up beside a last-checked time on an idle appliance.

**Replaced with** (the correct half kept, the promise dropped, and a second paragraph added):

> That decision is taken from the job the scan worker reports it is running, and not from whether some other health check happened to be busy at the same moment: when another check is holding the scanner briefly -- the capability read saneless does at start-up, for instance -- the row says the scanner was busy and names no scan at all.
>
> One case remains where that message outlives the scan that earned it. A skipped row stored *during* a real scan stays on the strip until the next probe replaces it, so for up to one refresh interval after a scan finishes the strip can still say a scan is running. Unlike a row produced by contention, that one was true when it was written; it is stale rather than wrong, and pressing `Check again` replaces it at once.

`grep -c "cannot show up beside a last-checked time" docs/reference/web-api.md` is **0**.

## What the split costs, and what now guarantees the two paths agree

`_scanner_result` no longer calls `_dispatch`. The uniform per-key switch used to be the structural reason the gated path and `saneless doctor`'s ungated path could not diverge — one function, two callers. Now the gated path is `_scanner_preflight` then `_scanner_enumeration` with an acquire between them, and `_check_scanner` (the `_dispatch` path) is the same two calls with no acquire. They agree because the halves are shared, not because a single function is shared.

The guarantee that they *stay* in agreement is **`tests/test_checks.py::TestRunChecksUnderTheScannerGate::test_a_gated_run_returns_what_an_ungated_run_returns`**, which runs both paths over equivalent contexts and asserts `gated == ungated` on the whole tuple. It passes unmodified. `_scanner_result`'s docstring names that test explicitly, so anyone changing either half is told where the equivalence lives.

## Files Created/Modified

- `src/saneless/checks.py` — new `_scanner_busy()`, `_scanner_support_missing()`, `_scanner_preflight()`, `_scanner_enumeration()`; `_check_scanner` is now preflight-then-enumerate; `_scanner_result` runs the preflight ungated, acquires, enumerates, releases in a `finally`.
- `src/saneless/worker.py` — comment only, at the startup `with self._scanner_gate:`. The existing re-entrancy argument stays; the fact it omitted is added: because this runs before any job exists, a check that loses this gate has not lost it to a scan, pointing at `_scanner_busy`.
- `docs/reference/web-api.md` — the overclaim replaced, the one-interval residual stated.
- `tests/test_checks.py` — 5 new cases in `TestRunChecksUnderTheScannerGate`, a new `_gate_sampling_dialler` helper, and `test_a_raising_scanner_check_still_releases_the_gate` retargeted at `_scanner_enumeration`.
- `tests/test_refresher.py` — new `test_a_gate_held_by_another_checker_stores_a_row_naming_no_scan`, which drives the **real** registry; `_context`/`_build` gained an optional backend so the scanner check can reach the gate.

## Decisions Made

- **Two rows, not one flag.** `CheckResult.skipped` is stored but nothing in the templates or the CLI branches on it — `message`, `state` and `next_step` are what both surfaces render. So the fix had to be a different *message*, not a different flag, and the tests assert the message for the same reason.
- **The tests assert the row, not the constructor.** Both RED cases match the message against `re.compile(r"\bscan(s|ning)?\b")` — word-bounded on purpose, because "scanner" is the row's own name and every scanner message is entitled to say it. A substring test would have rejected the fix.
- **The refresher test drives the real registry.** The previous regression test (`test_a_gate_held_by_another_checker_is_not_a_running_scan`) stubbed `run_checks` and asserted `skip_scanner is False`. That assertion was true all along, which is exactly why the defect survived it. The flag assertion is kept — it is still worth pinning — and the new case renders the row instead. It supplies a stub backend deliberately: with no scanner at all the preflight settles the row before the gate is reached and the case would pass vacuously.
- **`SANE_NET_HOSTS` is cleared in the new refresher test**, the same discipline `tests/test_checks.py`'s autouse fixture applies: an exported value on a developer's machine would redirect the pre-probe and make the verdict depend on the host the suite runs on.
- **`_scanner_enumeration`'s unreachable `None` branch returns the shared row.** Both real callers run the preflight first, so `context.scanner is None` cannot arrive here. Re-narrowing rather than casting is what keeps `ty` and `pyrefly` clean with no `cast(` and no suppression anywhere in `checks.py` (grep: 0 matches), and returning `_scanner_support_missing()` keeps that row defined exactly once.

## Deviations from Plan

Plan executed as written, with one adjustment the plan did not anticipate:

**1. [Rule 3 - Blocking] `test_a_raising_scanner_check_still_releases_the_gate` retargeted at `_scanner_enumeration`**
- **Found during:** Task 2 (GREEN)
- **Issue:** the test monkeypatched `checks._check_scanner` to raise and asserted `gate.releases == 1`. After the split, `_check_scanner` is not on the gated path at all — `_scanner_result` calls the preflight and the enumeration directly — so the stand-in was never reached and, with its `scanner=None` context, the preflight settled the row before the gate was taken. The test would have failed on `gate.releases == 1` while still passing its state assertion.
- **Fix:** patch `_scanner_enumeration` instead and supply a `_CountingBackend` so the preflight returns `None` and the acquire happens. The test's intent is preserved and sharpened: it now proves the release discipline around the region that is actually gated (T-30-30-05). Its docstring says why both changes were needed.
- **Files modified:** `tests/test_checks.py`
- **Commit:** `75ba56b`
- **Why not RED:** `_scanner_enumeration` does not exist at the RED commit, so `monkeypatch.setattr` would have raised `AttributeError`. The change belongs with the implementation it follows.

No other rule fired. No package was installed. No architectural decision was needed.

## Issues Encountered

- The first draft of `_scanner_busy`'s docstring quoted `_scanner_skipped`'s sentence verbatim, which took `grep -c "Not checked while a scan is running" src/saneless/checks.py` to 2 and broke the plan's own acceptance criterion. The docstring now refers to the sentence by its constructor's name, which keeps the grep meaningful as a one-row-one-claim check.
- The new refresher test needed a non-`None` scanner in the context, which `_context(settings)` did not offer. Adding an optional backend parameter was the smallest change; every existing policy case still gets `None`, because those cases stub the registry out entirely and want no backend.
- `git commit` is rewritten by the `rtk` shell hook into a form the worktree-isolation guard rejects. `/usr/bin/git` was used for `add`, `commit`, `log` and `status`; hooks ran normally every time (never `--no-verify`, never `SKIP=`).

## Verification

All of the plan's `<verification>` gates were run in this worktree:

- `uv run pytest -q` — **3065 passed**, up from the 3059 post-merge baseline (+6: 5 in `test_checks.py`, 1 in `test_refresher.py`). No test removed.
- `uv run pytest -q -m browser` — **123 passed**.
- `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pyrefly check src tests` — clean; pyrefly reports 0 errors over `src tests`.
- `uv run prek run --all-files` — every hook passed.
- `uv run saneless doctor --help` — runs; `tests/test_doctor.py` green with no edit, so the exit semantics are untouched by the new row's `CheckState.OK`.
- `uv run pytest tests/test_checks.py -q -k "ScannerCheck"` — 22 passed, no case needing a new stub because of the split.
- `grep -c "Not checked while a scan is running" src/saneless/checks.py` — **1**.
- `grep -c "cannot show up beside a last-checked time" docs/reference/web-api.md` — **0**.
- `grep -c "_scanner_host_unanswered()" src/saneless/checks.py` — **2** (one definition, one call site; the amber row was not duplicated by the split).
- `grep -rn "# noqa\|# type: ignore\|cast(" src/saneless/checks.py` — **no matches**; same over `src/saneless/worker.py` for the first two patterns.
- `grep -rc "time\.sleep" --include='*.py' tests/ | awk -F: '{s+=$2} END {print s}'` — **17**, unmoved.
- `git diff --diff-filter=D --name-only 04c7ad2 HEAD` — empty; nothing was deleted.
- `git status --short` — clean; no untracked file left behind.

## Known Stubs

None. Every function the split introduced is reached by a test, except `_scanner_enumeration`'s re-narrowing branch, which is unreachable through both real callers by construction and is documented as existing for the type checkers.

## Threat Flags

None. No network endpoint, auth path, file access pattern or schema changed; no route was added or removed. The register's dispositions stand:

- **T-30-30-01** (a row asserting a scan that is not running) — mitigated. `_scanner_busy()` names no scan; pinned by the `run_checks` case and by the refresher case that drives the real registry, plus the one-row grep.
- **T-30-30-02** (the gate held across an unbounded `getaddrinfo`) — mitigated. The gated region is the enumeration alone; pinned by `test_the_gate_is_free_while_the_saned_pre_probe_runs`, which samples the gate from inside the dialler.
- **T-30-30-03** (`doctor`'s exit code under contention) — accepted as planned. The row is `CheckState.OK`; `tests/test_doctor.py` unchanged and green.
- **T-30-30-04** (the new row's strings) — mitigated. Developer constant, no host, address, port, path, URL or exception text.
- **T-30-30-05** (release discipline after the split) — mitigated. The acquire and the `finally: release()` are adjacent and wrap `_scanner_enumeration` alone; pinned by the retargeted raising test (`releases == 1`, gate free afterwards) and by `test_the_gate_is_free_once_the_run_returns`.
- **T-30-30-SC** — no package installed.

## TDD Gate Compliance

Both tasks followed RED → GREEN, and the gate sequence is visible in `git log`:

| Gate | Task 1 | Task 2 |
|---|---|---|
| RED (`test(...)`) | `d0aa0b7`, 2 failures | `8d3984e`, 3 failures |
| GREEN (`fix(...)`) | `5f1a214` | `75ba56b` |
| REFACTOR | none needed | the GREEN commit is the refactor |

Each RED commit was verified to fail for the intended reason before the implementation was written, and the failure text is recorded under Task Commits above.
