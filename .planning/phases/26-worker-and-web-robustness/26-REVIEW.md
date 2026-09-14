---
phase: 26-worker-and-web-robustness
reviewed: 2026-09-14T22:15:14Z
depth: deep
files_reviewed: 42
files_reviewed_list:
  - src/saneless/worker.py
  - src/saneless/web/app.py
  - src/saneless/web/routes.py
  - src/saneless/web/errors.py
  - src/saneless/web/cross_origin.py
  - src/saneless/web/cache.py
  - src/saneless/vocabulary.py
  - src/saneless/job.py
  - src/saneless/config.py
  - src/saneless/auto_profiles.py
  - src/saneless/cli.py
  - src/saneless/web/templates/base.html
  - src/saneless/web/templates/index.html
  - src/saneless/web/templates/partials/error.html
  - src/saneless/web/templates/partials/flip.html
  - src/saneless/web/templates/partials/scan_button.html
  - src/saneless/web/templates/partials/status_response.html
  - src/saneless/web/static/app.css
  - .github/workflows/ci.yml
  - .pre-commit-config.yaml
  - .gitattributes
  - tests/conftest.py
  - tests/test_app_lifespan.py
  - tests/test_auto_profiles.py
  - tests/test_browser.py
  - tests/test_cache.py
  - tests/test_cli.py
  - tests/test_config.py
  - tests/test_cross_origin.py
  - tests/test_job.py
  - tests/test_outcomes_e2e.py
  - tests/test_vendor_assets.py
  - tests/test_vocabulary.py
  - tests/test_web_errors.py
  - tests/test_web.py
  - tests/test_web_state_rendering.py
  - tests/test_worker.py
  - docs/reference/web-api.md
  - docs/explanation/architecture.md
  - docs/how-to/deploy-docker-compose.md
  - docs/reference/docker.md
findings:
  critical: 1
  warning: 9
  info: 5
  total: 15
status: issues_found
---

# Phase 26: Code Review Report

**Reviewed:** 2026-09-14T22:15:14Z
**Depth:** deep
**Files Reviewed:** 42
**Status:** issues_found

## Narrative Findings (AI reviewer)

## Summary

I traced the worker loop, stop, degraded and recovery code across the job store, the routes, the status lookup and the Scan button template. I also traced the error-rendering and middleware path, and checked the lifespan order against D-01..D-23. Decisions made on purpose (D-05 ERROR rows, D-09 leaving the store open, D-20/D-21 header rules, D-03 message-slot lifetime) are not flagged.

The happy paths and the documented failure paths hold up. The main defect is in the reconciliation added for "research Pitfall 6". A failure that the loop guard could not write is retried only while the worker is **degraded**. Below the degraded threshold, the job row stays active forever. The status area then reports that row as the current job, and the server-owned Scan button stays disabled until restart, while `/health` reports 200. A scratch script reproduced this (row `SCANNING`, health `HEALTHY`, `_unrecorded_failures` non-empty, `latest_run_job().is_active == True`). An existing test asserts the stranded row and claims the probe reconciles it, but the probe never runs when the worker is not degraded.

Other confirmed defects:
- A rejected-submit row can be stranded in the same way.
- A successfully uploaded document can be recorded as ERROR.
- Startup auto-profiles make the in-memory `default` differ from the file (reproduced: `ADF Front` this run, `Flatbed` after restart).
- A cache-invalidate race returns stale data.
- 405 responses lose their `Allow` header (reproduced).
- The cross-site docs overstate what the Origin check protects against: DNS rebinding gets through (reproduced: `Origin == Host` for a foreign hostname returns 200).

## Critical Issues

### CR-01: A failure the loop guard could not record is never retried unless the worker is degraded, so the UI stays stuck on a phantom active job

**File:** `src/saneless/worker.py:743-755`, `src/saneless/worker.py:765-766`, `src/saneless/worker.py:799-806` (consumers: `src/saneless/web/routes.py:116-121`, `src/saneless/web/templates/partials/scan_button.html:9`, `src/saneless/web/templates/partials/status.html:2-6`)

**Issue:**
`_best_effort_fail` stores a failed write in `_unrecorded_failures`. The only code that drains it is `_try_recover`, and `_idle_housekeeping` calls that only when `self._degraded.is_set()`. One loop failure only counts 1 towards `_DEGRADED_AFTER = 3`, and the next successful job resets the count. So a single transient store error that hits both the terminal write and the retry (for example `database is locked` or a brief disk-full) leaves the row in `PENDING`/`SCANNING`/`UPLOADING` until the process restarts.

The effect in the UI:
- If that row is the newest non-REJECTED row, `_current_or_recent_job` returns it once `current_job_id` clears.
- `status.html` polls it every second, forever.
- `scan_button.html` renders `disabled` because `job.is_active`, so the operator cannot start the scan that would push the stuck row out of the status area.
- `/health` stays 200, so nothing tells an orchestrator or operator about it.

This breaks ROBU-01's goal and D-12's "recovery needs no user scan".

Reproduced with a scratch script: `finish_job` raises twice, then the store heals. After 1 s: row `SCANNING`, `health=HEALTHY`, `_unrecorded_failures={id: (...)}`, `latest_run_job().is_active=True`.

`tests/test_worker.py:2208-2209` (`test_a_failed_best_effort_write_is_only_logged`) asserts `stranded.is_active` with the comment "only the recovery probe (D-12) reconciles this row". `test_probe_is_not_called_while_not_degraded` proves the probe never runs in that state. The test locks the bug in, and it passes only because a second, newer job hides the stranded row.

**Fix:** Retry unrecorded writes on every idle tick while any are owed, whether or not the worker is degraded. Keep the probe and the degraded clear exclusive to the degraded state:
```python
def _idle_housekeeping(self) -> None:
    if self._degraded.is_set():
        self._try_recover()
    elif self._unrecorded_failures:
        self._flush_unrecorded_failures()  # same loop as _try_recover's first block; swallow + log.debug on raise
    ...
```
Then change the test to assert that the stranded row reaches `ERROR` once the fault heals, with a single job so no newer row can hide it. Also add a web-level test: after the heal, the status poll re-enables `#scan-btn`.

## Warnings

### WR-01: A rejected-submit row is written in two steps and can be stranded as PENDING, which also sticks the UI

**File:** `src/saneless/web/routes.py:301-320`, `src/saneless/web/routes.py:375-404`

**Issue:** `_record_rejected_submit` runs `create_job` (PENDING) and then `finish_job(ERROR, REJECTED)` as two transactions. In the post-submit path (`job_id=job.id`), `create_job` has already committed at line 375. If `finish_job` raises, the exception is swallowed (`return False`) and the row stays `PENDING` with no category. The worker never saw the id, so nothing reconciles it: it is not in `_unrecorded_failures`, and `fail_active_jobs` runs only when restart recovery is pending. `latest_run_job` does not filter it out (category is NULL), so CR-01's symptom follows: a permanent "Starting scan..." and a disabled Scan button. The `job_id=None` path has the same gap between its two statements. Even without a failure, a status poll that lands between the two statements briefly renders the refused attempt as a live PENDING job.

**Fix:** For `job_id=None`, add a single-statement `JobStore.create_rejected_job(profile, title, tags, correspondent, error)` that inserts the row directly as `ERROR`/`REJECTED`. For the post-submit path, pass a `finish_job` failure to the worker so the idle tick from CR-01 retries it, for example `worker.owe_rejection(job.id, error)`, which adds to `_unrecorded_failures` under a lock. Do not just log it.

### WR-02: A failed success-path write records an ERROR for a document Paperless already accepted

**File:** `src/saneless/worker.py:956-966`, `src/saneless/worker.py:688-693`, `src/saneless/worker.py:743-747`

**Issue:** The terminal `finish_job(DONE/FALLBACK, result=...)` runs after `run_pipeline` has returned, so the upload has already happened. If that write raises, the exception reaches `_run`, and `_best_effort_fail` writes `JobState.ERROR` with `str(exc)` (for example `disk I/O error`) and `ErrorCategory.UNKNOWN`. `_unrecorded_failures` keeps that ERROR text for later retries too. The operator sees "Error: disk I/O error" for a scan that reached Paperless and will likely re-scan, which creates a duplicate document. The raw sqlite message also appears in the status area and in history.

**Fix:** Keep the outcome the loop was trying to write. Catch the terminal-success write in `_scan_job`, then retry that same write (state and `JobResult`) through the owed-writes mechanism instead of turning it into an ERROR:
```python
try:
    self._job_store.finish_job(job.id, state, result=job_result)
except Exception:
    self._owed_writes[job.id] = (state, job_result, None, None)
    raise
```
and have `_best_effort_fail` / the flush replay the owed tuple.

### WR-03: Startup generation replaces `default` in memory, but the file keeps the old one, so `default` changes after a restart

**File:** `src/saneless/worker.py:571-579`, `src/saneless/auto_profiles.py:530-532`

**Issue:** `is_bare_default` returns True for a config file that spells out `[profiles.default]` with default values (its docstring says so). Generation then **replaces** the in-memory set, including a generated `default`. `write_profiles_to_config(..., force=False)` skips `default` because the key already exists, and writes only the other profiles. After a restart the file holds the old `default` plus the generated profiles, so the set is no longer bare and nothing is regenerated.

Reproduced: this run's `default` is `source='ADF Front', mode='Color', auto_generated=True`; after reload it is `source='Flatbed', mode='color', auto_generated=False`. On an ADF-only scanner, scans with `default` work until the first restart and then fail. The UI dropdown shows the same name both times.

**Fix:** Make memory match what was persisted. Either merge in memory only the names `write_profiles_to_config` actually wrote, or keep the loaded `default` when the file already defines one:
```python
written = self._persist_generated_profiles(profiles)  # return the written names, or all names when not persisted
with self._profiles_lock:
    if is_bare_default(self._settings):
        merged = dict(self._settings.profiles) if config_path_has_default else {}
        merged.update({n: profiles[n] for n in written_or_all})
        self._settings.profiles = merged
```

### WR-04: Any persist exception outside `(OSError, ConfigError)` throws away the generated profiles, contrary to D-18

**File:** `src/saneless/worker.py:568-579`, `src/saneless/worker.py:631-644`, `src/saneless/auto_profiles.py:494-495`

**Issue:** `_generate_startup_profiles` persists **before** it swaps the profiles into memory. `write_profiles_to_config` can raise other exceptions: `tomlkit.exceptions.ParseError` (a `ValueError`) when the file changed since load or tomlkit rejects what tomllib accepted, `UnicodeDecodeError`, or tomlkit container errors. `_persist_generated_profiles` does not catch these, so the `_run` backstop logs "startup generation failed" and the swap never runs. D-18 requires that profiles which cannot be written are still used in memory.

**Fix:** Swap in memory first, then persist. Or catch `Exception` in `_persist_generated_profiles`: the method only logs, so a broad catch is correct here.

### WR-05: Single-flight re-check lets `invalidate` return stale data when a fetch was already in flight

**File:** `src/saneless/web/cache.py:94-103`, `src/saneless/web/cache.py:105-113`, `src/saneless/web/routes.py:478-482`

**Issue:** `invalidate` pops the entry without taking the key lock. Take a fetch F1 that started before the operator created a tag in Paperless. If the refresh button's `invalidate` runs while F1 is in flight:
1. F1 finishes and `set()`s the pre-change list.
2. The refresh request's `get_or_fetch` waits on the key lock.
3. It re-checks, finds F1's fresh-by-timestamp entry, and returns it.

The refresh button therefore returns exactly the stale data it exists to bypass, and caches it for another TTL. The single-flight re-check added in this phase introduced the race.

**Fix:** Use a per-key generation counter. `invalidate` bumps the counter, and `set` from a fetch stores its result only if the generation it started under is still current:
```python
def invalidate(self, key: str) -> None:
    with self._locks_guard:
        self._generation[key] = self._generation.get(key, 0) + 1
        self._store.pop(key, None)
```

### WR-06: Every pipeline failure during shutdown is recorded as "server restarted", even ones shutdown did not cause

**File:** `src/saneless/worker.py:712-714`, `src/saneless/worker.py:927-946`

**Issue:** `_failure_record` returns `RESTART_REASON` with no category whenever `_stopping` is set, whatever `exc` was. A genuine `PaperlessError`, a jam, or a flip Abort the operator clicked just before shutdown, if raised inside the 5 s join window, is written as "The server restarted before this scan finished" with `error_category=NULL`. The real cause and category are lost from history. The in-code rationale covers only stop()'s own Abort, and D-15's rule is to never guess a cause.

**Fix:** Use the restart reason only when stop() itself claimed the flip answer. For example, record `claimed = coordinator.signal_abort()` in `stop()` as `self._shutdown_aborted_job = coordinator.job_id` when it returns True, and check `job.id == self._shutdown_aborted_job` in `_failure_record`. Otherwise classify normally.

### WR-07: The error handlers write raw request paths to the log, contradicting the module's own guarantee

**File:** `src/saneless/web/errors.py:198-203`, `src/saneless/web/errors.py:220-225`

**Issue:** The module docstring says "No request input ... reaches a response body or a log line from here". `_validation_error` and `_unhandled_exception` both log `request.url.path` with `%s`. Starlette's `^...$` route regex matches a path with a trailing decoded newline, and `/static/{path}` accepts arbitrary decoded content. An attacker-chosen path can therefore forge a log line. `cross_origin.py:149-160` handles the same risk with `%r` and has a test for it (`test_rejection_log_escapes_control_characters_in_the_path`), so the two modules are inconsistent.

**Fix:** Use `%r` for `request.url.path` in both calls (and the method, for symmetry). Add the same control-character test for the 422 and 500 paths, or correct the docstring.

### WR-08: 405 responses lose their required `Allow` header

**File:** `src/saneless/web/errors.py:177-181`, `src/saneless/web/errors.py:145-163`

**Issue:** Starlette raises `HTTPException(405, headers={"Allow": ...})`. `_http_exception` passes only the status to `render_error`, which builds a fresh `headers` dict, so `exc.headers` is dropped. Reproduced: `GET /api/scan` returns `405` with only `content-length` and `content-type`. RFC 9110 §15.5.6 requires `Allow` on a 405. The same applies to any future `HTTPException` that carries headers, such as `WWW-Authenticate`.

**Fix:**
```python
def render_error(request, rejection, *, status_code, refresh_history=False, extra_headers=None):
    headers: dict[str, str] = dict(extra_headers or {})
    ...
# in _http_exception:
return render_error(request, rejection_for_status(exc.status_code),
                    status_code=exc.status_code, extra_headers=exc.headers)
```

### WR-09: The cross-site docs promise more than the Origin/Host check delivers: DNS rebinding gets through

**File:** `docs/reference/web-api.md` (Cross-site requests: "a web page on another site cannot start a scan or answer a flip prompt in your browser"), `src/saneless/web/cross_origin.py:58-79`

**Issue:** Branch 2 accepts any request whose `Origin` host equals its `Host` header. An attacker page at `http://rebind.attacker.example:8080` whose DNS then rebinds to the saneless LAN IP sends `Origin: http://rebind.attacker.example:8080` and `Host: rebind.attacker.example:8080`, and the request passes. Reproduced with TestClient: 200. saneless has no authentication and binds `0.0.0.0` by default, so a hostile page can start scans and read history. Go's `CrossOriginProtection` has the same limit, and D-21 rejected a trusted-origins setting, so this is not a code demand. The documentation's guarantee is still false as written.

**Fix:** Add a sentence to the Cross-site requests section and the reverse-proxy how-to. The guard does not stop DNS-rebinding attacks. Operators who need that should put saneless behind a proxy that answers only its own hostname, or bind to a specific interface. Record a Host allow-list as a deferred idea.

## Info

### IN-01: `_set_profiles` is used only by tests, and the stress tests exercise it instead of the production swap

**File:** `src/saneless/worker.py:524-539`, `src/saneless/worker.py:572-579`
**Issue:** Production swaps profiles inline at line 579. `_set_profiles` is called only from `tests/test_worker.py:2581-2667` and `tests/test_web.py:445`, so the D-19 lock stress test proves a method production never calls.
**Fix:** Route the swap in `_generate_startup_profiles` through one locked helper that takes a predicate (re-check bare, then rebind), and test that helper. Otherwise delete `_set_profiles` and have the tests drive `_generate_startup_profiles`.

### IN-02: `error_message(REJECTED)` advice is wrong for down and degraded rejections

**File:** `src/saneless/vocabulary.py:473-477`
**Issue:** "Wait for the current scan to finish, then try again" fits only QUEUE_FULL. REJECTED also covers worker-down and degraded rows, where no scan is running. The function has no production caller today, but whoever wires it up later gets the wrong text.
**Fix:** Word it neutrally ("This scan was not started. See the job's error for why."), or derive the text from the row's `error`.

### IN-03: Tests that assert nothing happened can pass without the worker ever running

**File:** `tests/test_worker.py:2111-2131`, `tests/test_worker.py:2487-2506`
**Issue:** `time.sleep(10 * _FAST_TICK)` followed by `probes.calls == []` / `spy.calls == []` still passes if the thread has not reached its first `queue.get` on a loaded runner. The window proves nothing unless ticks actually happened.
**Fix:** Count idle ticks, for example by spying on `_idle_housekeeping` or wrapping `queue.get`. Wait until at least N ticks have run, then assert.

### IN-04: The vendor-asset test checks that the file matches its hash, not that it is the upstream file

**File:** `tests/test_vendor_assets.py:70-90`
**Issue:** The test compares `base.html`'s `integrity` with the file's bytes, plus the byte length. A tampered or wrong build whose integrity attribute was regenerated passes, as long as its size is unchanged or the size constant was edited too.
**Fix:** Pin the published upstream SHA-384 digests as test constants, independent of `base.html`, and assert both the file and the template against them.

### IN-05: "Behind an HTTPS proxy, nothing extra is needed" is not true for every browser

**File:** `docs/how-to/deploy-docker-compose.md` (Running behind a reverse proxy)
**Issue:** Browsers without Fetch Metadata (Safari before 16.4) send no `Sec-Fetch-Site` even over HTTPS. They fall back to the Origin-vs-Host check, so behind nginx's default `Host` rewrite their scans are rejected with 403.
**Fix:** Recommend `proxy_set_header Host $host` (or `X-Forwarded-Host`) for every proxy, and describe the HTTPS case as "usually works without it".

---

_Reviewed: 2026-09-14T22:15:14Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
