---
phase: 30-appliance-layer
reviewed: 2026-09-17T00:00:00Z
depth: deep
files_reviewed: 24
files_reviewed_list:
  - docs/reference/web-api.md
  - src/saneless/checks.py
  - src/saneless/cli.py
  - src/saneless/config.py
  - src/saneless/pipeline.py
  - src/saneless/vocabulary.py
  - src/saneless/web/app.py
  - src/saneless/web/checks_cache.py
  - src/saneless/web/refresher.py
  - src/saneless/web/routes.py
  - src/saneless/web/templates/partials/checks.html
  - src/saneless/worker.py
  - tests/test_app_lifespan.py
  - tests/test_browser.py
  - tests/test_checks_cache.py
  - tests/test_checks.py
  - tests/test_config.py
  - tests/test_doctor.py
  - tests/test_pipeline.py
  - tests/test_refresher.py
  - tests/test_vocabulary.py
  - tests/test_web_checks.py
  - tests/test_web.py
  - pyproject.toml
findings:
  critical: 1
  warning: 3
  info: 5
  total: 9
status: issues_found
---

# Phase 30 gap closure: Code Review Report

**Reviewed:** 2026-09-17
**Depth:** deep (cross-file: import graph, call chains, lock-ordering, template/route contract)
**Diff base:** `3199941..HEAD` (plans 30-20 .. 30-27)
**Status:** issues_found

## Summary

Thirteen of the sixteen prior findings are genuinely closed, with real tests behind
each one rather than a claim in a SUMMARY. The three that are not fully closed
(CR-02, WR-01, WR-04) are closed *in the direction* the prior review asked for and
leave a narrower residual in each case, and one of them (WR-04) is contradicted by a
sentence the same round added to `docs/reference/web-api.md`.

The concurrency work is the strongest part of the round and the review could not break
it. Lock ordering is acyclic: `_probe_lock` → `scanner_gate` (non-blocking) →
`cache._lock`, with the cache lock never held across a probe and `_tick`'s
`is_fresh()` releasing before `_probe_and_store` takes the probe lock. The worker takes
only `scanner_gate` and never `_probe_lock`, so no deadlock is constructible. Every
acquire of `scanner_gate` outside the worker is `blocking=False` with a `finally`
release. `claim_manual_refresh`'s read-then-rebind is correct under interleaving,
including the stale-clock case (a thread whose `now` predates the stored stamp is
refused and, importantly, does not stamp — so a refusal can never move the floor
backwards). The shutdown arithmetic is correct: `max(0.0, deadline - monotonic())`
cannot go negative, `stop(timeout=0.0)` uses `is None` rather than falsiness so a
spent budget is a poll and not `join(None)`, and the leave-resources-open branch is
untouched. The htmx poll cannot self-re-arm: `load` rides only on `poll_attempt == 1`,
which is only ever produced by an `attempt=0` request, and every polled body carries
`every 2s` alone. A refused refresh is byte-identical to a served one (same handler,
same partial, same 200).

`junit_family = "legacy"` is genuinely inert: no workflow in `.github/workflows/` and
no script passes `--junitxml`, and a manual `pytest --junitxml=…` run over
`tests/test_checks_cache.py` completes with no warning and no error. It changes the
XML schema only, and masks nothing — `filterwarnings = ["error"]` is untouched.

Toolchain is clean on the tree as it stands: `ruff check` (no issues), `ruff format
--check` (63 files formatted), `ty check` (all checks passed), `pyrefly check src
tests` (0 errors), and 2896 non-browser tests pass. There are zero `# noqa`,
`# type: ignore` or rule-disabling suppressions anywhere in the diff.

What the review did find is one new functional regression introduced by the WR-02 fix
— the saned pre-probe now dials only the *first* resolved address, which is enshrined
in a test — plus two residuals that the round's own documentation overclaims about.

---

## Prior Findings Verification

| ID | Verdict | Evidence |
|----|---------|----------|
| CR-01 | **CLOSED** | `config.py:673` `profile_storage_for_loaded` is now the single derivation; called from `worker.py:911` (generation skipped), `worker.py:917` (SANE failure) and `cli.py:1154`. Six worker tests pin it (`tests/test_worker.py:5921-6060`), including a test that renders the row from the worker's value and compares it with `doctor`'s. |
| CR-02 | **CLOSED-WITH-CONCERN** | The false **red** row and `doctor` exit 2 are gone: a refused pre-probe now returns the amber `_scanner_host_unanswered()` (`checks.py:744`, used at `checks.py:853`). The env-var divergence is closed by `_saned_host_setting` (`checks.py:469`, used at `checks.py:850`). **Concern:** the prior review asked for the probe to stop *replacing* `get_devices()`'s answer; it still does. A machine with a working USB scanner and a stale `scanner.host` never enumerates and reports amber "the scanner could not be checked". This is a recorded, reasoned trade (see the docstring at `checks.py:744-770`), not an oversight — logged below as IN-02. |
| WR-01 | **CLOSED-WITH-CONCERN** | `::`-bearing and bracketed literals are now refused (`checks.py:587-591`, `_looks_like_a_host_name` at `checks.py:499`); verified `_saned_hosts("fe80::1") == ()` and `_saned_hosts("[fe80::1]:6566") == ()`. **Concern:** a *fully expanded* IPv6 literal has no blank segment and only hex-safe characters, so it is not refused — see WR-01 below. |
| WR-02 | **CLOSED-WITH-CONCERN** | The unbounded-DNS and per-address multiplication are both now stated honestly (`checks.py:80-92`, `checks.py:616-633`) and the multiplication is removed in code (`checks.py:657-662`). **Concern:** the removal was done by dialling only `getaddrinfo(...)[0]`, which is a new correctness regression — see CR-01 below. |
| WR-03 | **CLOSED** | `run_checks(context, *, scanner_gate=None)` (`checks.py:1186`) holds the gate for `_check_scanner` alone via `_scanner_result` (`checks.py:1154-1183`, release in `finally`). Both call sites pass it rather than wrapping (`refresher.py:274`; the route's own copy of the block is deleted). `tests/test_refresher.py:363` asserts the gate is free during `run_checks`. Minor residual logged as IN-03. |
| WR-04 | **CLOSED-WITH-CONCERN** | `skip_scanner` now comes from the worker's job id (`refresher.py:273`, wired at `app.py:162`), the same fact `_checks_context` renders (`routes.py:305`), and two checkers can no longer contend at all (`_probe_lock`, `refresher.py:270`). **Concern:** gate contention is still *rendered* as "a scan is running" (`checks.py:1178-1179` returns `_scanner_skipped()`), and the worker's startup capability read holds the gate with no job in flight (`worker.py:947`) — see WR-02 below. `docs/reference/web-api.md:147` now asserts this cannot happen. |
| WR-05 | **CLOSED** | `MIN_MANUAL_REFRESH_SECONDS = 2.0` and `claim_manual_refresh` (`checks_cache.py:198-243`), gating the probe at `routes.py:1342`. A refusal re-renders the same partial with the same status. Eleven cache tests (`tests/test_checks_cache.py:214-272`) plus route tests. Documented at `docs/reference/web-api.md:151`. Interaction defect logged as WR-03 below. |
| WR-06 | **CLOSED** | The fallback is now gated on the config key, never the submitted value (`routes.py:1073-1076`). Covered from both directions in `tests/test_web.py:2323-2592` (control on + cleared list → `[]`; control off → profile default). |
| WR-07 | **CLOSED** | One shared deadline (`app.py:196-201`), `stop(timeout=…)` with `max(0.0, …)` clamping and an `is None` default (`refresher.py:173, 198-199`), and both docstrings corrected (`app.py:170-177`, `refresher.py:154-165`). Four budget tests at `tests/test_app_lifespan.py:505-543` including the overspent case. |
| IN-01 | **CLOSED** | `_tag_list_context` returns the normal key set emptied when `show_tags` is off (`routes.py:401-407`) — key-for-key identical to the normal return, so no template name goes undefined — and the correspondents fetch is gated at `routes.py:789-793`. |
| IN-02 | **CLOSED** | `routes.py:874` now logs `type(exc).__name__`; `grep` finds no remaining `%s`-with-exception-object in the module. |
| IN-03 | **CLOSED** | `%r` at `pipeline.py:440`, `pipeline.py:1572`, `pipeline.py:2118-2119` and `pipeline.py:2216`; `grep -n "'%s'" src/saneless/pipeline.py` returns nothing. |
| IN-04 | **CLOSED** | Resolved the second way the prior review permitted: the property docstring now states the discipline explicitly (`worker.py:771-782`) and a pointer sits at the attribute (`worker.py:409-412`). |
| IN-05 | **CLOSED** | `config.py:241-244` no longer cites deleted lines and states the upgrade-path justification instead. |
| IN-06 | **CLOSED** | `vocabulary.py:632` `.rstrip()`, with the trailing-only reasoning in the docstring; `tests/test_vocabulary.py` covers the empty-`%Z` host. |
| IN-07 | **CLOSED** | `POLL_ATTEMPT_CAP = 10` (`checks.py:144`), `poll_attempt` computed server-side (`routes.py:306-318`), bounded at the route (`routes.py:1248`), emitted only inside the conditional and with `load` only on the first link (`partials/checks.html:53-58`). A Chromium test asserts exactly `POLL_ATTEMPT_CAP` requests and then stillness (`tests/test_browser.py:3096-3136`). Narrow residual logged as IN-04. |

---

## Critical Issues

### CR-01: The saned probe dials only the first resolved address, so a name that resolves IPv6-first is reported "not answering" while it serves fine over IPv4

**File:** `src/saneless/checks.py:657-663`

**Issue:** WR-02's fix replaced `socket.create_connection((host, port), timeout=…)`
with a single resolve-and-dial:

```python
family, socket_type, protocol, _canonical_name, address = socket.getaddrinfo(
    host, port, type=socket.SOCK_STREAM
)[0]
with socket.socket(family, socket_type, protocol) as probe:
    probe.settimeout(timeout)
    probe.connect(address)
    return True
```

`create_connection` tries *every* resolved address and succeeds if any one connects.
This tries exactly one and calls the host dead if that one fails. `socket.getaddrinfo`
is called with `flags=0`, so `AI_ADDRCONFIG` is **not** set and glibc returns AAAA
records even on a host with no IPv6 route — and RFC 6724 puts the IPv6 address first.
Measured on this machine:

```
localhost -> [(AF_INET6, …, ('::1', 6566, 0, 0)), (AF_INET, …, ('127.0.0.1', 6566))]
```

So for any `scanner.host` that is a *name* with both record types — `scanner.local` via
Avahi (link-local AAAA first, which cannot even be dialled without a scope id), a LAN
DNS name, `localhost` for a co-located saned, or any container whose v6 path to the
scanner is unrouted — the probe fails on address one, `any(...)` yields `False`, and
`_check_scanner` returns `_scanner_host_unanswered()` **without ever calling
`get_devices()`**. The appliance's primary check is then permanently amber, saying the
host is not answering, on a deployment where it answers and scans.

This is strictly a regression: the pre-gap-closure code would have fallen through to
`127.0.0.1` and connected. The documented deployments in `docs/` all use IPv4 literals
(one address, no impact), which is what limits the blast radius — but nothing in the
config validation or the docs requires a literal, and a hostname is the natural thing
to type.

The behaviour is also pinned by a test, so it will not be caught later:
`tests/test_checks.py:608-661` feeds an IPv6 address followed by two working IPv4
addresses and asserts `recorder.events.count("connect") == 1`.

The docstring's justification — "the answer for a host that is switched off is the
same on every address it has" (`checks.py:620-623`) — is true for a host that is off
and false for exactly the case that matters here: a host that is on, reachable on one
family and not the other.

**Fix:** keep WR-02's real bound (total ≤ `timeout` for one host) while restoring
multi-address behaviour, by sharing one deadline across the addresses instead of
discarding them:

```python
try:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
except OSError as exc:
    logger.debug("saned probe failed: %s", type(exc).__name__)
    return False
deadline = time.monotonic() + timeout
for family, socket_type, protocol, _canonical_name, address in infos:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    try:
        with socket.socket(family, socket_type, protocol) as probe:
            probe.settimeout(remaining)
            probe.connect(address)
    except OSError as exc:
        logger.debug("saned probe failed: %s", type(exc).__name__)
        continue
    else:
        return True
return False
```

The WR-02 property is preserved and testable as stated ("three addresses cost one
*budget*"), so `tests/test_checks.py:608-661` should be rewritten to assert elapsed
budget rather than attempt count, plus a new case: first address refuses, second
connects, result is `True`.

---

## Warnings

### WR-01: A fully expanded IPv6 literal still fans out into junk hosts, and digit-only segments resolve to arbitrary IPv4 addresses

**File:** `src/saneless/checks.py:587-596`

**Issue:** the refusal fires only when a blank segment sits in the interior or a
segment contains a character `_looks_like_a_host_name` rejects. A fully expanded IPv6
literal has neither. Verified against the shipped function:

```
'fe80::1'               -> ()                      # refused, correct
'[fe80::1]:6566'        -> ()                      # refused, correct
'2001:db8:0:0:0:0:0:1'  -> (('2001', 6566), ('db8', 6566), ('0', 6566), … 8 entries)
'host-a:99999'          -> (('host-a', 6566), ('99999', 6566))
```

The second row matters more than it looks, because those "host names" are all digits
and glibc's resolver accepts the single-integer form of an IPv4 address:

```
getaddrinfo('2001', 6566) -> 0.0.7.209:6566
getaddrinfo('99999', 6566) -> 0.1.134.159:6566
getaddrinfo('0', 6566)     -> 0.0.0.0:6566     # connect(0.0.0.0) reaches localhost
```

So an operator who types an expanded IPv6 address makes the appliance open TCP
connections to `0.0.7.209` and friends — addresses nobody configured — and, through
CR-01's short circuit, never enumerates the scanner. The `0.0.0.0` entry is worse in a
different direction: on Linux `connect()` to `0.0.0.0` reaches loopback, so the probe
can report "reachable" because something unrelated listens on port 6566 locally.

The `host-a:99999` case also falsifies the docstring's own safety claim
(`checks.py:550-556`): "passed to `getaddrinfo` it is truncated modulo 65536 …
a real probe of an address nobody configured. Reading such a segment as a host name
avoids both." It does not avoid it — it dials `0.1.134.159:6566` instead of
`host-a:34463`. Both are addresses nobody configured.

**Fix:** two small additions, both stdlib.

1. Refuse anything the stdlib recognises as an IPv6 address, before splitting:

```python
import ipaddress

stripped = host_setting.strip()
with suppress(ValueError):
    if ipaddress.ip_address(stripped.strip("[]").partition("%")[0]).version == 6:
        return ()
```

2. Make `_looks_like_a_host_name` reject all-digit segments (no legal hostname is
   all digits, and every all-digit segment here is either a mis-parsed port or half an
   IPv6 literal):

```python
if segment.isdigit():
    return False
```

Then `host-a:99999` yields `(("host-a", SANED_PORT),)` and the `99999` segment is
dropped rather than dialled; update `tests/test_checks.py:371-383` accordingly, and add
`_saned_hosts("2001:db8:0:0:0:0:0:1") == ()`.

---

### WR-02: Gate contention is still rendered as "a scan is running", and the worker's own startup capability read is a contender with no job in flight

**File:** `src/saneless/checks.py:1178-1179`, `src/saneless/worker.py:947`, `docs/reference/web-api.md:147`

**Issue:** WR-04's fix corrected where `skip_scanner` comes from, and single-flighted
the two web checkers so they cannot contend. It did not change what a *failed gate
acquire* renders as:

```python
if not scanner_gate.acquire(blocking=False):
    return _scanner_skipped()
```

and `_scanner_skipped()` is the row whose message is literally
`"Not checked while a scan is running."` (`checks.py:809`). So the question is only
whether anything other than a scan can hold that gate. It can:
`ScanWorker._read_generated_profiles` takes `with self._scanner_gate:` around
`get_devices()` **and** `get_capabilities()` (`worker.py:947`) as the worker thread's
first act at startup, while `_current_job_id` is still `None` (it is set at
`worker.py:1394`, inside `_process_job`).

That window is not hypothetical — it is the *most* likely contention window in the
product, because it coincides exactly with the cold-start poll:

1. Lifespan starts the worker, then the refresher (`app.py:290-295`).
2. A browser on the appliance loads the page; the cold cache emits the poll.
3. `note_watcher` stamps, the refresher's next tick probes, `_check_scanner` tries the
   gate, the startup capability read owns it (a real scanner's `get_devices` +
   `get_capabilities` is seconds; an unreachable net host is up to ~127 s).
4. `_scanner_skipped()` is stored. The strip renders "Not checked while a scan is
   running." with `scan_active=False`, i.e. beside `Last checked 14:02.` — the exact
   self-contradicting body WR-04 described, on an appliance that has never scanned.

The same round also added a sentence to `docs/reference/web-api.md:147` claiming this
is impossible: "the decision is taken from the job the scan worker reports it is
running, not from whether some other health check happened to be busy at the same
moment, so it cannot show up beside a last-checked time on an idle appliance." It can,
by the path above — and, by the refresher's own admitted residual
(`refresher.py:253-258`), also for up to one TTL after any scan ends. Two independent
falsifications of one user-facing guarantee.

The existing regression test does not catch it because it stubs `run_checks` and
asserts only the flag: `tests/test_refresher.py:340-359` checks
`spy.calls[0].skip_scanner is False` and never renders the row the real registry would
have produced under a held gate.

**Fix:** stop overloading one row with two facts. `_scanner_result` knows it lost a
race; only `context.skip_scanner` knows a scan is running. Give contention its own
neutral outcome:

```python
def _scanner_result(context: CheckContext, scanner_gate: threading.Lock) -> CheckResult:
    if not scanner_gate.acquire(blocking=False):
        # Not _scanner_skipped(): that row names a running scan, and run_checks
        # has already handled that case above.  Something else holds the gate --
        # today, the worker's startup capability read -- and the honest report is
        # that this cycle did not look.
        return _scanner_busy()
    ...
```

where `_scanner_busy()` is `CheckState.OK, skipped=True` with a message that names no
scan ("The scanner was busy; not checked this time."). Alternatively, have the probe
store nothing for the scanner row when it lost the gate and no scan is running, so the
previous row survives. Either way, correct
`docs/reference/web-api.md:147` so it does not promise a property the code does not
have, and extend `tests/test_refresher.py:340` to assert the *rendered row*, not the
flag.

---

### WR-03: A Check again click that collides with the background probe is swallowed, leaves the strip showing pre-probe results, and burns the 2 s claim

**File:** `src/saneless/web/routes.py:1341-1348`, `src/saneless/web/refresher.py:270-271`

**Issue:**

```python
state.refresher.note_watcher()
if state.checks.claim_manual_refresh():
    state.refresher.probe_now()
return state.templates.TemplateResponse(request, "partials/checks.html",
                                        _checks_context(state))
```

`probe_now` → `_probe_and_store` returns silently when `_probe_lock` is already held
(`refresher.py:270`). Three consequences compose badly:

1. The claim is **consumed** even though no probe ran — `claim_manual_refresh` stamped
   before `probe_now` discovered it had nothing to do. The user's next click within
   2 s is refused.
2. The response renders `_checks_context(state)` from the cache *as it stands now*,
   which is the pre-probe entry: the in-flight probe has not called `store()` yet.
3. Once results exist, `poll_attempt` is `None` (`routes.py:316-318`), so the body
   carries **no** htmx trigger. Nothing on the page will ever pick up the result the
   in-flight probe lands a second later. The strip is only refetched by another click,
   the terminal-state reload, or a page load.

So the appliance's one manual control can visibly do nothing, twice in a row, with no
indication — and `docs/reference/web-api.md:149` tells the user the opposite: "The
answer the in-flight refresh is about to produce is the same answer, seconds away."
It is, but nothing delivers it.

The window is one background probe's duration (the Paperless budget alone is up to
5 s, `PROBE_READ_SECONDS`) out of every TTL, while somebody is watching — which is
precisely when the button gets pressed.

**Fix:** let the collapse be visible to the caller and to the page. Make `probe_now`
report whether it probed, and do not spend the claim on a collapse:

```python
def probe_now(self) -> bool:
    """Returns whether this call actually probed."""
    return self._probe_and_store()   # False when the probe lock was held
```

```python
state.refresher.note_watcher()
if state.checks.claim_manual_refresh() and not state.refresher.probe_now():
    # Another checker owns the probe; its store is imminent and this body would
    # otherwise be the last word.  Give the claim back and let the strip ask once.
    state.checks.release_manual_claim()
    return state.templates.TemplateResponse(
        request, "partials/checks.html", _checks_context(state, attempt=0, poll_once=True)
    )
```

The minimum viable fix is smaller: have the collapsed branch render with a one-shot
`hx-trigger="load delay:1s"` body so the imminent result reaches the page. Add a route
test that holds `_probe_lock`, POSTs the refresh, and asserts the response body still
asks for itself.

---

## Info

### IN-01: `getaddrinfo(...)[0]` can raise `IndexError`, which the probe's `except OSError` does not catch

**File:** `src/saneless/checks.py:657-659`
`socket.getaddrinfo` normally raises `gaierror` (an `OSError`) rather than returning an
empty list, so this is defensive rather than observed — but the failure mode is worth
closing since the surrounding code is written to never raise: an `IndexError` escapes
`_saned_reachable`, escapes `_check_scanner`, and is caught only by `run_checks`'
per-check handler, turning a resolver oddity into a red Scanner row with the generic
"check failed" text. The CR-01 fix above (iterating `infos`) removes the subscript and
therefore this case at the same time. If CR-01 is fixed differently, guard with
`if not infos: return False`.

### IN-02: The pre-probe still replaces `get_devices()`'s answer rather than cheapening it

**File:** `src/saneless/checks.py:850-854`
CR-02's prior fix suggestion — "let it degrade the *cost* of `get_devices()`, never
replace its answer" — was not taken; the verdict was softened from red to amber
instead. The consequence is recorded honestly in `_scanner_host_unanswered`'s docstring
(`checks.py:744-770`) and accepted by design, so this is not re-raised as a defect.
Flagged only so the residual stays visible: an appliance with a working local scanner
and a stale `scanner.host` reports amber "the scanner could not be checked" for ever,
and an appliance with *no* scanner at all reports amber rather than red whenever a
configured host is down, so `doctor` exits 0 for it. Both are behaviours a later reader
will find surprising without this note.

### IN-03: The scanner gate is now held across an unbounded `getaddrinfo`

**File:** `src/saneless/checks.py:1178-1183` with `checks.py:850-854`
WR-03 narrowed the gate to `_check_scanner`, which is right — but `_check_scanner`'s
first act is the saned pre-probe, whose name resolution is explicitly outside every
budget (`checks.py:80-86`). So `ScanWorker._scan_job`'s `with self._scanner_gate:` can
still park, with the job row already written `SCANNING`, for as long as a broken
resolver takes — and `POST /api/checks/refresh` can re-arm that every 2 s. The
exposure is far smaller than before the fix (the Paperless budget and the two
filesystem writes are out of the gate) and the pre-probe is genuinely SANE-adjacent
work, so this is a note rather than a defect. If it is ever worth closing, resolve the
host list *before* taking the gate and pass addresses in.

### IN-04: The attempt cap does not bound the poll when `/api/checks` returns a non-2xx

**File:** `src/saneless/web/templates/partials/checks.html:53-58`
The cap works by the *response body* omitting the trigger, and htmx does not swap on an
error response. If `GET /api/checks` starts returning 5xx (or 422, for a hand-crafted
`attempt` outside the bound), the existing body is never replaced, keeps its
`every 2s`, and polls for ever at the same attempt number — IN-07's failure mode
restored for a different cause. IN-07's motivating case (a dead refresher thread) is
fully closed, since that path still returns 200, so this is a narrow residual. If it is
worth closing, add `hx-on::response-error="this.removeAttribute('hx-trigger')"` or
switch the give-up ending to `HX-Reswap`/286 once the htmx-config meta can change.

### IN-05: `junit_family = "legacy"` is a repo-wide schema change made for two tests

**File:** `pyproject.toml:161-168`
Verified inert and non-masking: no `--junitxml` appears in `.github/workflows/ci.yml`,
`.github/workflows/release.yml` or any script, `filterwarnings = ["error"]` is
untouched, and `pytest --junitxml=… tests/test_checks_cache.py` runs clean under
pytest 9.0.2. The only effect is that *if* somebody later asks for JUnit XML they get
xunit1 rather than xunit2, which some CI consumers (and the schema most tooling
validates against) no longer prefer. A narrower alternative exists — record the two
measurements through the `record_testsuite_property` fixture, or attach them to the
test's own report — if the repo ever wants xunit2 back. Not a defect today; noted so
the coupling between two measurement tests and the whole repo's XML schema is
discoverable.

---

_Reviewed: 2026-09-17_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
