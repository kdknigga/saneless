---
phase: 30-appliance-layer
reviewed: 2026-09-16T00:00:00Z
depth: deep
files_reviewed: 28
files_reviewed_list:
  - src/saneless/checks.py
  - src/saneless/cli.py
  - src/saneless/config.py
  - src/saneless/job.py
  - src/saneless/paperless.py
  - src/saneless/pipeline.py
  - src/saneless/auto_profiles.py
  - src/saneless/vocabulary.py
  - src/saneless/worker.py
  - src/saneless/web/app.py
  - src/saneless/web/checks_cache.py
  - src/saneless/web/errors.py
  - src/saneless/web/refresher.py
  - src/saneless/web/routes.py
  - src/saneless/web/cross_origin.py
  - src/saneless/web/static/app.css
  - src/saneless/web/templates/index.html
  - src/saneless/web/templates/partials/checks.html
  - src/saneless/web/templates/partials/error.html
  - src/saneless/web/templates/partials/flip.html
  - src/saneless/web/templates/partials/history.html
  - src/saneless/web/templates/partials/profile_description.html
  - src/saneless/web/templates/partials/scan_button.html
  - src/saneless/web/templates/partials/status.html
  - src/saneless/web/templates/partials/status_response.html
  - src/saneless/web/templates/partials/tags.html
  - src/saneless/web/templates/partials/terminal_reload.html
  - docker-compose.yml
findings:
  critical: 2
  warning: 7
  info: 7
  total: 16
status: issues_found
---

# Phase 30: Code Review Report

**Reviewed:** 2026-09-16
**Depth:** deep
**Files Reviewed:** 28 source files (plus the 10 changed docs, skimmed for contract drift)
**Status:** issues_found

## Summary

The phase's security posture is genuinely good and the review could not break the
things it was pointed at hardest. The owner gate is enforced server-side in
`continue_flip` / `abort_flip` through `_owner_answers`, the token never reaches a
template context (only the derived `is_owner` bool does), it is never logged as a
value, and the comparison goes through `secrets.compare_digest` on a
`token_urlsafe(32)` mint. The non-owner rendering omits the buttons from the
markup rather than hiding them with CSS. `DeviceInfo.name` is never rendered —
`_device_label` reads only `vendor`/`model`. Jinja autoescape is on and nothing in
the changed templates is marked `|safe`. `render_error` carries only a status code
and a job id into the `<details>`, and every message is a vocabulary constant. The
placeholder-token refusal is present and unconditional on both surfaces. There are
zero suppressions in the diff; `ruff check` and `ruff format --check` pass clean.

What the review did find is in the *correctness* of the new check registry, not
its confidentiality. Two defects make the appliance report FAIL/WARN on a healthy
deployment, and one of them makes `saneless doctor` and the web strip disagree —
which is the exact property D-02 exists to guarantee. Both are reproducible from
the shipped code without a scanner.

A second cluster concerns the scanner gate: it is now held across work that never
touches SANE, and gate contention between two *checkers* is misreported to the
user as "a scan is running".

## Critical Issues

### CR-01: The web Profiles row lies on every config-file deployment, and contradicts `doctor`

**File:** `src/saneless/worker.py:411`, `src/saneless/worker.py:889`, `src/saneless/checks.py:795-802`, `src/saneless/cli.py:1155-1160`

**Issue:**
`ScanWorker.__init__` seeds `self._profile_storage = ProfileStorage.IN_MEMORY_NO_CONFIG_FILE`
(worker.py:411). `_generate_startup_profiles` returns at `if not bare: return`
(worker.py:889) *before* any branch that writes `_profile_storage`. So for the
normal production shape — an operator with a real `config.toml` holding profiles
that are not the bare default, which is exactly what `docker-compose.yml` mounts
at `./config` — the recorded outcome stays `IN_MEMORY_NO_CONFIG_FILE` for the life
of the process.

`_check_profiles` then renders that as a permanent amber row:

```
WARN | Generated in memory — no configuration file is in use, so they are lost on restart.
     | Create a saneless config file so the profiles are saved.
```

Every clause of that sentence is false: the profiles were not generated, a
configuration file *is* in use, and they will survive a restart. The next step
tells the operator to create a file they already have.

Meanwhile `doctor` derives the same fact differently (cli.py:1155-1160,
`PERSISTED if settings.config_path is not None`) and reports `[ OK ] Profiles  2
scan profiles configured.` for the identical appliance. D-02's contract — "both
surfaces report the *same* checks in the *same* words" — is broken by the one
field that is passed in rather than computed in `checks.py`.

Reproduced against the shipped code:

```
is_bare_default: False
WEB (worker default)         -> WARN  | Generated in memory — no configuration file is in use, …
CLI doctor (config_path set) -> OK    | 2 scan profiles configured.
```

No test covers the "startup generation skipped because the settings are not bare"
path; `tests/test_worker.py:5742` only asserts the *initial* value before any
attempt, which is why the gap survived.

**Fix:** the default is only correct for "an attempt has not happened yet". When
generation is skipped because the profiles did not come from generation, the
storage fact is about the loaded settings, not about a write that never ran:

```python
# worker.py, _generate_startup_profiles
if not bare:
    # Nothing was generated, so nothing was persisted -- but the profiles in
    # hand came from the loaded file (or from env), and the Profiles row must
    # not claim they are in-memory.  Mirror doctor's derivation (cli.py:1155).
    self._profile_storage = (
        ProfileStorage.PERSISTED
        if self._settings.config_path is not None
        else ProfileStorage.IN_MEMORY_NO_CONFIG_FILE
    )
    return
```

The same correction is owed to the `profiles is None` early return a few lines
below (a SANE failure during generation leaves the loaded profiles in place and
must not report them as in-memory either). Add a worker test for
`profile_storage` after a non-bare start with `config_path` set, and a
`checks.py` test asserting `doctor`'s and the worker's derivations agree for that
settings shape.

---

### CR-02: The saned pre-probe short-circuits `get_devices()`, producing a false FAIL and `doctor` exit 2 on a working scanner

**File:** `src/saneless/checks.py:634-638`

**Issue:**

```python
entries = _saned_hosts(context.settings.scanner.host)
if entries and not any(
    _saned_reachable(host, port, PROBE_CONNECT_SECONDS) for host, port in entries
):
    return _scanner_unreachable()
```

The guard treats "every configured sane-net host refuses TCP" as "there is no
scanner", and returns without ever calling `get_devices()`. That inference is
wrong, because `SANE_NET_HOSTS` **adds** net devices; it does not replace local
backend enumeration. `sane_backend.py:_ensure_initialised` only sets the env var
(`os.environ["SANE_NET_HOSTS"] = host`); the dll backend still loads every local
backend. So an appliance with a working USB/local scanner *and* a stale or
powered-down `scanner.host` entry now reports:

```
[FAIL] Scanner   Not reachable.
       Check the scanner is switched on and connected, then press Check again.
```

…and `saneless doctor` exits 2, on a machine that scans perfectly. A scripted
health gate goes red for a healthy appliance — the failure mode
`CheckState`'s own docstring says must not happen.

There is a second, independent divergence on the same lines. `_ensure_initialised`
honours a pre-existing `SANE_NET_HOSTS` in the environment over `scanner.host`
("explicit env var takes priority over config file"), but `_saned_hosts` reads
`context.settings.scanner.host` only. When both are set to different values the
probe dials the host SANE is *not* using, so a down config host produces FAIL
while the live env host is serving devices.

`tests/test_checks.py:682-760` covers the no-devices and raising-backend paths but
has no case where the probe fails and the backend still has a device.

**Fix:** the pre-probe is a latency optimisation, not a verdict. Let it degrade
the *cost* of `get_devices()`, never replace its answer — or gate it on the
backend actually being net-only:

```python
entries = _saned_hosts(context.settings.scanner.host)
probe_failed = bool(entries) and not any(
    _saned_reachable(host, port, PROBE_CONNECT_SECONDS) for host, port in entries
)
try:
    devices = scanner.get_devices()
except Exception as exc:
    logger.warning("Scanner enumeration failed: %s", type(exc).__name__)
    return _scanner_unreachable()
if devices:
    ...  # OK row -- a local device is a device, whatever the net host did
return _scanner_unreachable()
```

If the ~127 s `get_devices()` stall for an unplugged net host must stay bounded,
the bound belongs on the enumeration (run it on a worker thread with a deadline,
or skip *only* the net portion), not on a verdict that ignores local devices.
Either way, read `os.environ.get("SANE_NET_HOSTS", settings.scanner.host)` so the
probe dials what SANE dials.

## Warnings

### WR-01: `_saned_hosts` mis-parses IPv6 and bracketed addresses into bogus host/port pairs

**File:** `src/saneless/checks.py:451-459`

**Issue:** the parser splits unconditionally on `:`. Verified against the shipped
function:

```
'fe80::1'        -> (('fe80', 1),)
'[fe80::1]:6566' -> (('[fe80', 6566), ('1]', 6566), ('6566', 6566))
```

`fe80::1` produces two non-empty segments, the second is all digits and in range,
so it is read as a *port* — the probe dials host `"fe80"` on port 1. Every such
dial fails, and via CR-02 that is a `FAIL` row and `doctor` exit 2. A bracketed
literal fans out into three junk hostnames.

**Fix:** refuse to parse rather than guess. If the setting contains more than one
`:` and any segment is not a plausible hostname/IPv4 literal, return `()` so the
check falls back to `get_devices()` — the documented "a setting this module cannot
parse into an entry leaves the check behaving exactly as it did before the probe
existed" behaviour that the docstring promises but the code does not deliver:

```python
if host_setting.count(":") > 1 and not all(
    _looks_like_a_host_name(segment) for segment in present
):
    return ()
```

Add the two cases above to `tests/test_checks.py:310-366`.

---

### WR-02: The saned probe's documented 2-second bound is neither the wall-clock bound nor DNS-inclusive

**File:** `src/saneless/checks.py:80-85`, `src/saneless/checks.py:499-507`

**Issue:** `PROBE_CONNECT_SECONDS` is documented as "the whole of success criterion
2 on the SANE side". It is not, for two reasons visible in CPython's
`socket.create_connection`:

1. `getaddrinfo(host, port, 0, SOCK_STREAM)` runs *before* the loop and before any
   `sock.settimeout(timeout)`. Name resolution is completely unbounded. On a
   container with an unreachable resolver this blocks for
   `resolv.conf timeout × attempts × nameservers` — tens of seconds — inside the
   refresher thread, while it holds the scanner gate (see WR-03).
2. The timeout is applied *per resolved address*, inside the `for res in
   getaddrinfo(...)` loop. A dual-stack host that is down costs
   `2 s × len(addresses)`, not 2 s.

Combined with `any(...)` over N configured hosts, the real worst case is
`N × addresses × 2 s + N × DNS`, not 2 s.

**Fix:** either state the true bound in the constant's comment and in
`_saned_reachable`'s "Failure policy" paragraph, or bound it for real — resolve
once with a deadline and dial a single address:

```python
infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)  # still unbounded
af, socktype, proto, _canon, sa = infos[0]
with socket.socket(af, socktype, proto) as sock:
    sock.settimeout(timeout)
    sock.connect(sa)
```

A test that asserts the elapsed time for a multi-address unreachable host would
pin whichever answer is chosen.

---

### WR-03: The scanner gate is held across work that never touches SANE, stalling scan starts

**File:** `src/saneless/web/refresher.py:224-238`, `src/saneless/web/routes.py:1231-1245`

**Issue:** both probe sites acquire `worker.scanner_gate` and hold it for the
*entire* `run_checks(context)` call. Only `_check_scanner` touches SANE.
`_check_paperless` makes an HTTP request budgeted at `httpx.Timeout(5.0,
connect=2.0)` — up to ~7 s — and `_check_fallback` / `_check_data_dir` each create
and delete a real file. So the lock that exists to keep two callers out of libsane
is routinely held for ten seconds or more while nothing is inside libsane.

`ScanWorker._scan_job` starts with `with self._scanner_gate:` and *blocks*. The job
row has already been written `SCANNING` at that point, so the status area reads
"Scanning…" while the worker is parked behind a Paperless health probe. Combined
with WR-05 this is how a LAN client can stall scan starts indefinitely.

**Fix:** narrow the gate to the one check that needs it:

```python
# checks.py
def run_checks(context: CheckContext, *, scanner_gate: AbstractContextManager | None = None)
```

or, simpler and local to the two call sites, decide `skip_scanner` from the
non-blocking attempt, release immediately, and re-acquire only around
`_check_scanner`. At minimum, release the gate before the Paperless and
filesystem rows run.

---

### WR-04: Gate contention between two checkers is rendered as "Not checked while a scan is running"

**File:** `src/saneless/web/routes.py:1231-1233`, `src/saneless/web/refresher.py:224-228`

**Issue:** `skip_scanner=not acquired` treats *any* failure to take the gate as
"a scan is running". But the gate is also taken by the refresher tick and by a
concurrent `POST /api/checks/refresh`. Given WR-03's wide hold window, a Check
again click landing during a refresher tick fails the acquire and stores a
`_scanner_skipped()` row.

The response then renders both facts in one body: `_checks_context`
(`routes.py:288`) computes `scan_active = state.worker.current_job_id is not None`
→ `False`, so the strip reads

```
· Scanner   Not checked while a scan is running.
  Last checked 14:02.
```

— a self-contradicting strip, with the appliance idle. `docs/reference/web-api.md`
states this cannot happen ("During a scan, the checks that would touch the scanner
are skipped").

**Fix:** derive the skip from the fact it claims to report, and use the gate only
as the mutual-exclusion primitive:

```python
acquired = gate.acquire(blocking=False)
scan_running = state.worker.current_job_id is not None
context = replace(state.refresher.build_context(), skip_scanner=not acquired)
...
```
and either suppress the strip update entirely when `not acquired and not
scan_running` (another checker owns it; its own store is imminent), or single-flight
the probe so two checkers never contend at all.

---

### WR-05: `POST /api/checks/refresh` is an unauthenticated, unbounded probe amplifier that repeatedly takes the scanner gate

**File:** `src/saneless/web/routes.py:1205-1250`

**Issue:** the route deliberately bypasses the TTL and has no rate limit, no
debounce and no single-flight. `CrossOriginGuard` allows a request carrying
neither `Sec-Fetch-Site` nor `Origin` (documented), so `curl` in a loop works. Each
call issues a Paperless HTTP request, up to N saned TCP dials and two filesystem
writes, and takes the scanner gate for the duration (WR-03). `GET /api/checks`
carries the documented guarantee that "watching the page cannot generate scanner or
paperless-ngx traffic"; this POST is the hole in it, and the docs do not say so.

On a LAN with no authentication the marginal risk over `POST /api/scan` is small,
but the scan-starvation effect is new: the worker's `with self._scanner_gate:` is
not a fair lock, so a tight refresh loop can park a submitted job indefinitely
while its row reads `SCANNING`.

**Fix:** put a floor under the bypass — a minimum interval (say 2 s) between
honoured refreshes, enforced in `CheckCache`, with a too-soon click simply
re-rendering the current strip:

```python
if not state.checks.claim_manual_refresh(min_interval=2.0):
    return state.templates.TemplateResponse(request, "partials/checks.html",
                                            _checks_context(state))
```

That preserves D-09's "do not make somebody wait out a 30 s TTL" while removing
the amplifier. Note the limit in `docs/reference/web-api.md`.

---

### WR-06: A user can no longer submit a scan with no tags or no correspondent

**File:** `src/saneless/web/routes.py:1009-1011`

**Issue:**

```python
tags = tags or found.default_tags
if correspondent is None:
    correspondent = found.default_correspondent
```

D-29's stated intent is "with `show_tags` off, the submit carries nothing for that
field and the profile's own default is what applies". The implementation cannot
distinguish "the control was not rendered" from "the control was rendered and the
user cleared it" — both arrive as an empty list / `None`. With the default
`show_tags = true` and a profile carrying `default_tags`, a user who unticks every
box silently gets the profile's tags applied anyway, with no indication. Before
this phase the web path applied no profile defaults at all
(`git show 6867e79:src/saneless/web/routes.py` has no `default_tags` reference), so
this is a behaviour regression, not just an unimplementable intent.

**Fix:** gate the fallback on the setting that actually says whether the control
was on the page:

```python
if not state.settings.web.show_tags:
    tags = found.default_tags
if not state.settings.web.show_correspondent:
    correspondent = found.default_correspondent
```

Add a route test: `show_tags=True`, profile with `default_tags=[1]`, submit with no
`tags` field → the created job must carry `[]`.

---

### WR-07: Shutdown's worst case is two join bounds, not the one the comment claims

**File:** `src/saneless/web/app.py:255-263`

**Issue:** the comment asserts "Both stop events are set before either join begins,
so the two bounded joins overlap and the worst case stays `STOP_JOIN_SECONDS`
instead of doubling". The joins are sequential: `refresher.stop()` is called only
after `worker.stop()` returns, and its `self._thread.join(timeout=STOP_JOIN_SECONDS)`
starts its clock then. The early signal makes the claim true only when the
refresher winds down inside the window; in the pathological case the bound exists
for — a refresher parked inside an unbounded `getaddrinfo` (WR-02) or inside
`sane_get_devices` — the total is `2 × STOP_JOIN_SECONDS`.

**Fix:** either make the claim true by computing a shared deadline —

```python
deadline = time.monotonic() + STOP_JOIN_SECONDS
worker_stopped = worker.stop()
refresher_stopped = refresher.stop(timeout=max(0.0, deadline - time.monotonic()))
```

— or correct the comment and `CheckRefresher.request_stop`'s docstring
(`refresher.py:151-163`), which repeats the same claim, to say the bound is
per-thread.

## Info

### IN-01: The index page fetches Paperless metadata it will never render

**File:** `src/saneless/web/routes.py:733-740`
`_tag_list_context` and `_get_cached_or_fetch(..., "correspondents")` run
unconditionally, even when `settings.web.show_tags` / `show_correspondent` are
`False` and the markup for both is omitted. On a cold metadata cache that is two
Paperless round-trips per page load for data that is discarded. Guard both calls on
the same flags the template branches on.

### IN-02: `paperless_test` logs the exception object, the only ASVS V7 outlier left in the file

**File:** `src/saneless/web/routes.py:822`
`logger.warning("Paperless connection test failed: %s", exc)` — every other handler
in the file (including all of this phase's new code) logs `type(exc).__name__`
precisely because the Paperless URL may carry `user:pass@` (noted at
`checks.py:21`). The line predates the phase, but the phase reworked the module
around it and established the rule; it should be brought into line:
`logger.warning("Paperless connection test failed: %s", type(exc).__name__)`.

### IN-03: `_note_pass_count` interpolates a user-supplied title with `%s`, not `%r`

**File:** `src/saneless/pipeline.py:433-438`
`web/errors.py`'s module docstring states the discipline: request input goes into a
log line with `%r` "so a control character in them is escaped and cannot forge a
log line". A job title is request input, is bounded only in length
(`TITLE_MAX_LENGTH`), and can contain newlines. The new call follows the existing
`%s` style at `pipeline.py:1567` and `pipeline.py:2211`, so this is a consistency
question rather than a new class of defect — but the safe spelling is `%r` and the
phase is the right moment to convert all three.

### IN-04: `_profile_storage` is cross-thread state with no lock, unlike its sibling

**File:** `src/saneless/worker.py:411`, `src/saneless/worker.py:735`
`_front_pages` got a dedicated `_front_pages_lock` with a comment explaining why.
`_profile_storage` is written by the worker thread and read by request threads
(via the `profile_storage` property, through `build_check_context`) with no
synchronisation. A single enum rebind is atomic under the GIL so there is no torn
read today, but the asymmetry invites a later reader to assume a lock exists. Either
add one or state in the property docstring that it is a single startup-time rebind
and deliberately unlocked.

### IN-05: `PLACEHOLDER_TOKENS` cites a line the same phase deleted

**File:** `src/saneless/config.py:240`
`# Shipped today at docker-compose.yml:30 and docs/reference/docker.md:178.`
Neither location contains `changeme` any more — `docker-compose.yml`'s
`SANELESS_PAPERLESS__TOKEN` line was commented out in this phase's own diff, and
`docs/reference/docker.md:178` is now a scanner-host YAML block. The justification
for keeping `changeme` in the set is still sound (upgraders' compose files carry
it), but the citation should say so instead of pointing at lines that no longer
exist.

### IN-06: `local_time` can emit a trailing space into a generated document title

**File:** `src/saneless/vocabulary.py` (`local_time`), `src/saneless/config.py:438`
`LOCAL_TIME_FORMAT` ends in `%Z`, which `strftime` renders as the empty string when
the platform reports no zone abbreviation. `resolve_job_title` interpolates the
result directly (`f"Scan {local_time(now)}"`), so such a host produces a
paperless-ngx document title with a trailing space. `" ".join(...).strip()` or
`.rstrip()` on the formatted value removes the case.

### IN-07: The cold-start strip polls every 2 s forever if the cache is never filled

**File:** `src/saneless/web/templates/partials/checks.html:37`
`hx-trigger="load, every 2s"` is emitted whenever `checks is none`, and the only
thing that ends it is results landing in the cache. If the refresher thread dies —
or if `WATCH_WINDOW_SECONDS` / gate contention keeps every tick from storing — an
open tab polls `/api/checks` twice a second indefinitely. Each request is cheap
(one cache read), but the poll is unbounded by design. Consider capping it
(`hx-trigger="load, every 2s"` plus a server-side attempt counter, or a wider
interval after the first few tries).

---

_Reviewed: 2026-09-16_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
