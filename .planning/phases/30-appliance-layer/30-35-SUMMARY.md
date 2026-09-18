---
phase: 30-appliance-layer
plan: 35
subsystem: ui
tags: [htmx, polling, fastapi, health-checks, tdd, mutation-testing, asvs-v7, r3-wr-04]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "the bounded cold-start poll and POLL_ATTEMPT_CAP (30-27), the settling poll driven by probe_in_flight (30-29), the saned pre-probe and _MAX_PROBE_HOSTS (30-31), the in-handler attempt clamp and _checks_fallback_context (30-32), the row-level skipped markers (30-34)"
provides:
  - "checks.POLL_PROBE_ATTEMPT_CAP, the second and larger attempt bound that applies only while a checker holds the refresher's single-flight lock"
  - "checks.POLL_STILL_CHECKING_LINE, the sentence a cold strip prints instead of the give-up line while its first check is demonstrably running"
  - "routes._poll_line, the one place the two poll endings are chosen between"
  - "a _checks_context whose gave_up is measured against the applicable cap rather than always against POLL_ATTEMPT_CAP"
  - "a GET /api/checks clamp whose upper bound is the larger cap, so a legitimate settling attempt is not clamped into an early ending"
  - "tests/_SpendingClock, a monotonic clock the dial itself moves, which is what makes a shared deadline distinguishable from a per-socket timeout in a suite that never waits"
  - "the four-context gated-equals-ungated parametrisation named in _scanner_result's docstring"
affects: [status strip, the cold-start and settling polls, saneless doctor's agreement with the strip, docs/reference/web-api.md]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "a bound may be chosen per render from a liveness observation, provided every branch of the choice is itself a bound"
    - "a sentence is withheld when the action it recommends cannot help, and returned when it becomes the only action left"
    - "a timing property that a suite which never sleeps cannot observe is measured against a clock the unit under test's own collaborator advances"
    - "a test named as the guarantee that two surfaces agree is parametrised over every context whose output differs, not over the one that was convenient"

key-files:
  created: []
  modified:
    - src/saneless/checks.py
    - src/saneless/web/routes.py
    - src/saneless/web/templates/partials/checks.html
    - tests/test_web_checks.py
    - tests/test_checks.py
    - docs/reference/web-api.md

key-decisions:
  - "Both directions R3-WR-04 offered are taken, in the only combination that keeps IN-07 intact: POLL_ATTEMPT_CAP keeps its value and its meaning (nothing in flight, stop asking) and POLL_PROBE_ATTEMPT_CAP = 90 applies only while probe_in_flight is true"
  - "gave_up means 'this cold chain has stopped', measured against the applicable cap -- so the give-up line IS printed at the larger cap even over a held lock, because by then the button really is the only way forward and a lock a dead thread holds stays held forever"
  - "still_checking carries no scan_active carve-out, following gave_up's own precedent: neither flag has ever consulted it"
  - "The two endings are chosen in a named helper (_poll_line) rather than a nested ternary, so the mutual exclusion can be stated once and read once"
  - "The shared-budget property is measured twice, against two spend fractions, because no single scripted run distinguishes both mutants: full consumption catches a walk with no deadline, partial consumption catches a deadline that still hands each socket the full budget"
  - "_MAX_PROBE_HOSTS, _scanner_preflight and _saned_reachable are untouched: plan 30-21's decision record settled that the pre-probe short-circuit stays and that sane_get_devices is never wrapped in a thread-plus-deadline.  This plan reshaped the window only"

patterns-established:
  - "Every mutation named in the plan's acceptance criteria was applied, run, counted and reverted, and the observed failure text is recorded below"
  - "When a scripted-clock test exists to forbid an implementation, the forbidden implementation is named in the test's own docstring"

requirements-completed: [APPL-02]

# Metrics
duration: 22min
completed: 2026-09-17
---

# Phase 30 Plan 35: The Poll Window Follows the Probe Summary

**A cold strip no longer says "The checks have not run yet. Press Check again to try now." while its first check is demonstrably still running: the chain keeps asking for about three minutes under a second, still-finite cap whenever a checker holds the refresher's single-flight lock, and two tests that were cited as guarantees but could not fail for their own properties now do.**

## Performance

- **Duration:** ~22 min (first commit 2026-09-17T21:31:28-05:00, last 21:44:47-05:00, base `1a0ad05`)
- **Tasks:** 3
- **Commits:** 4
- **Files modified:** 6 (0 created, 0 deleted)

## Accomplishments

### Task 1 — the window distinguishes "gave up" from "still waiting" (`24d06fa` RED, `92f7887` GREEN)

`POLL_ATTEMPT_CAP`'s comment no longer claims a 7 s worst case. The disproved arithmetic — "about two and a half times the worst probe budget a cold start can cost, `PROBE_CONNECT_SECONDS` for saned plus `PROBE_READ_SECONDS` for Paperless" — is replaced by the three facts this same module documents that falsify it: `getaddrinfo` is outside every budget here (an unreachable resolver costs whatever `resolv.conf` says), the pre-probe pays that once per configured host up to `_MAX_PROBE_HOSTS`, and the ordinary local-USB deployment has no parseable host at all, so `_scanner_preflight` dials nothing and `_scanner_enumeration` enters `get_devices()`, which this file costs at roughly 127 s. The cap is restated as what it was always really sized for: the bound for a chain with **nothing in flight** — a dead refresher thread and a tab left open in front of it.

`POLL_PROBE_ATTEMPT_CAP = 90` sits beside it with the arithmetic in full: 90 attempts at the real two-second interval is about 180 s, which exceeds the ~127 s enumeration plus both probe budgets with room for the pre-probe's unbounded resolutions. Its comment states why it is a cap rather than an exemption — `Lock.locked()` stays true forever if the holder dies, and a thread that dies inside the lock is exactly the failure mode this area keeps hitting.

`POLL_STILL_CHECKING_LINE` is the sentence printed instead of the give-up line while a probe runs: *"The first check is still running. This can take a couple of minutes if the scanner is not reachable."* It names no host, port, path, URL or exception text, and a test asserts that character-set property directly, because it renders on a page the whole LAN can read (ASVS V7).

`_checks_context` reads `probe_in_flight` once, as it already did, and derives everything from it: `applicable_cap`, `gave_up` (cold **and** at the applicable cap), `still_checking` (cold, in flight, past `POLL_ATTEMPT_CAP`, not yet given up) and `poll_attempt`. `_poll_line` picks the sentence. `get_checks` clamps against `POLL_PROBE_ATTEMPT_CAP` so a legitimate settling attempt is not clamped down into an ending it never reached.

One refinement against the plan's prose, driven by the plan's own behaviour list: the give-up line **is** printed at `POLL_PROBE_ATTEMPT_CAP` even with the lock still held. "The give-up line only when nothing is in flight" is true of the interesting window; at the far cap the chain has stopped and the button is genuinely the only thing left, so withholding it there would leave a strip that neither asks nor advises. The first RED run caught exactly this (see below).

### Task 2 — two named-but-decorative tests made load-bearing (`482bd82`)

**R3-IN-02.** `test_three_resolved_addresses_share_one_budget` asserted `timeouts[0] <= budget`, `all(t > 0)` and `timeouts == sorted(timeouts, reverse=True)`. `[2.0, 2.0, 2.0]` satisfies all three — the last because a reverse sort of equal values is the same list — so a per-socket `settimeout` passed the test named after a shared deadline. It could not have done better under a real clock: nothing in this file waits, so three refused connects land in the same microsecond and each is handed very nearly the whole budget whichever implementation is underneath.

`_SpendingClock` fixes that. It is a monotonic clock the *dial* moves forward, by `spend × granted timeout` per connect, installed over `checks.monotonic` the way `test_the_deadline_stops_the_walk`'s scripted clock is. The property is measured twice:

- `spend=1.0` (a socket that sat there until its timeout expired): the granted timeouts must **sum** to no more than one budget. The deadline gives the first attempt everything and leaves nothing for the second. No tolerance is needed — the scripted clock's arithmetic is exact.
- `spend=0.25`: all three addresses are reached and each grant must be **strictly** smaller than the one before.

The forbidden implementation is named in the test's docstring: `probe.settimeout(PROBE_CONNECT_SECONDS)` per socket, which produces three equal budgets.

**R3-IN-01.** `test_a_gated_run_returns_what_an_ungated_run_returns` is parametrised over the four contexts whose rows differ: `scanner=None` (the no-python-sane row), a configured host that refuses the pre-probe (the host-unanswered row), a host that answers and enumerates one device (the only case it used to run), and an enumeration that raises. `_gated_context_builder` returns a *factory* rather than a context, because the comparison runs the checks twice and the backends count their calls. Every variant stubs `checks._saned_reachable`, including the two that never reach it. The test keeps its name — `_scanner_result`'s docstring and the 30-30 summary both cite it — and that docstring now says which four contexts it covers and why each one exercises a different half of the function.

### Task 3 — documentation that matches the two endings and admits the race (`0279122`)

`docs/reference/web-api.md`'s `GET /api/checks` give-up paragraph became four, giving both bounds in seconds rather than attempt counts: about twenty seconds with nothing in flight, about three minutes while a check is demonstrably running, and the return of the give-up line at the far end. Each quotes the line it prints, and the "advice that cannot help" reasoning is stated rather than assumed.

The `POST /api/checks/refresh` busy-row paragraph gained the sampling race: whether a scan is running is read *once*, before the checks run, and the worker records the job it is about to start *before* it takes the scanner, so a scan starting in that window is invisible to the sample and produces the contention row under a "paused during scan" line. The docs state that neither is false — nothing was checked either way — and that the next refresh replaces it. The collapsed-refresh paragraph's "a handful of attempts" is replaced by the same pair of bounds. No code change was needed for task 3 and none was made.

## Claim-by-claim traceability for the new documentation

| Claim in `docs/reference/web-api.md` | Traceable to |
|---|---|
| "gives up after about twenty seconds" | `checks.POLL_ATTEMPT_CAP = 10` at the two-second interval `partials/checks.html` emits |
| the give-up line's exact text | `checks.POLL_GAVE_UP_LINE` |
| "keeps asking instead, for about three minutes" | `checks.POLL_PROBE_ATTEMPT_CAP = 90` at the same interval |
| the still-running line's exact text | `checks.POLL_STILL_CHECKING_LINE` |
| "the line goes back to saying the checks have not run yet" after three minutes | `routes._checks_context`: `gave_up = cold and attempt >= applicable_cap` |
| "a check that has been running that long is one whose thread may well have died holding the lock" | `refresher.CheckRefresher.probe_in_flight`'s `Lock.locked()` read |
| "name resolution has no timeout of its own" | `checks.PROBE_CONNECT_SECONDS`' comment |
| "a deployment with no configured scanner host goes straight into the SANE enumeration … around two minutes" | `checks._scanner_preflight` returning `None` with no entries, and `_saned_reachable`'s ~127 s figure |
| "press … collapses into the check in flight" | `refresher._probe_and_store`'s non-blocking `_probe_lock.acquire` |
| "whether a scan is running is read once, before the checks run" | `refresher._probe_and_store`: `replace(self.build_context(), skip_scanner=self._scan_active())` |
| "the worker records the job … before it takes the scanner" | `worker._process_job`: `self._current_job_id = job.id` then `self._scan_job(job)` |
| "the line above the rows … is composed after the checks have finished" | `routes._checks_context` reading `state.worker.current_job_id` at render time |

## Mutation evidence (each applied, run, counted and reverted by hand)

| Mutation | Test | Observed |
|---|---|---|
| Revert `gave_up` to `cached.results is None and attempt >= POLL_ATTEMPT_CAP` | `test_a_cold_chain_waiting_on_a_probe_says_the_first_check_is_still_running` | **1 failed, 136 passed** — `AssertionError: assert 'The checks h...n to try now.' == 'The first ch...ot reachable.'`: the give-up line is printed while a probe runs |
| Collapse the two caps (`applicable_cap = POLL_ATTEMPT_CAP`) | the settling and cold chains | **6 failed, 131 passed** — `test_the_settling_poll_chain_is_bounded_even_with_a_probe_stuck` and `test_the_cold_chain_under_a_stuck_probe_is_bounded_by_the_second_cap` both end at 11 links instead of 91; the keeps-asking, still-running, settling-line and clamp cases fail with an empty attribute string where a trigger was expected |
| Replace the deadline walk with a per-socket `settimeout(PROBE_CONNECT_SECONDS)` (deadline removed) | `test_three_resolved_addresses_share_one_budget` | **1 failed** — `AssertionError: [0.5, 0.5, 0.5]` on `sum(spent.timeouts) <= _PROBE_BUDGET` (1.5 against 0.5) |
| The subtler variant: keep the deadline, hand each socket the full `timeout` | `test_three_resolved_addresses_share_one_budget` | **1 failed** — `AssertionError: [0.5, 0.5, 0.5]` on the strictly-decreasing assertion. Run in addition to the one the plan named, because the plan's mutation removes two things at once and a reviewer is entitled to know which half of the test kills which |
| `_scanner_result` short-circuits `context.scanner is None` to `_scanner_busy()` (gated path only) | `test_a_gated_run_returns_what_an_ungated_run_returns` | **1 failed of 4 params** — `[no-python-sane]`: `At index 0 diff: CheckResult(… message='The scanner was busy, so it was not checked this time.', skipped=True) != CheckResult(… state=FAIL, message='Scanner support is not installed on this machine.', …)`. The pre-parametrisation version of this test used a real backend and would not have failed at all |

After every mutation the source was restored and the affected module re-run green before any commit. `git status --short` is clean: no mutation edit survived.

**One process note, recorded because it cost real work:** reverting the first probe mutation with `git checkout -- src/saneless/checks.py` also discarded an uncommitted `_scanner_result` docstring edit from the same file. The edit was reconstructed and is present in `482bd82`. Mutations are applied and reverted by targeted string replacement from now on, never by a checkout of a file that also carries uncommitted work.

## TDD gate compliance

Task 1 ran `test(...)` → `feat(...)`:

- **RED** (`24d06fa`): **12 failed, 125 passed**. Nine failures were `AttributeError: module 'saneless.checks' has no attribute 'POLL_PROBE_ATTEMPT_CAP' / 'POLL_STILL_CHECKING_LINE'`; two were genuine behavioural failures against the old code (`assert '/api/checks?attempt=11' in ''` — the chain had already stopped), which is the defect stated as an assertion. The one new case that passed in RED is `test_a_cold_chain_with_nothing_in_flight_still_gives_up_at_the_first_cap`, deliberately: it is the contrast case and its passing is what makes the others mean something.
- **GREEN** (`92f7887`): 136 passed, 1 failed — `test_a_probe_that_never_ends_still_stops_the_asking` got the still-running line where the plan's behaviour list demands the give-up line. `gave_up` was widened from "at the applicable cap **and** nothing in flight" to "at the applicable cap", with `still_checking` narrowed by `not gave_up` to keep the two mutually exclusive. 137 passed after that.

Task 2 is test-only and carries no implementation to gate; its `feat` half is the two mutations above, which prove the tests fail against the wrong implementations. Task 3 is documentation. No REFACTOR commit: neither GREEN left anything to clean up.

## Acceptance criteria

| Criterion | Result |
|---|---|
| `grep -n "POLL_PROBE_ATTEMPT_CAP\|POLL_STILL_CHECKING_LINE" src/saneless/checks.py` ≥ 4 matches | **5 lines** |
| `uv run python -c "…assert POLL_PROBE_ATTEMPT_CAP > POLL_ATTEMPT_CAP; assert POLL_PROBE_ATTEMPT_CAP * 2 > 127"` | exit 0 |
| `grep -c "two and a half times the worst probe budget" src/saneless/checks.py` is 0 | **0** |
| `grep -n "min(max(attempt" src/saneless/web/routes.py` shows the larger cap | `counted = min(max(attempt, 0), POLL_PROBE_ATTEMPT_CAP)` |
| `grep -n "sum(" tests/test_checks.py` shows the summed-budget assertion | line 1187, inside `test_three_resolved_addresses_share_one_budget` |
| `grep -c "sorted(timeouts, reverse=True)" tests/test_checks.py` is 0, **or** the remaining comparison is strict | **1 match, and it is prose** — the docstring names the disproved assertion so a future reader knows what was removed and why. The surviving comparison in code is `earlier > later` over `pairwise`, which is strict, so the criterion's second branch is met |
| `uv run pytest tests/test_checks.py -q -k gated_run` collects ≥ 4 cases and exits 0 | 4 parametrised cases (6 tests match the filter); passed |
| `grep -c "The checks have not run yet" docs/reference/web-api.md` ≥ 1 | **1**, and the surrounding section describes the still-running ending too |
| `grep -ci "still running" docs/reference/web-api.md` ≥ 1 | **2** |
| The busy-row paragraph names the sample-then-acquire ordering and does not claim contention is the only cause | Yes — quoted in the traceability table above |
| `uv run prek run --all-files` | every hook passed |

## Verification

Every gate in the plan's `<verification>` block was run in this worktree, on the final tree:

- `uv run pytest tests/test_checks.py tests/test_web_checks.py -q` — **317 passed** (180 + 137).
- `uv run pytest -q` — **3168 passed**, 0 failed, 0 skipped.
- `uv run pytest tests/test_browser.py -q` — **133 passed** (Chromium, offline). The strip's cold-start poll and its failure ending are measured in a real browser by `TestColdStartPollStops` and `TestPollEndsOnAnErrorResponse`; both still pass against the new arithmetic, which is what pins that the larger cap did not disturb the healthy-idle chain the browser measures.
- `uv run ruff check .` — clean. `uv run ruff format --check .` — 63 files already formatted.
- `uv run ty check` — all checks passed. `uv run pyrefly check src tests` — 0 errors.
- `uv run prek run --all-files` and `uv run prek run --stage pre-push --all-files` — every hook passed, including the full `ty` and `pyrefly src tests`.
- No `# type: ignore`, `# noqa`, `--no-verify` or `SKIP=` anywhere in this plan.

**"No DNS lookup" (task 2's last criterion), measured rather than asserted.** `tests/test_checks.py` was re-run under a plugin that replaces `socket.getaddrinfo` with one that raises: **7 failed, 173 passed**. All seven are pre-existing cases that resolve *loopback* on purpose — `TestSanedReachable`'s three real-socket cases and four `TestScannerCheck` cases built on them — which this module's own docstring declares ("The saned pre-probe is tested against real loopback sockets"). None of the four `gated_run` parametrisations and neither half of `test_three_resolved_addresses_share_one_budget` resolves anything: every one of them stubs the probe seam or the recorder's `getaddrinfo`.

## Deviations from Plan

No deviation rule fired; nothing was auto-fixed. Three implementation decisions differ from the plan's prose and each is argued above rather than silently taken:

1. **The give-up line returns at `POLL_PROBE_ATTEMPT_CAP`.** The plan's summary sentence says the give-up line appears "only when nothing is demonstrably in flight"; its own behaviour list says the far cap prints it with a probe still held. The behaviour list is implemented, because a chain that has stopped asking and also declines to name the button is a strip with nothing on it that can help.
2. **`still_checking` has no `scan_active` carve-out.** During a scan with a cold cache the still-running line replaces `_COLD_PAUSED_LINE` rather than deferring to it. That follows `gave_up`'s own precedent — it has never consulted `scan_active` either — and adding the branch would have been behaviour no criterion asked for.
3. **The choice between the three sentences lives in `_poll_line`**, a new module-private helper, rather than in a nested conditional expression inside the returned dict. The mutual exclusion of the two endings is worth stating once in prose next to the code that relies on it.

Two files outside the plan's `files_modified` list were touched, both because they assert or describe the thing that changed:

- `src/saneless/web/templates/partials/checks.html` — two comment paragraphs said every chain is "bounded by checks.POLL_ATTEMPT_CAP whichever condition started it", which this plan makes false. They now name both caps and which observation picks between them. No markup changed.
- `tests/test_web_checks.py` — beyond the new class, four existing cases encoded the single-cap behaviour and were updated: the two settling-poll bounds, the settling last-checked line, and the crafted-attempt clamp expectation (`seen == [cap, 0, cap]` now against the larger cap). `_POLL_CHAIN_LIMIT` rose from 60 to 200, since a 91-link chain has to be followable before it can be called bounded.

## Issues Encountered

- **A suite that never sleeps cannot see a shared deadline.** The plan asks for a summed-budget assertion, and under a real clock the sum is three budgets *under the correct implementation* too, because nothing consumes time between refused connects. The fix is `_SpendingClock`: the property is only observable once the dial itself is what moves the clock. Recording this because the same trap will catch the next timing test written in this file.
- **One scripted run does not kill both mutants.** A full-consumption clock stops the walk after one attempt, so it cannot see a strictly-decreasing sequence; a partial-consumption clock reaches all three addresses but sums to more than one budget under either implementation. Both fractions are needed, and both are in the one test the plan named.
- **`git checkout --` on a file carrying uncommitted work.** Noted in the mutation table; the lost docstring was reconstructed and committed.

## Known Stubs

None. Every branch added by this plan is reached by a test: both caps at and below their bounds, all three freshness sentences, the clamp at both ends of its enlarged range, and each of the four gated contexts.

## Threat Flags

None. No endpoint, auth path, file access pattern or schema was added or removed. The plan's register is honoured as written:

- **T-30-35-01** (DoS via the longer window) — mitigated. `POLL_PROBE_ATTEMPT_CAP` is a cap, pinned by `test_a_probe_that_never_ends_still_stops_the_asking` and `test_the_cold_chain_under_a_stuck_probe_is_bounded_by_the_second_cap`, both of which hold the lock for the whole walk and still require the chain to end.
- **T-30-35-02** (`attempt` tampering) — mitigated, unchanged in kind. The clamp still runs before anything reads the value; only its upper bound moved. `test_a_crafted_attempt_never_reaches_the_context_or_the_body` records what `_checks_context` was actually handed (`[90, 0, 90]`) and asserts the crafted string never reaches a rendered `hx-get`.
- **T-30-35-03** (information disclosure via the new line) — mitigated. `POLL_STILL_CHECKING_LINE` is a developer-authored constant and `test_the_still_checking_line_names_nothing_a_lan_reader_may_not_see` asserts the absence of `http`, `://`, `/`, `\`, `:`, `Error` and `Traceback` in it directly.
- **T-30-35-04** (probe rate under a longer chain) — accepted as planned. Every poll is a cache read and never a probe (D-04); `TestNoProbeInARequest` and `test_the_read_only_route_neither_claims_nor_probes` still hold.
- **T-30-35-SC** — no package was installed; `uv.lock` is untouched.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- **R3-WR-04 is closed** with a window that outlasts the probe this module documents, an honest comment on both caps, and mutation evidence for each half of the mechanism.
- **R3-IN-01 and R3-IN-02 are closed** by tests that demonstrably fail against the implementations they are named to forbid — which is the property the previous round's closure lacked.
- **R3-IN-06 is closed** in documentation only, as the plan specified: the race is real, the message is not false, and no code change was warranted.
- Out of scope and untouched by design: the probe itself. Plan 30-21's decision record — the 2 s saned pre-probe short-circuit stays, and `sane_get_devices` is never wrapped in a worker thread plus a deadline because a helper thread that misses its deadline is still inside SANE after the gate is released — is honoured. `_saned_reachable`, `_scanner_preflight` and `_MAX_PROBE_HOSTS` have no behavioural change in this plan.
- The next person to change either poll bound should start at `checks.POLL_ATTEMPT_CAP`'s and `POLL_PROBE_ATTEMPT_CAP`'s comments, which now state what each one is sized against, and at `routes._checks_context`, which is the one place the choice between them is made.

## Self-Check: PASSED

All six modified files exist on disk. All four task commits are present in this worktree's history — `24d06fa`, `92f7887`, `482bd82`, `0279122` — with this SUMMARY committed on top of them. `git status --short` was clean before the SUMMARY was written: no mutation edit survived, and `STATE.md` and `ROADMAP.md` were not touched, since the orchestrator owns those.
