---
phase: 30-appliance-layer
reviewed: 2026-09-17T00:00:00Z
depth: deep
round: 4
diff_base: 2b3cced6aaa2a274bfc1e1f8560ac775a65b090d
files_reviewed: 17
files_reviewed_list:
  - docs/reference/web-api.md
  - src/saneless/checks.py
  - src/saneless/cli.py
  - src/saneless/web/app.py
  - src/saneless/web/checks_cache.py
  - src/saneless/web/errors.py
  - src/saneless/web/refresher.py
  - src/saneless/web/routes.py
  - src/saneless/web/templates/partials/checks.html
  - tests/test_browser.py
  - tests/test_checks_cache.py
  - tests/test_checks.py
  - tests/test_doctor.py
  - tests/test_refresher.py
  - tests/test_web_checks.py
  - tests/test_web_errors.py
  - tests/test_web.py
findings:
  critical: 0
  warning: 3
  info: 3
  total: 6
status: issues_found
---

# Phase 30 (round 4): Code Review Report

**Reviewed:** 2026-09-17
**Depth:** deep (cross-file: `checks.py` → `refresher.py` → `checks_cache.py` → `routes.py` →
`partials/checks.html` / `partials/terminal_reload.html` / `partials/status_response.html` →
`errors.py` → vendored `static/vendor/htmx-2.0.8.min.js`; plus `cli.py` and `app.py` filter wiring)
**Files Reviewed:** 17
**Status:** issues_found

_No `<structural_findings>` block was supplied for this round, so there is no
fallow-substrate section; everything below is narrative._

## Summary

**All twelve round-3 findings are closed, and the two critical ones were
verified by mutation on this tree rather than taken from a SUMMARY.**

- **R3-CR-01** — reverted `set(maybe_port) <= _ASCII_DIGITS` to
  `maybe_port.isdigit()` in `_saned_hosts`; four tests failed
  (`TestUnicodeDigitPorts::test_a_superscript_two_port_does_not_raise`,
  `…test_a_circled_one_port_does_not_raise`,
  `…test_an_arabic_indic_port_is_never_read_as_a_number`, and the end-to-end
  `TestScannerCheck::test_a_unicode_digit_port_does_not_redden_the_whole_run`,
  which reproduced `WARNING Check SCANNER raised ValueError`). Restored
  byte-for-byte (md5 `3e3f36f…` before and after).
- **R3-CR-02** — deleted the `HX-Target != CHECKS_POLL_TARGET_ID` condition in
  `render_error` so the retarget applies unconditionally; 14 cases in
  `tests/test_web_errors.py::TestChecksPollTargetIsExemptFromTheRetarget`
  failed, and three of the four Chromium cases in
  `tests/test_browser.py::TestPollEndsOnAnErrorResponse` failed —
  `#checks-strip .status-error` never appeared, i.e. the strip was still on the
  page and still polling. Restored byte-for-byte (md5 `2bacf46…`). The browser
  class now drives the failure through the server with `route.continue_` and a
  tampered `attempt`, so round 2's `route.fulfill()` defect is genuinely gone.

Two further closures were mutation-checked as well: R3-WR-02 (moving `store`
back into an `else` arm kills three `test_refresher.py` cases; deleting the
`_run` backstop kills `test_a_raising_tick_does_not_end_the_refresher`) and
R3-WR-03 (deleting the four `skipped` branches in `check_row_class`,
`check_row_glyph`, `check_row_label` and `cli._row_marker` kills 13 cases
across `test_checks.py`, `test_doctor.py` and `test_web_checks.py`).

R3-WR-01's wide guard was checked empirically rather than by reading: over
245,410 generated segments drawn from the permitted charset, **zero** segments
that `_looks_like_a_host_name` accepts are read numerically by glibc
(`AI_NUMERICHOST`) without also being a legal dotted-quad literal. The
shorthand class is closed. What is *not* closed is the literal `0.0.0.0`, which
the guard's own docstring names as "the worst of them" — see R4-WR-03.

Working tree left clean (`git status --short` empty), full suite green
(3051 passed, 124 browser deselected), `ruff check`, `ruff format --check`,
`ty check` and `pyrefly check src tests` all clean.

**What this round found.** Nothing that stops the phase, and no security
defect: every string on the strip is still developer-authored, the error slot
still carries only a status code and a job id, the poll is still bounded on
both caps, and no request input reaches a body or a log line. Three warnings,
all of the round-3 *class* — a fix whose blast radius is wider than the
element it was written for, and two sentences (one in shipped docs, one in a
docstring) that assert a property the code at HEAD does not have.

R3-CR-02's exemption is keyed on the `HX-Target` request header alone, and the
strip's own `Check again` button sends that same header, so an error on
`POST /api/checks/refresh` — a route that, unlike its sibling `GET`, has no
`_checks_fallback_context` guard — now deletes the strip instead of landing in
the message slot. `docs/reference/web-api.md:136` says a counter the server
cannot read as an in-range number comes back "with no poll attached … and
nothing asks again"; a below-range counter does the opposite, and a test pins
it doing the opposite. And `_looks_like_a_host_name` builds its case on
`0.0.0.0` reaching loopback while accepting `0.0.0.0` into the dial list.

---

## Critical Issues

None. The two round-3 criticals are closed and verified by mutation, and
nothing new in this diff reaches that bar.

---

## Warnings

### R4-WR-01: R3-CR-02's exemption is not scoped to the poll, so a failing `Check again` deletes the strip — and `refresh_checks` has no fallback

**Files:** `src/saneless/web/errors.py:207-226` (`render_error`),
`src/saneless/web/templates/partials/checks.html:103-104` (the button),
`src/saneless/web/routes.py:1446-1537` (`refresh_checks`),
`src/saneless/web/routes.py:1431-1443` (`get_checks`, for the contrast)

**Issue:** The exemption is `request.headers.get("HX-Target") != CHECKS_POLL_TARGET_ID`.
It is documented as the *polling strip's* exemption — `errors.py:70-81` argues
it entirely in terms of an armed htmx poll and `ct()`/`se(e)` — but the header
is not unique to the poll. The `Check again` button in the same template is

```html
<button type="button" class="check-refresh secondary"
        hx-post="/api/checks/refresh" hx-target="#checks-body" hx-swap="outerHTML">
```

so its POST arrives with `HX-Target: checks-body` too, and is exempted
identically. Measured on this tree with `_checks_context` made to raise:

```
REFRESH STATUS 500
REFRESH HEADERS {}                                    # HX-Target: checks-body
OTHER TARGET HEADERS {'hx-retarget': '#status-message',
                      'hx-reswap': 'innerHTML'}       # HX-Target: status-area
```

With no retarget, the button's own `hx-swap="outerHTML"` writes
`partials/error.html` over `#checks-body`. That partial carries no id, and the
existing browser test asserts exactly this outcome for the poll
(`expect(page.locator("#checks-body")).to_have_count(0)`), so the consequence is
not inferred. For the *poll* that is the fix. For the *button* it is a pure
regression: before R3-CR-02 the same 500 landed in `#status-message` and left
five rows and the button on the page; now one click removes the strip, its
five rows and the only affordance that could bring them back, for the life of
the tab.

Two things sharpen it:

- `get_checks` treats a raise from `_checks_context` as "render
  `_checks_fallback_context` at 200" and `TestTheStripSurvivesItsOwnFailure`
  parametrises both halves of its guarded region. `refresh_checks` calls the
  *same* `_checks_context` with no guard at all, and nothing in
  `tests/test_web_checks.py` or `tests/test_web_errors.py` exercises a failing
  refresh. The route that can destroy the strip is the unguarded one.
- On the collapse branch the claim has already been released, but on the
  probe-succeeded branch the manual-refresh claim is spent *and* the clicker
  gets a strip-deleting 500, which is the "nothing, twice in a row" symptom
  WR-03 was raised to close, in a new spelling.

**Fix:** scope the exemption to the request that actually polls, and give the
POST the guard its sibling has.

```python
# errors.py -- the strip's *poll* is the exemption, not everything aimed at it.
_CHECKS_POLL_PATH: Final = "/api/checks"

...
    if request.headers.get("HX-Request") == "true":
        polling_strip = (
            request.headers.get("HX-Target") == CHECKS_POLL_TARGET_ID
            and request.method == "GET"
            and request.url.path == _CHECKS_POLL_PATH
        )
        if not polling_strip:
            headers["HX-Retarget"] = "#status-message"
            headers["HX-Reswap"] = "innerHTML"
```

```python
# routes.py -- refresh_checks gets the same fallback get_checks has.
    try:
        context = _checks_context(state)
    except Exception:
        logger.exception("Failed to render the status strip")
        context = _checks_fallback_context()
    return state.templates.TemplateResponse(
        request, "partials/checks.html", context
    )
```

Both need a test: one asserting `POST /api/checks/refresh` with
`HX-Target: checks-body` still carries `HX-Retarget: #status-message`, and one
asserting a raising `_checks_context` on that route is a 200 strip rather than
a 500.

### R4-WR-02: `docs/reference/web-api.md` states that an out-of-range counter stops the poll; a below-range counter restarts it, and a test pins that

**Files:** `docs/reference/web-api.md:136`,
`src/saneless/web/routes.py:1432` (`counted = min(max(attempt, 0), POLL_PROBE_ATTEMPT_CAP)`),
`src/saneless/web/templates/partials/checks.html:80-85`,
`tests/test_web_checks.py` (`test_a_negative_attempt_is_the_start_of_a_chain`)

**Issue:** The shipped sentence is

> "An attempt counter the server cannot read as an in-range number, and any
> failure inside the strip's own rendering, come back as the strip itself with
> no poll attached: the five rows and the `Check again` button are still on the
> page, and **nothing asks again**."

That is true of the above-range half and false of the below-range half. `-5` is
clamped to `0`, which is the number a page render uses, so `_checks_context`
returns `poll_attempt == 1`, and `poll_attempt == 1` is precisely the value the
template uses to emit the `load` trigger:

```html
hx-trigger="{% if poll_attempt == 1 %}load, {% endif %}every 2s"
```

So a below-range counter comes back **with** a poll attached, asks again
immediately on `load`, and starts a fresh chain with a fresh count. The
route's own docstring is honest about this ("a value below zero is read as the
start of a fresh chain"), and `test_a_negative_attempt_is_the_start_of_a_chain`
asserts `"/api/checks?attempt=1" in …`, i.e. the suite pins the behaviour the
documentation denies. Two sentences about the same input, in the same tree,
disagreeing — which is the failure mode round 3 named as its central lesson,
and the reason the doc half of R3-CR-02 was called worse than the open finding.

The practical exposure is small: the counter is only ever minted by the server,
so nothing a browser does produces a negative one, and a crafted one resets a
bound that only ever protected the crafter's own tab. It is the *claim* that is
the defect, because the next round is told to read this sentence rather than
re-derive the clamp.

**Fix:** say what the clamp does, in the two directions it does it.

```markdown
A counter above the range is read as the end of the chain: it clamps to the
larger cap, which is the give-up body, so the strip comes back with no poll
attached. A counter below the range -- which nothing on the page ever sends --
is read as zero, the number a page render starts at, so it comes back as the
first body of a fresh chain. Any failure inside the strip's own rendering comes
back as the cold-start body with no poll attached: five named rows, the
`Check again` button, and the line saying the checks have not run yet.
```

### R4-WR-03: the numeric-address guard's own docstring names `0.0.0.0` as the worst case, and the code dials it

**Files:** `src/saneless/checks.py:762-819` (`_looks_like_a_host_name`),
`src/saneless/checks.py:715-759` (`_segment_is_a_numeric_address_shorthand`),
`src/saneless/checks.py:999-1001` (the filtered return)

**Issue:** `_segment_is_a_numeric_address_shorthand` answers True only for a
segment that is numeric to glibc *and* is not a legal dotted quad. `0.0.0.0` is
a legal dotted quad, so it answers False, and `_looks_like_a_host_name` accepts
it. Measured on this tree:

```
_looks_like_a_host_name("0.0.0.0")   -> True
_saned_hosts("0.0.0.0")              -> (('0.0.0.0', 6566),)
_saned_hosts("0.0.0.0:6566")         -> (('0.0.0.0', 6566),)
_saned_hosts("scanbox:0.0.0.0")      -> (('scanbox', 6566), ('0.0.0.0', 6566))
```

The last line is the one that matters: a stray character in a port turns a
one-host setting into a two-entry dial list whose second entry is the
unspecified address. The same docstring that permits this states the hazard
outright:

> "`0.0.0.0` is the worst of them: on Linux a `connect()` to it reaches
> loopback, so such a segment in the dial list lets the probe report the
> configured scanner host 'reachable' off any unrelated local process listening
> on 6566 (R2-WR-01, T-30-28-01, T-30-31-02)."

And `_scanner_preflight` is an `any(...)`, so one spurious "reachable" is enough:
`_scanner_host_unanswered` is suppressed, the check falls through to
`_scanner_enumeration`, and the appliance pays the ~127 s uninterruptible
`get_devices()` this module documents — the exact cost the pre-probe exists to
avoid, on a host that is in fact dead. `SANE_NET_HOSTS=0.0.0.0` reaches it from
the environment as well as from the config file.

The wide guard is otherwise sound; I could not break it. The residual is one
value, it is the value the docstring picked out, and it is pinned by nothing:
`TestNumericAddressShorthand` parametrises `0.0`, `0x0.0`, `0x7f.1`, `127.1`,
`6566.0`, `0xdeadbeef` and `01.02.03.04`, and `0.0.0.0` appears in no test.

**Fix:** either exclude the unspecified address by name — it is the one dotted
quad that cannot be a scanner — or, if accepting whatever libsane would dial is
the deliberate reading, say so where the docstring currently says the opposite.
The first is two lines and a test:

```python
def _segment_is_a_numeric_address_shorthand(segment: str) -> bool:
    if not all(_part_is_a_number_to_glibc(part) for part in segment.split(".")):
        return False
    try:
        address = ipaddress.IPv4Address(segment)
    except ValueError:
        return True
    # The one legal literal that is still the hazard this function documents:
    # on Linux a connect() to the unspecified address reaches loopback, so it
    # would report the configured host reachable off any local listener.  It
    # names no scanner, so nothing is lost by refusing it (R4-WR-03).
    return address.is_unspecified
```

with `assert _saned_hosts("0.0.0.0") == ()` and
`assert _saned_hosts("scanbox:0.0.0.0") == (("scanbox", SANED_PORT),)` beside
the existing parametrised cases.

---

## Info

### R4-IN-01: once an error body replaces the strip, nothing on the page can restore it, and two other swaps silently miss

**Files:** `src/saneless/web/templates/partials/terminal_reload.html:15`,
`src/saneless/web/templates/partials/status_response.html:14`,
`src/saneless/web/templates/partials/checks.html:79`,
`docs/reference/web-api.md:136`

`partials/error.html` carries no id, so after the exempt swap `#checks-body` is
gone from the DOM — which the browser test asserts. Two other mechanisms
address that id and therefore become no-ops for the rest of the tab's life:
`terminal_reload.html`'s `hx-get="/api/checks" hx-target="#checks-body"`, fired
on every terminal job state, and the scan submit's out-of-band strip
(`status_response.html` includes `checks.html` with `oob = true`, whose
`hx-swap-oob="true"` needs a matching id). Neither failure is visible to the
user; htmx logs and moves on. The docs say the failure response "replaces the
strip, and the poll ends with it" but do not say the strip does not come back,
or that a subsequent scan's D-08 paused note will have nowhere to land. **Fix:**
one sentence in the docs, and — if the strip is meant to be recoverable — give
`partials/error.html` the option of carrying the target id when it is rendered
for this one target, so the next `terminal_reload` can swap over it.

### R4-IN-02: `_checks_fallback_context`'s give-up line is justified with a claim that is false whenever the cache is warm

**File:** `src/saneless/web/routes.py:423-453`

The docstring says `freshness_line` is `POLL_GAVE_UP_LINE`, "which is true here
for the same reason it is true at the cap -- nothing has been checked". That
holds only when the failure was the cache read. The guard in `get_checks`
covers `note_watcher()` and the whole of `_checks_context`, so a raise from
`state.worker.current_job_id`, from `state.refresher.probe_in_flight` or from
`local_time` produces this body on an appliance whose cache holds five true
rows — and the body then shows five `Checking…` placeholders and asserts the
checks have never run. The choice of body is defensible (nothing can be
trusted); the *justification* is not, and it is the kind of sentence round 3
was created to catch. **Fix:** replace the claim with the real reason — "the
render that would have read the cache is the one that failed, so this body
asserts nothing about it beyond the fact that it could not be shown" — or fall
back to the last known-good snapshot when the cache read itself succeeded.

### R4-IN-03: three Jinja filters are registered with no template consumer

**File:** `src/saneless/web/app.py:96-98`

`check_state_class`, `check_state_glyph` and `check_state_label` are registered
on `templates.env.filters`; since R3-WR-03's fix, `grep -rn` over
`src/saneless/web/templates` finds no use of any of the three — `checks.html`
uses only `check_row_class`, `check_row_glyph` and `check_row_label`, and
`_CheckingRow` carries its constants directly. The comment beside them argues
they stay "because they are what these three delegate to", but that delegation
is in Python and needs no filter registration. Three names in the template
namespace that no template may correctly use is a small trap: the next author
of a check row has two plausible filters to pick from and only one is right.
**Fix:** drop the three registrations (the Python functions stay, and
`tests/test_checks.py` and `tests/test_browser.py` import them directly), or
add a line saying they are retained as a deprecated alias and are not to be
used in new markup.

---

## Round-3 closure status

| id | verdict | how it was checked |
|---|---|---|
| R3-CR-01 | **closed** | mutation: `isdigit()` restored → 4 tests fail, incl. the end-to-end red-row case |
| R3-CR-02 | **closed** | mutation: exemption removed → 14 `test_web_errors` cases and 3 Chromium cases fail; browser class now drives the failure server-side |
| R3-WR-01 | **closed for its class** | 245,410-candidate sweep: no accepted segment is glibc-numeric without being a legal literal. Literal `0.0.0.0` residual → R4-WR-03 |
| R3-WR-02 | **closed** | mutation ×2: `store` back into `else` → 3 fails; `_run` backstop removed → 1 fail |
| R3-WR-03 | **closed** | mutation: all four `skipped` branches removed → 13 fails across checks/doctor/web |
| R3-WR-04 | **closed** | `POLL_PROBE_ATTEMPT_CAP`/`POLL_STILL_CHECKING_LINE` implemented; chain walked link-by-link to `cap + 1` with the lock held |
| R3-IN-01 | **closed** | `test_a_gated_run_returns_what_an_ungated_run_returns` parametrised over all four contexts |
| R3-IN-02 | **closed** | scripted `_SpendingClock`; sum-of-timeouts and strictly-decreasing assertions |
| R3-IN-03 | **closed** | compare-and-clear; `test_a_stale_stamp_does_not_clear_another_callers_claim` is a real mutation detector |
| R3-IN-04 | **closed** | docstring corrected; all three behaviours pinned (`localhost:6566:`, `host:065`, `scanner.local.`) |
| R3-IN-05 | **closed** | `_MAX_PROBE_HOSTS = 4`, filter-then-cap, pinned by a 40-segment case |
| R3-IN-06 | **closed** | the sample-versus-acquire race is stated in `web-api.md` |

---

_Reviewed: 2026-09-17_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
_Round: 4 (diff base 2b3cced)_
