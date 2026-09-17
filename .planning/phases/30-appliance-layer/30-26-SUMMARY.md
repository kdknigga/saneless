---
phase: 30-appliance-layer
plan: 26
subsystem: web
tags: [rate-limit, health-checks, denial-of-service, htmx, tdd, wr-05]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "CheckRefresher.probe_now() as the one single-flighted probe path (30-25), refresh_checks reduced to note_watcher() + probe_now() (30-25), CheckCache's injectable monotonic clock and its entry lock (30-07)"
provides:
  - "CheckCache.claim_manual_refresh(min_interval=MIN_MANUAL_REFRESH_SECONDS): a grant at most once per interval, taken under the cache's own lock"
  - "MIN_MANUAL_REFRESH_SECONDS = 2.0: the floor under the deliberate TTL bypass"
  - "A refresh_checks that probes only on a granted claim and re-renders otherwise"
  - "docs/reference/web-api.md stating the POST's limit beside the GET's no-traffic guarantee"
affects: [appliance-layer, status-strip, scan-latency, 30-27]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Rate-limit by claim: the state that bounds a side effect lives with the state the side effect writes, not in the handler"
    - "A refused request renders the same partial from the same read as a granted one, so the response body reveals nothing about the limiter"
    - "Overlap pinned with a re-entrant clock double: the second call happens inside the first one's own clock read, so the overlap is a fact of the call stack"
    - "Substitute a collaborator where the app builds it (monkeypatching the module attribute create_app reads), not on app.state afterwards, so every holder shares the one instance"

key-files:
  created: []
  modified:
    - src/saneless/web/checks_cache.py
    - src/saneless/web/routes.py
    - tests/test_checks_cache.py
    - tests/test_web_checks.py
    - docs/reference/web-api.md

key-decisions:
  - "The claim stamp is a second piece of state under the existing entry lock rather than a lock of its own: the read-and-rebind must be atomic against another request thread, and the cache already owns exactly one lock whose scope comment can name both"
  - "The clock is read before the lock is taken, never inside it -- which the re-entrant test pins by construction: a claim that read the clock under the lock would deadlock there rather than fail an assertion"
  - "A refusal changes no state at all, so a caller hammering the endpoint just inside the interval cannot hold the floor shut against the household member standing at the appliance"
  - "The route calls claim_manual_refresh() with no argument: the interval is a call-time default, and a test shortens the *clock* rather than the argument, so the production call site carries no test affordance"
  - "_make_app gained stub_connection=False rather than a second app builder, so the one test that counts Paperless requests at the transport shares every other line of the app the rest of the module drives"

patterns-established:
  - "Counting a rate limit where it costs something: the amplifier assertion counts requests an httpx.MockTransport received, not calls a spy saw, so it is about traffic that would really have left the appliance"

requirements-completed: [APPL-02]

# Metrics
duration: 22min
completed: 2026-09-16
---

# Phase 30 Plan 26: A Floor Under the Refresh Button Summary

**`POST /api/checks/refresh` now probes only on a claim `CheckCache` grants at most once every two seconds, so a scripted loop that used to turn twenty requests into twenty paperless-ngx requests, twenty rounds of saned dials and forty filesystem writes now costs exactly one -- while a click that arrives too soon re-renders the current strip with the same status code and a byte-identical body, and a click after the interval still bypasses the 30 s TTL exactly as D-09 promises.**

## Performance

- **Duration:** ~22 min
- **Started:** 2026-09-16T23:13Z (local 23:13 CDT)
- **Completed:** 2026-09-16T23:35Z
- **Tasks:** 2 (both TDD, 4 commits: two RED, two GREEN)
- **Files modified:** 5 (+459 / −13)

## Measured amplifier: before and after

The plan asks for the loop measured against a real appliance, not only through a spy. Measured
with a real uvicorn server on loopback, a stub scanner backend, a real `PaperlessClient` whose
transport counts every request, and twenty `POST /api/checks/refresh` issued back to back by an
httpx client sending **neither `Origin` nor `Sec-Fetch-Site`** -- the header-less shape
`CrossOriginGuard` allows by design, and the shape `curl` in a loop sends:

| Shape | 20 POSTs → paperless-ngx requests | Status codes |
|-------|-----------------------------------|--------------|
| Before this plan (the route probes unconditionally) | **20** | all 200 |
| After this plan (the route claims first) | **1** | all 200 |

The "before" figure is the same binary with the claim forced to grant, which is exactly the code
this plan replaced. Each of those twenty requests also carried up to N saned TCP dials and two
`NamedTemporaryFile` write tests, and each one contended for the scanner gate that
`ScanWorker._scan_job` blocks on -- that is the part WR-05 called scan starvation, and it is now
bounded at one probe per two seconds no matter how the endpoint is driven.

Unit-level, the same scenario is pinned twice over: `spy.calls == 1` for twenty POSTs, and
`counter.count == 1` for twenty POSTs against an `httpx.MockTransport`.

## Accomplishments

- **WR-05 closed.** `CheckCache.claim_manual_refresh` keeps its own `_last_manual_claim`, reads
  the injected clock *before* taking the cache's lock, and rebinds the stamp under it. Two
  request threads arriving together get one grant between them (T-30-26-01); a serial loop --
  the case 30-25's single flight explicitly left open -- gets one grant per interval.
- **A refusal is invisible.** The refused response is a 200 carrying `partials/checks.html`
  rendered from the same `_checks_context(state)` read a granted one uses. Pinned to be
  **byte-identical to what `GET /api/checks` renders**, so there is nothing in the body that
  could tell a user, or a prober, that the limiter turned them away (T-30-26-04).
- **D-09 survives.** A click after the interval probes again, and the interval is 2 s against a
  30 s TTL: the promise D-09 actually makes -- "do not make somebody who just plugged the scanner
  back in wait out the cache" -- is untouched. Pinned on the injected clock, so the test costs no
  wall-clock time.
- **The floor is not a second TTL.** A `store` grants nothing, a grant stores nothing, and an
  expired entry grants nothing. All three are pinned, because conflating the two intervals would
  let a refused click reset the freshness of results it never produced.
- **A refusal moves nothing.** Three refusals in a row still let the first call after the
  interval succeed, so a loop cannot starve a legitimate click indefinitely.
- **The reference doc admits the limit.** `GET /api/checks` keeps its no-traffic guarantee word
  for word; the `POST /api/checks/refresh` section now says in prose that a refresh is honoured
  at most once every couple of seconds, that a sooner request re-renders without probing, and
  that the full bypass returns once the couple of seconds are out.
- **20 new tests**, suite at **3011 passing** (baseline 2991), no test removed, `tests/`
  `time.sleep` count unchanged at **17**.

## Task Commits

1. **Task 1: the cache grants a manual refresh at most once per interval** — `0eb6f37` (test, RED), `d5fa446` (feat, GREEN)
2. **Task 2: the refresh route honours the claim, and the docs admit the limit** — `4044b51` (test, RED), `6a445a6` (fix, GREEN)

No REFACTOR commit was needed: both GREEN commits landed clean under ruff, ruff-format, ty and
pyrefly.

## TDD Gate Compliance

Both tasks ran RED then GREEN in that order, with a `test(...)` commit preceding each
implementation commit.

- **Task 1 RED:** all 11 new cases failed, ten on
  `AttributeError: 'CheckCache' object has no attribute 'claim_manual_refresh'` and the
  concurrency case on `assert [] == [False, True]` (both threads died on the same
  `AttributeError`). Nothing passed unexpectedly.
- **Task 2 RED:** 3 of 9 failed, and they are the three that carry the plan's claim:
  `assert 2 == 1` for two clicks, `assert 20 == 1` for the loop at the spy, and
  `assert (20 - 0) == 1` for the loop at the Paperless transport. The other six are deliberate
  no-change guards -- the first click still probes, the watcher is still stamped, `GET` still
  claims nothing, D-09 still holds -- and are expected to pass at RED for the same reason plan
  30-25's `test_an_ungated_run_still_produces_every_row` was.

## Verification

| Gate | Result |
|------|--------|
| `uv run pytest -q` | 3011 passed (baseline 2991) |
| `uv run pytest tests/test_checks_cache.py -q` | 23 passed |
| `uv run pytest tests/test_web_checks.py -q` | 87 passed (78 at the parent commit; none removed) |
| `uv run pytest tests/test_checks_cache.py -q -k ClaimManualRefresh` | 11 selected, all passed (≥7 required) |
| `uv run pytest tests/test_web_checks.py -q -k RefreshMinimumInterval` | 9 selected, all passed (≥5 required) |
| `uv run ruff check .` | clean |
| `uv run ruff format --check .` | clean |
| `uv run ty check` | clean |
| `uv run pyrefly check src tests` | 0 errors |
| `grep -c "def claim_manual_refresh(" src/saneless/web/checks_cache.py` | 1 |
| `grep -c "MIN_MANUAL_REFRESH_SECONDS: Final = 2.0" src/saneless/web/checks_cache.py` | 1 |
| `grep -c "claim_manual_refresh()" src/saneless/web/routes.py` | 1 |
| `grep -c "probe_now()" src/saneless/web/routes.py` | 1 |
| The plan's one-liner `uv run python -c ...` claim/refuse/claim check | exits 0 |
| `grep -c "time\.sleep" tests/test_checks_cache.py` | 0 |
| `tests/` `time.sleep` count over source files | 17 (unchanged) |
| `git diff --stat docs/reference/web-api.md` | changed (1 file, +1 −1 paragraph) |
| Routes added or removed | none, so `_ROUTE_CALLS` / `_ROUTE_SKIPS` and `tests/test_app_lifespan.py` are untouched |

## Browser validation

Driven with Playwright directly (`uv run python` against a loopback uvicorn serving the real app
over a stub scanner and a counting Paperless transport), because the Playwright MCP tools were
not exposed to this executor. Per CLAUDE.md, nothing was deferred to a human.

- **Five rapid clicks on `Check again`** (roughly one every 120 ms): every response `200`, and
  **one** paperless-ngx request for the five of them. The four refusals are what a real button
  held down looks like, and the browser could not tell them from the grant.
- The strip after those clicks is intact: five rows, the real registry's sentences, the
  `Last checked …` freshness line, the `Check again` button still present, and no
  "not checked while a scan is running" beside an idle appliance.
- **After waiting the interval out**, the next click probed again -- D-09 through a real browser,
  not only through an injected clock.
- Screenshot captured and visually checked: Scanner / Paperless / Data folder green, Profiles and
  Fallback amber with their next steps, nothing that reads as an error anywhere on the card.

## Deviations from Plan

None. Both tasks were executed as written, with two structural choices taken inside the plan's
latitude:

- **`_make_app` gained `stub_connection: bool = True`** rather than a second app builder. The
  test that counts requests at the transport needs the real `test_connection` to reach that
  transport, and every other line of the app it drives should stay the one the rest of the module
  drives. Default behaviour is unchanged, so no existing test is affected.
- **The clocked app substitutes `CheckCache` where `create_app` builds it** (monkeypatching
  `saneless.web.app.CheckCache`) rather than rebinding `app.state.checks` afterwards. Rebinding
  afterwards would leave the refresher holding the original cache, and the route and the
  refresher must share the one instance exactly as they do in production.

## Issues Encountered

**The plan's `grep -rc "time\.sleep" tests/ | awk ...` gate reports 19, not 17 -- and the source
count is unchanged.** `grep -rc` over `tests/` also descends into `tests/__pycache__/`, and a
stale `test_paperless.cpython-314-pytest-9.0.2.pyc` contains the byte sequence twice, adding 2 to
the sum on any checkout where the suite has been run. Over the ten source files that match, the
count is **17**, identical to the parent commit, and neither file this plan touched contains a
sleep (`tests/test_checks_cache.py` is 0 and `tests/test_web_checks.py` is 0). The criterion's
intent -- "this plan adds no sleeping test" -- holds exactly; only the command's arithmetic is
affected, and it is affected by build artifacts rather than by anything in this change. Verified
with `grep -rn "time\.sleep" tests/`, which lists all 17 and their files.

## Residual, recorded deliberately

- **The floor is per-process, not per-caller.** Two different browsers on the LAN clicking within
  the same two seconds get one probe between them, not one each. That is the intended shape --
  the resource being protected is the scanner and paperless-ngx, not a per-client quota -- and
  the second clicker sees the strip the first one's probe produced, which is the answer they
  wanted anyway.
- **T-30-26-03 stays accepted.** `CrossOriginGuard` still allows a request carrying neither
  `Sec-Fetch-Site` nor `Origin`, by design and by REQUIREMENTS (the appliance has no
  authentication on the LAN). The floor is deliberately placed on the *cost* of a request rather
  than on its origin, so nothing here depends on identifying the caller.
- **Nothing bounds `POST /api/scan` this way.** It was already bounded by the worker's queue
  depth and by the exclusive scanner, and it is out of this plan's scope; recorded only so the
  next reader does not assume the refresh floor generalises.

## Known Stubs

None. No placeholder value, empty-collection default or "coming soon" string was introduced.

## Threat Flags

None. No network endpoint, auth path, file-access pattern or schema was added: the change is a
guard in front of an existing handler and one new method on an existing in-memory object. No
package was installed and `pyproject.toml` is untouched, so T-30-26-SC remains `n/a`.

### Threat register outcomes

| Threat ID | Disposition | Outcome |
|-----------|-------------|---------|
| T-30-26-01 | mitigate | **Closed.** One probe per interval for serial and concurrent callers alike; pinned by the twenty-request loop counted at the spy and at the transport, and measured at 20 → 1 against a real server. |
| T-30-26-02 | mitigate | **Closed.** With the probe rate capped and 30-25's hold window under 1 ms, the scanner gate is free between probes, so a refresh loop can no longer park a submitted job whose row reads `SCANNING`. |
| T-30-26-03 | accept | Unchanged and deliberate, as above. |
| T-30-26-04 | mitigate | **Closed.** The refused response is byte-identical to a cache read; there is no new error path and no state disclosed. |

## Self-Check

- `src/saneless/web/checks_cache.py` — FOUND
- `src/saneless/web/routes.py` — FOUND
- `tests/test_checks_cache.py` — FOUND
- `tests/test_web_checks.py` — FOUND
- `docs/reference/web-api.md` — FOUND
- `.planning/phases/30-appliance-layer/30-26-SUMMARY.md` — FOUND
- Commits `0eb6f37`, `d5fa446`, `4044b51`, `6a445a6` — all FOUND in `git log`

## Self-Check: PASSED
