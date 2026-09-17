---
phase: 30-appliance-layer
reviewed: 2026-09-17T00:00:00Z
depth: deep
round: 3
diff_base: d3f20a6c70fac1ff324589352ff0663806af85c3
files_reviewed: 12
files_reviewed_list:
  - docs/reference/web-api.md
  - src/saneless/checks.py
  - src/saneless/web/checks_cache.py
  - src/saneless/web/refresher.py
  - src/saneless/web/routes.py
  - src/saneless/web/templates/partials/checks.html
  - src/saneless/worker.py
  - tests/test_browser.py
  - tests/test_checks_cache.py
  - tests/test_checks.py
  - tests/test_refresher.py
  - tests/test_web_checks.py
findings:
  critical: 2
  warning: 4
  info: 6
  total: 12
status: issues_found
---

# Phase 30 (round 3): Code Review Report

**Reviewed:** 2026-09-17
**Depth:** deep (cross-file: `checks.py` → `refresher.py` → `routes.py` → template → `errors.py` → vendored htmx)
**Files Reviewed:** 12
**Status:** issues_found

## Summary

The eight round-2 findings are, with two exceptions, genuinely closed. The
deadline walk in `_saned_reachable` holds its bound (verified by construction
and by the scripted-clock test), the empty-resolver path cannot raise, the
gate-boundary split releases on every path including the exception path, a
preflight that settles the row takes the gate zero times, `probe_now`'s
"did I get the lock" contract is read correctly by its one caller, and the
manual-refresh floor cannot be defeated by the new `release_manual_claim`
(the first grant after an honoured probe is still ≥ `MIN_MANUAL_REFRESH_SECONDS`
away, because a release can only follow a grant that itself required the
interval to have elapsed). `POLL_ATTEMPT_CAP` is enforced at the route via
`Query(ge=0, le=POLL_ATTEMPT_CAP)`, and a crafted `attempt` is a 422 before
`_checks_context` sees it. `_scanner_busy`'s `CheckState.OK` cannot break an
exit code: `doctor` never passes a gate, so that row is unreachable from the
CLI, and `/health` does not consult the registry at all.

Two things do not hold.

The host parser has an **uncaught `ValueError`**: `_saned_hosts("host:²")`
raises, because `str.isdigit()` is true for Unicode digit characters that
`int()` refuses. It escapes the preflight into `run_checks`' generic handler,
so a single malformed `scanner.host` (or `SANE_NET_HOSTS`) value makes the
Scanner row a permanent red "This check could not be completed." and
`saneless doctor` exit 2. This is the same defect *class* as R2-IN-01 — a
non-`OSError` escaping the probe into the generic red row — reintroduced two
lines away from where R2-IN-01 was fixed. Reproduced end to end.

And **R2-IN-04's closure is invalid**. The new browser tests conclude "there is
no defect" by fabricating the failure response client-side with
`route.fulfill()`. Every error this application actually sends to an htmx
request goes through `render_error`, which sets `HX-Retarget: #status-message`
and `HX-Reswap: innerHTML`. htmx 2.0.8 applies `HX-Retarget` *before* the swap,
so the error partial lands in the scan-status slot and `#checks-body` — with
its `every 2s` trigger — is never replaced. The poll therefore does not end,
which is exactly what R2-IN-04 said, and `docs/reference/web-api.md` now
asserts the opposite in shipped prose. Verified against the real app and
against the vendored htmx source.

Beyond those: the "no all-digit segment is ever dialled" defence is incomplete
(glibc's short dotted, hex and octal IPv4 forms all contain a `.` and so pass
the guard — `0.0` and `0x0.0` still resolve to `0.0.0.0`), the refresher thread
has no per-tick exception backstop where `ScanWorker` does, and
`CheckResult.skipped` is dead data that two docstrings claim is rendered.

---

## Critical Issues

### R3-CR-01: `_saned_hosts` raises `ValueError` on a Unicode-digit port, making the Scanner row permanently red

**File:** `src/saneless/checks.py:671-680` (the `len(present) == 2` port branch)

**Issue:** The port branch gates `int(maybe_port)` on `maybe_port.isdigit()`.
`str.isdigit()` is `True` for Unicode category `No` characters — `²` (U+00B2),
`①` (U+2460) and friends — for which `int()` raises `ValueError`.
`_saned_hosts` catches nothing, `_scanner_preflight` catches nothing, and the
probe's `except OSError` is not reached (the raise happens before any socket
call). The exception escapes into `run_checks`' generic per-check handler.

Reproduced on this tree:

```
$ SANE_NET_HOSTS='scanbox:²' python -c '...run_checks(ctx)...'
Check SCANNER raised ValueError
SCANNER FAIL | This check could not be completed. | Restart saneless, then press Check again.
```

Consequences, all of them the ones this module's own docstrings promise cannot
happen:

- The Scanner row is red **permanently** — nothing about restarting changes the
  setting, so the next step printed can never help.
- `worst_state` is `FAIL`, so `saneless doctor` exits `ExitCode.CONFIG` (2) and
  a scripted health gate goes red on an appliance that scans fine.
- `_saned_hosts`' own docstring: "An operator who typed an IPv6 literal loses
  the pre-probe's latency saving and **never gets a wrong verdict**, which is
  the trade the whole module is built on." That trade is broken here.
- It is reachable from the environment, not only the config file:
  `_saned_host_setting` prefers `SANE_NET_HOSTS`.

Note also `'٦٥٦٦'` (Arabic-Indic digits) is accepted and yields port 6566 — no
crash, but the probe then dials a port libsane's C-side parsing would never
derive from that string, which is the precise divergence
`_saned_host_setting` exists to prevent.

**Fix:** restrict the digit test to ASCII decimal. `digits` is already imported
from `string` in this module:

```python
    if len(present) == 2:
        host, maybe_port = present
        # `str.isdigit()` is True for Unicode digits `int()` refuses (`²`,
        # `①`), and True for non-ASCII decimals libsane's C-side parsing
        # would read as a name.  ASCII decimal only, therefore.
        if (
            _looks_like_a_host_name(host)
            and maybe_port
            and set(maybe_port) <= frozenset(digits)
            and 0 < int(maybe_port) <= 65535
        ):
            return ((host, int(maybe_port)),)
```

`_looks_like_a_host_name`'s own `segment.isdigit()` needs no change — it only
ever *rejects*, and the charset check behind it already excludes non-ASCII — but
a test case pinning `_saned_hosts("host:²") == ()` and one pinning
`_saned_hosts("host:٦٥٦٦") == (("host", 6566),) is False` belong with it.

---

### R3-CR-02: the poll does **not** end on a failed request — R2-IN-04 is closed against a response the app never sends

**Files:**
`tests/test_browser.py:3167-3300` (`TestPollEndsOnAnErrorResponse`,
`_fail_the_checks_poll`),
`docs/reference/web-api.md:136`,
`src/saneless/web/templates/partials/checks.html:1-13`,
`src/saneless/web/errors.py:176-190` (`render_error`)

**Issue:** The new browser class asserts, and its docstring states outright,
that "Nothing in the source changes for this finding, and nothing should: there
is no defect", on the strength of `base.html`'s
`{"code":"[45]..","swap":true,"error":true}` rule. The measurement does not
exercise the application's error path. `_fail_the_checks_poll` answers the poll
with `route.fulfill(status=..., content_type="text/html", body=_CHECKS_ERROR_BODY)`
— a bare body with **no response headers**.

Every error this app returns to an htmx request is built by `render_error`,
which unconditionally sets, for `HX-Request: true`:

```
HX-Retarget: #status-message
HX-Reswap: innerHTML
```

Confirmed against the real app (`GET /api/checks?attempt=99`, the 422 the
route's own `Query(le=POLL_ATTEMPT_CAP)` bound produces):

```
422 {'hx-retarget': '#status-message', 'hx-reswap': 'innerHTML', ...}
```

And confirmed in the vendored `static/vendor/htmx-2.0.8.min.js`: `HX-Retarget`
rewrites `responseInfo.target` **before** `shouldSwap` is acted on, and only
`status === 286` cancels polling. So on any 4xx/5xx from `/api/checks`:

1. the error partial is swapped into `#status-message` (innerHTML);
2. `#checks-body` is **not** replaced, keeps `hx-trigger="every 2s"`, and keeps
   polling for as long as the tab is open — the unbounded poll IN-07 and
   R2-IN-04 are both about, and the one thing `POLL_ATTEMPT_CAP` cannot bound
   because the attempt counter only advances through bodies the server swaps in;
3. every 2 s the failing poll overwrites `#status-message`, which is the scan
   status slot the strip routes are explicitly documented to leave alone (D-03).
   A scan in progress has its progress line replaced by "The request was not
   valid. Reload the page, then try again." twice a minute.

The shipped documentation now states the false half as fact:

> "A poll whose request fails also ends, and for a simpler reason: the failure
> response replaces the strip, and the replacement carries no poll, so there is
> nothing left to fire."

Two passing browser tests plus a doc sentence that both point away from a live
defect are worse than the open finding was, because the next round is told to
read a number instead of re-running the argument.

**Fix (pick one, then re-point the tests at the real response):**

- Make the poll self-limiting on the client, so it does not depend on the swap
  target at all — on the conditional block in `checks.html`:
  ```html
  hx-on::response-error="this.removeAttribute('hx-trigger')"
  hx-on::send-error="this.removeAttribute('hx-trigger')"
  ```
- or exempt this route from the retarget, so the failure genuinely does replace
  the strip: have `get_checks` catch its own failure and return the strip
  partial with `poll_attempt=None` (a 200 carrying the last-known-good rows and
  no trigger), which also keeps the `Check again` button on the page;
- or suppress `HX-Retarget`/`HX-Reswap` for requests whose target is
  `#checks-body`.

For the test: drive the failure from the **server** (e.g. monkeypatch
`_checks_context` to raise, or request a tampered `attempt`) so the response
carries the headers the application really sends, and assert on
`#checks-body`'s survival and the request count over the window. `route.fulfill`
with hand-written bodies must not stand in for the app's own error rendering
anywhere in this class.

---

## Warnings

### R3-WR-01: the all-digit guard is a partial defence — `0.0`, `0x0.0`, `127.1` all pass it and all reach loopback

**File:** `src/saneless/checks.py:503-547` (`_looks_like_a_host_name`),
`src/saneless/checks.py:681-683` (the filtered return)

**Issue:** `_looks_like_a_host_name` rejects a segment only when
`segment.isdigit()`. The hazard it was written for is glibc's non-dotted-quad
numeric parsing, and that parsing accepts far more than all-digit strings.
Measured on this machine:

| segment | `_looks_like_a_host_name` | `getaddrinfo(..., 6566)` |
|---|---|---|
| `0.0` | True | `0.0.0.0` |
| `0x0.0` | True | `0.0.0.0` |
| `0x7f.1` | True | `127.0.0.1` |
| `127.1` | True | `127.0.0.1` |
| `6566.0` | True | NXDOMAIN (one unbounded lookup) |

So the sentence the docstring builds its case on — "on Linux a `connect()` to
`0.0.0.0` reaches loopback, so a `0` in the dial list lets the probe report the
configured scanner host 'reachable' off any unrelated local process listening on
6566 (R2-WR-01, T-30-28-01)" — is still true of the shipped parser; only the
spelling changed. And the parser can still *invent* such a segment from a
mistyped port rather than requiring the operator to type an address:
`_saned_hosts("host:0.0")` returns `(("host", 6566), ("0.0", 6566))`, so a
stray `.` in a port dials `0.0.0.0`.

Impact is the loss of the amber row rather than a false green: a spurious
"reachable" makes `any(...)` true, suppresses `_scanner_host_unanswered`, and
falls through to `get_devices()` — i.e. it reinstates the ~127 s uninterruptible
hang the pre-probe exists to avoid, on an appliance whose configured host is in
fact dead.

**Fix:** require a segment to be either a real IPv4 literal or to contain at
least one ASCII letter, which is what separates a name from every numeric form
glibc accepts:

```python
def _segment_is_a_numeric_address_shorthand(segment: str) -> bool:
    """True for the short dotted, hex and octal forms glibc reads as IPv4."""
    if any(character in ascii_letters for character in segment):
        return "x" in segment or "X" in segment  # 0x7f.1 and friends
    try:
        ipaddress.IPv4Address(segment)
    except ValueError:
        return True   # digits and dots that are not a legal literal: 0.0, 127.1
    return False
```

and reject in `_looks_like_a_host_name` when it answers True. The safe
direction is unchanged: a false "no" costs only the pre-probe's latency saving.

### R3-WR-02: `CheckRefresher._run` has no per-tick exception backstop, so one raise kills the thread for the life of the process

**File:** `src/saneless/web/refresher.py:207-218`, `src/saneless/web/refresher.py:313-326`

**Issue:** The loop is

```python
while not self._stopping.wait(TICK_SECONDS):
    self._tick()
```

with no `try`. `ScanWorker._run` (`worker.py:1076-1101`) wraps each iteration
in `try/except Exception` precisely so "nothing ends the loop but stopping
(ROBU-01)", and `CheckRefresher`'s own class docstring claims it "follows
`ScanWorker` in every structural respect D-07 names" — it does not follow this
one.

There is a live path to a raise. In `_probe_and_store`, the store is in the
`else` arm:

```python
try:
    ...
    results = run_checks(...)
except Exception:
    logger.exception("Check refresh failed; keeping the previous results")
else:
    self._cache.store(results)       # <-- not covered by the except above
finally:
    self._probe_lock.release()
```

Python does not route an exception raised in `else` to that `try`'s handlers, so
a raise from `store` (or from anything added to that arm later) propagates out
of `_probe_and_store`, out of `_tick`, and ends `_run`. The consequence is the
one `POLL_ATTEMPT_CAP`'s own comment names as the motivating case: "an appliance
whose refresher thread has died". The cache then never updates again, silently,
for the life of the process, and the strip's only remaining way to get results
is the `Check again` button.

The same raise out of `probe_now()` also 500s `POST /api/checks/refresh` with
the manual claim spent.

**Fix:** both halves.

```python
    def _run(self) -> None:
        while not self._stopping.wait(TICK_SECONDS):
            try:
                self._tick()
            except Exception:
                # The backstop ScanWorker._run has: a surprise must not end the
                # thread, because a dead refresher is a strip that never
                # updates again and says nothing about it.
                logger.exception("Check refresher tick failed; continuing")
```

and move the store inside the guarded region:

```python
        try:
            context = replace(self.build_context(), skip_scanner=self._scan_active())
            self._cache.store(run_checks(context, scanner_gate=self._scanner_gate()))
        except Exception:
            logger.exception("Check refresh failed; keeping the previous results")
        finally:
            self._probe_lock.release()
```

### R3-WR-03: `CheckResult.skipped` is dead data, and two docstrings claim it is what the surfaces render

**File:** `src/saneless/checks.py:908-972` (`_scanner_skipped`, `_scanner_busy`),
`src/saneless/web/templates/partials/checks.html:50-82`,
`src/saneless/cli.py:1161-1167`

**Issue:** `grep -rn skipped src/saneless/web/templates src/saneless/cli.py`
returns nothing. The template's `check_row` macro takes
`(name, state_class, glyph, state_label, message, next_step)` and `doctor`
prints `_state_marker(result.state)`. Neither surface reads the flag. So both
skipped rows render as a **green ✓** with the screen-reader word "OK" in front
of a sentence that says nothing was checked:

```
✓  Scanner   The scanner was busy, so it was not checked this time.
✓  Scanner   Not checked while a scan is running.
```

`_scanner_skipped`'s docstring says "The `skipped` flag, **not the state**, is
what the two surfaces render", and `_scanner_busy`'s says "The `skipped` flag
discloses that nothing was checked." Both are false. The 30-30 summary records
the correct fact ("`CheckResult.skipped` is stored but nothing in the templates
or the CLI branches on it"), so the knowledge exists — it just never reached the
source, where the next maintainer will read it and rely on it.

This is a correctness problem in its own right, not only a comment problem: a
neutral cold-start glyph (`CHECKING_GLYPH`, `check-checking`,
`CHECKING_STATE_LABEL`) already exists for exactly "we did not look", and a
green tick claiming OK for an unprobed row is the same lie-by-marker the phase
rejects elsewhere.

**Fix:** either render the flag — pass `c.skipped` into `check_row` and select
the neutral glyph/class/label when it is set (and print a neutral marker in
`doctor`) — or delete the field and both claims. Rendering it is the smaller
change and the one the docstrings already promise; it needs no new colour token,
because the cold-start trio is already defined and already used by
`_CheckingRow`.

### R3-WR-04: the 20 s poll window is shorter than the worst-case probe the same module documents, so the give-up line can print over a probe that is still running

**File:** `src/saneless/checks.py:126-157` (`POLL_ATTEMPT_CAP`, `POLL_GAVE_UP_LINE`),
`src/saneless/web/routes.py:327-340`

**Issue:** The cap's justification is arithmetic: "about two and a half times
the worst probe budget a cold start can cost -- `PROBE_CONNECT_SECONDS` for
saned plus `PROBE_READ_SECONDS` for Paperless", i.e. 7 s against a 20 s window.
Three things this module documents elsewhere make that understate the worst
case by more than an order of magnitude:

- `getaddrinfo` is outside every budget — `PROBE_CONNECT_SECONDS`' own comment
  says "an unreachable resolver costs whatever `resolv.conf` says";
- the pre-probe is per configured host, and `_saned_hosts` puts no cap on the
  number of entries, so the budget is N × (resolution + 2 s);
- when there is **no** parseable host — the ordinary local-USB deployment —
  there is no pre-probe at all and `_scanner_enumeration` calls
  `get_devices()`, which the same file costs at "roughly 127 s for a silently
  unreachable host".

So a cold start on a wedged scanner reaches `attempt == POLL_ATTEMPT_CAP` at
~20 s with `results is None`, prints "The checks have not run yet. Press Check
again to try now.", and stops asking while the first probe is still legitimately
in flight. Pressing the button then collapses into that probe, emits a fresh
chain, and gives up again 20 s later — so the appliance can say "the checks have
not run yet" indefinitely while they are running. The settling poll
(`keep_asking = ... or probe_in_flight`) has the same shape: on a probe longer
than ~20 s the chain expires before the store, which reproduces exactly the
"Check again visibly does nothing" symptom R2-WR-03 was raised to close.

**Fix:** decide which bound is authoritative and make the other follow. Either
raise the cap so the window exceeds the probe's real worst case (and correct the
comment's arithmetic to name `getaddrinfo`, N hosts and `get_devices()`), or
keep 10 attempts and distinguish the two endings in `_checks_context` — a chain
that ran out **while `probe_in_flight`** should not print
`POLL_GAVE_UP_LINE`, because a probe is demonstrably running; give it a sentence
that says so, or let `probe_in_flight` extend the chain past the cap with its
own (larger) bound. At minimum the comment must stop asserting a 7 s worst case
it can compute is wrong.

---

## Info

### R3-IN-01: the one test named as the guarantee that `doctor` and the strip agree covers one of four scenarios

**File:** `tests/test_checks.py:2432-2450`, `src/saneless/checks.py:1421-1428`

`_scanner_result`'s docstring names
`test_a_gated_run_returns_what_an_ungated_run_returns` as the replacement for
the uniform-dispatch seam the split removed, and the 30-30 summary calls it
"the **only** guarantee that doctor and the strip agree". The test runs both
paths with a free `_RecordingLock`, no configured host and a backend reporting
one device — the single scenario where the gate is irrelevant. It would not
catch a divergence in the no-python-sane row, the host-unanswered row, or an
enumeration failure. **Fix:** parametrise it over those four contexts
(scanner `None`; host configured and refusing; host configured and answering;
enumeration raising) and assert `gated == ungated` for each.

### R3-IN-02: `test_three_resolved_addresses_share_one_budget` cannot fail for the property it is named after

**File:** `tests/test_checks.py:769-795`

The assertions are `timeouts[0] <= budget`, `all(t > 0)` and
`timeouts == sorted(timeouts, reverse=True)`. A per-socket implementation
(`probe.settimeout(timeout)`) produces `[2.0, 2.0, 2.0]`, which is equal to its
own reverse-sort, so all three assertions pass on the code this test exists to
forbid. The property is in fact pinned — by
`test_the_deadline_stops_the_walk`'s scripted clock — but this case is decorative.
**Fix:** assert the sum, e.g. `sum(recorder.timeouts) <= _PROBE_BUDGET * 1.01`,
or script the clock to advance a known amount per attempt and assert the exact
remainders.

### R3-IN-03: `release_manual_claim` clears unconditionally, and its safety argument has no test

**File:** `src/saneless/web/checks_cache.py:245-275`

The clear is `self._last_manual_claim = None` with no check that the stamp being
cleared is the caller's own. The docstring's argument holds today — the only
caller releases microseconds after its grant, on the same thread, and a
competing claimer inside that window is refused without writing — but it is a
narrative invariant about one call site, not an enforced one, and none of the
five new `TestReleaseManualClaim` cases exercises two threads. **Fix:** make it
a compare-and-clear (`claim_manual_refresh` returns the stamp it wrote, or
`release_manual_claim(stamp)` clears only if `self._last_manual_claim == stamp`),
which costs one parameter and removes the reasoning entirely.

### R3-IN-04: three `_saned_hosts` behaviours diverge from its docstring

**File:** `src/saneless/checks.py:590-683`

- "The stray colon at either edge stays tolerated, because `: host-a :` has only
  ever meant one host" — true for a bare name, false in combination with a port:
  `_saned_hosts("localhost:6566:")` and `_saned_hosts(":localhost:6566")` both
  return `()`, because the `len(segments) > 2` refusal sees `6566` and rejects
  the whole setting.
- `_saned_hosts("host:065")` returns `(("host", 65))` — a leading-zero port is
  read as decimal 65 with no comment on whether libsane agrees.
- `_saned_hosts("scanner.local.")` returns `()`: a legal fully-qualified name
  with a root dot is dropped by the leading/trailing-dot rule.

All three are the safe direction (a lost pre-probe, never a wrong verdict), so
this is a docstring correction plus, optionally, tolerating the root dot.

### R3-IN-05: the dial list has no length cap, so one refresh POST can hold a request thread for minutes

**File:** `src/saneless/checks.py:590-683`, `src/saneless/checks.py:1030-1037`

`_saned_hosts("h1:h2:...:h40")` yields 40 entries when every segment looks like
a name, and `_scanner_preflight` walks them with `any(...)`, paying
`getaddrinfo` (unbounded) plus `PROBE_CONNECT_SECONDS` for each. That whole walk
runs inside the `POST /api/checks/refresh` request thread (via
`probe_now`), so a 40-host setting is a request that can take minutes. The
scanner gate is correctly free throughout (R2-IN-03's fix), so no scan is
parked — but the manual-refresh floor does not bound duration, only rate.
**Fix:** cap the entries (`[:_MAX_PROBE_HOSTS]`, 4 or so) and say in the
docstring that a longer list loses the pre-probe for its tail.

### R3-IN-06: a scan that starts between `_scan_active()` and the gate acquire renders the contention row, which the docs say names only contention

**File:** `src/saneless/web/refresher.py:316-317`, `src/saneless/checks.py:1441-1442`,
`docs/reference/web-api.md:148`

`skip_scanner` is sampled once, before `run_checks`; `_process_job` sets
`_current_job_id` before `_scan_job` takes the gate. In the window between the
sample and the acquire a real scan can take the gate, and the row rendered is
then `_scanner_busy()` — while `_checks_context` reads `scan_active` at render
time and prints "Paused during scan — last checked …" above it. The message is
not false, but the docs state the busy row appears "when another check is
holding the scanner briefly -- the capability read saneless does at start-up,
for instance", which reads as exhaustive. A sentence acknowledging the race
would keep the docs honest; no code change is needed.

---

_Reviewed: 2026-09-17_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
_Round: 3 (diff base d3f20a6)_
