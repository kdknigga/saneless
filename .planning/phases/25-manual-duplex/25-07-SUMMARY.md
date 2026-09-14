---
phase: 25-manual-duplex
plan: 07
subsystem: cli
tags: [manual-duplex, cli, click, flip-coordinator, d-11, d-19, c-02, tdd]
requires:
  - phase: 25-03
    provides: "FlipCoordinator ABC, FlipOutcome, _FlipContext carrying output.flip_timeout_seconds"
  - phase: 25-04
    provides: "ProfileConfig.duplex as the strategy reader"
  - phase: 25-06
    provides: "feeder-source resolution for manual duplex"
provides:
  - "cli._stdin_is_interactive() seam"
  - "cli.ClickFlipCoordinator(FlipCoordinator), a bounded click.confirm prompt on a daemon thread"
  - "exit-2 refusal for manual duplex when stdin is not a terminal"
affects: [25-09 docs (prompt text, exit-2 message, cli-scripting exit-code table)]
tech-stack:
  added: []
  patterns:
    - "Blocking terminal read displaced onto a daemon thread, bounded by a threading.Event wait on the caller"
    - "Single-answer claim (_resolve returns the outcome in effect), same shape as WorkerFlipCoordinator"
    - "One-line interactivity seam so the refusal test and the prompt test can both be honest"
key-files:
  created: []
  modified:
    - src/saneless/cli.py
    - tests/test_cli.py
key-decisions:
  - "click.Abort (EOF) on the prompt thread and KeyboardInterrupt (Ctrl-C) on the calling thread both resolve ABORTED, so Ctrl-C and web Abort end the job the same way"
  - "The refusal fires right after the unknown-profile guard, before SaneBackend is constructed"
  - "click.confirm default=True: pressing Enter after reloading the stack continues, matching the natural 'press Enter' gesture"
requirements-completed: [DPLX-04, DPLX-05]
duration: ~30min
completed: 2026-09-14
---

# Phase 25 Plan 07: CLI Flip Prompt Summary

**`saneless scan` on a manual-duplex profile now asks the operator to flip the stack between the passes. The wait is bounded by `output.flip_timeout_seconds`. No, EOF and Ctrl-C all end the job at the flip prompt, and a run with no terminal is refused with exit 2 before any paper moves.**

## Performance

- **Duration:** ~30 min
- **Completed:** 2026-09-14
- **Tasks:** 2 planned (RED + GREEN), plus one extra RED/fix pair for a defect found during execution
- **Files modified:** 2

## Accomplishments

- `_stdin_is_interactive()` wraps `sys.stdin.isatty()`. The refusal test runs unpatched, because `CliRunner` really is not a TTY. The prompt tests patch this one function. A comment records why the seam exists.
- `ClickFlipCoordinator`:
  - A daemon thread named `saneless-flip-prompt` runs `click.confirm`.
  - The calling thread waits on a `threading.Event` for `timeout` seconds, then claims `TIMED_OUT` through `_resolve`. If the operator answered first, that answer is kept.
  - It has no `assert` and no `# noqa`, and the parameter is named `timeout`, matching the ABC.
- `scan` refuses manual duplex with exit 2 when stdin is not interactive. Otherwise it passes `flip_coordinator=ClickFlipCoordinator()` for manual-duplex profiles and `None` for all others.
- There are 7 new tests in `TestManualDuplexPrompt`:
  - prompt ordering, with two passes and one upload
  - no → abort, with no pass B and no upload
  - EOF gives the same result as no
  - Ctrl-C during the wait → `ABORTED`
  - D-19 timeout: a single pass and no upload
  - non-TTY refusal: exit 2, `scan_pages` never called
  - a simplex profile is unaffected

## Exact strings (for plan 25-09)

**Flip prompt** (`_FLIP_PROMPT`, rendered by `click.confirm` with `default=True`):

```
Flip the stack over and load it back into the feeder. Scan the back sides? [Y/n]:
```

**Exit-2 refusal** (stderr):

```
Profile '<name>' is manual duplex, which needs an interactive terminal: saneless must prompt you to flip the stack between the two passes. Run it from a terminal, or scan from the web UI.
```

**Resulting job errors** (from `pipeline._scan_manual_duplex`, printed as `Scan error: ...`, exit 1):

- no / EOF / Ctrl-C: `Manual duplex scan aborted at the flip prompt`
- timeout: `Manual duplex flip wait timed out after <N> seconds: nobody confirmed the stack was flipped`

## D-19 accepted cost, as written in the docstring

> Accepted cost, deliberate and not a leak: after a timeout the prompt thread is abandoned. It keeps its read on stdin until the process exits, and its prompt may be left sitting on the terminal. That is bounded -- the job has already failed and the CLI is on its way out -- and a daemon thread parked on stdin was measured not to delay interpreter shutdown. It must stay a daemon thread: a non-daemon one would hold the interpreter open at exit waiting for an answer nobody is going to give.

## Task Commits

1. **Task 1 (RED): pin the prompt, ordering, refusal, abort and timeout.** `1e19baa`
2. **Task 2 (GREEN): interactivity seam, bounded Click coordinator, exit-2 refusal.** `81e8d8e`
3. **Deviation RED: pin Ctrl-C during the flip wait as ABORTED.** `ac71885`
4. **Deviation fix: catch KeyboardInterrupt in the wait and claim ABORTED.** `112c157`

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Ctrl-C bypassed the ABORTED mapping**
- **Found during:** Task 2
- **Issue:** The plan catches `click.Abort` on the prompt thread and says this covers "Ctrl-C or EOF". That holds for EOF only. Python handles SIGINT on the main thread, which is parked in `Event.wait`. A real Ctrl-C therefore raised a raw `KeyboardInterrupt` out of `wait_for_flip`, and click reported a bare "Aborted!" instead of the job's flip-prompt abort. This was measured with a real `SIGINT` against a stdin that never answers; the probe printed `KeyboardInterrupt raised on the main thread`. That breaks the must-have "Ctrl-C or EOF at the prompt produces the same job outcome as clicking Abort".
- **Fix:** `wait_for_flip` catches `KeyboardInterrupt` around the wait and claims `FlipOutcome.ABORTED` through `_resolve`. The same probe now prints `outcome ABORTED`. The docstring names both paths.
- **Test:** `test_ctrl_c_during_the_wait_is_an_abort` replaces the coordinator's event with one whose `wait` raises `KeyboardInterrupt`. It does not send a real signal, because a regression would take the whole pytest session down. At the RED commit it did exactly that.
- **Files modified:** src/saneless/cli.py, tests/test_cli.py
- **Commits:** `ac71885`, `112c157`

**2. [Rule 1 - Test bug] EOF error line extraction**
- **Found during:** Task 2
- **Issue:** At EOF `click.confirm` writes no newline after the prompt, so `Scan error: ...` ends up on the prompt's line. The line-based comparison between the no case and the EOF case therefore differed on the prompt prefix.
- **Fix:** The test now slices the output from `Scan error` onwards before comparing.
- **Commit:** `81e8d8e`

## Verification

- `uv run pytest -q`: 1043 passed. That is the 1036 tests at this plan's base plus the 7 tests in `TestManualDuplexPrompt`, and there are no regressions.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: all clean. The four pyrefly warnings are pre-existing, in `pipeline.py`, `sane_backend.py` and `fake_sane.py`, and none are in files this plan touched.
- Grep gates:
  - `click.pause` appears 0 times in `src/saneless/cli.py` and 0 times in `tests/test_cli.py`.
  - `daemon=True` appears exactly once.
  - `FlipOutcome.TIMED_OUT` appears once or more.
  - `_timeout` appears 0 times, and `# noqa` appears 0 times.
  - There is no `assert ` in `cli.py`.

## TDD Gate Compliance

RED `1e19baa` (test) → GREEN `81e8d8e` (feat) → RED `ac71885` (test) → fix `112c157` (fix). Every gate commit is present and in order.

## Self-Check: PASSED

- FOUND: src/saneless/cli.py (ClickFlipCoordinator, _stdin_is_interactive)
- FOUND: tests/test_cli.py (TestManualDuplexPrompt)
- FOUND commits: 1e19baa, 81e8d8e, ac71885, 112c157
