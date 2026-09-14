---
phase: 26-worker-and-web-robustness
reviewed: 2026-09-14T23:38:23Z
depth: deep
files_reviewed: 42
files_reviewed_list:
  - src/saneless/worker.py
  - src/saneless/job.py
  - src/saneless/web/routes.py
  - src/saneless/web/app.py
  - src/saneless/web/errors.py
  - src/saneless/web/cross_origin.py
  - src/saneless/web/cache.py
  - src/saneless/vocabulary.py
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
  - docs/explanation/architecture.md
  - docs/how-to/configure-scan-profiles.md
  - docs/how-to/deploy-docker-compose.md
  - docs/reference/cli-commands.md
  - docs/reference/configuration.md
  - docs/reference/docker.md
  - docs/reference/environment-variables.md
  - docs/reference/web-api.md
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
findings:
  critical: 0
  warning: 10
  info: 8
  total: 18
status: issues_found
---

# Phase 26: Code Review Report (re-review after gap closure 26-15/16/17)

**Reviewed:** 2026-09-14T23:38:23Z
**Depth:** deep
**Files Reviewed:** 42
**Status:** issues_found

## Narrative Findings (AI reviewer)

## Summary

This is a re-review after plans 26-15, 26-16 and 26-17. The gap-closure diff (`54fdd97..HEAD`) touches only `worker.py`, `job.py`, `routes.py`, `docs/explanation/architecture.md` and four test files. I reviewed that diff line by line and traced it through `JobStore.finish_job`, `fail_active_jobs`, `latest_run_job`, `_current_or_recent_job` and the lifespan. Every file outside the diff is unchanged since the previous review. For those files I re-checked each earlier finding against the current code instead of re-deriving it.

**CR-01 and WR-01 are fixed.** Owed writes are now retried on every idle tick. Refused-before-row submits are a single `INSERT`. A failed post-submit rejection write is owed to the worker. I traced the concurrency the orchestrator asked about:
- **Snapshot, write outside the lock, delete if unchanged.** No update is lost. Each owed id is written by one thread only (the worker), so nothing is written twice.
- **Ordering with `fail_active_jobs`.** It is sound. `_restart_recovery_pending` is only ever True while degraded, and degraded is set before the thread starts. `_unhealthy_rejection` refuses every submit before `create_job` in that state, so a new PENDING row cannot appear between the flush snapshot and `fail_active_jobs(RESTART_REASON)`.
- **An id that is both owed and later re-submitted.** This cannot happen. Every submit gets a fresh `uuid4` row, and no route re-enqueues an existing id.
- **Delete-if-unchanged check.** The per-id equality check can never see a changed entry today. It is harmless, but untested (IN-07).

**What is still wrong in the new code:**
- **WR-10.** The fix covers only *transient* faults. If the store keeps refusing writes (for example, a full disk), the retries fail forever. They are never counted, so `/health` stays 200 while the row is active and the Scan button stays disabled: CR-01's symptom again, visible only in DEBUG logs. Reproduced: 197 failed retries, `health=HEALTHY`, row `PENDING`, `latest_run_job().is_active=True`, `_consecutive_loop_failures=1`.
- **WR-11.** One new worker test reads `_unrecorded_failures` right after the row turns ERROR. That races the flush's delete, which runs after the write. The sibling many-thread test was hardened against exactly this race, but this one was not. With an injected 50 ms delay before the delete, the read returns a non-empty dict.
- **WR-02 is now worse.** A failed success-path write that the guard also cannot record is now actively rewritten as `ERROR: disk I/O error` for a document Paperless already accepted. Before this fix, the row stayed active until restart.

All other earlier warnings and infos (WR-02..WR-09, IN-01..IN-05) are still present and are carried forward with their original IDs. WR-08 was re-reproduced: `GET /api/scan` returns 405 with no `Allow` header.

## Resolved since previous review

### CR-01 (resolved): A failure the loop guard could not record was never retried unless degraded

`_idle_housekeeping` (`src/saneless/worker.py:801-810`) now calls `_flush_unrecorded_failures` on every idle tick when not degraded, and `_try_recover` shares the same flush (`worker.py:882`). The web test `test_status_poll_reenables_the_scan_button_once_an_owed_failure_is_written` (`tests/test_web_state_rendering.py:638`) covers the UI half: stranded row, disabled button, 200 health, then heal and an enabled button. `test_a_failed_best_effort_write_is_retried_until_it_lands` (`tests/test_worker.py:2173`) no longer asserts the stranded row, and it waits for the first job to be terminal before submitting a second. The persistent-fault remnant is tracked separately as WR-10.

### WR-01 (resolved): A rejected-submit row written in two steps could be stranded PENDING

The refused-before-row path now uses `JobStore.create_rejected_job` (`src/saneless/job.py:657-724`), one `INSERT` written already `ERROR`/`REJECTED`. `test_create_rejected_job_runs_one_insert_and_no_update` pins this with a trace callback. In the post-submit path, `_reject_created_job` (`src/saneless/web/routes.py:317-359`) owes a failed write via `worker.owe_rejection` (`worker.py:280-302`), and the next idle tick drains it. The short window in which a status poll can still show the refused attempt as live is tracked as IN-08.

## Warnings

### WR-02: A failed success-path write records an ERROR for a document Paperless already accepted (still present, now aggravated)

**File:** `src/saneless/worker.py:1032-1042`, `src/saneless/worker.py:717-722`, `src/saneless/worker.py:773-786`, `src/saneless/worker.py:846-853`

**Issue:** The terminal `finish_job(DONE/FALLBACK, result=...)` runs after `run_pipeline` has returned, so the upload has already happened. If that write raises, `_run` sends the exception to `_best_effort_fail`, which records `JobState.ERROR` with `str(exc)` (for example `disk I/O error`) and `ErrorCategory.UNKNOWN`.

26-15 makes this worse. If the guard's write also fails, the ERROR tuple goes into `_unrecorded_failures`, and the idle flush now keeps retrying it until it lands. Before 26-15 that row stayed `UPLOADING` until restart and then got `RESTART_REASON`. Now the failure is guaranteed to end up recorded. The operator sees "Error: disk I/O error" for a scan that reached Paperless and will probably scan it again, which creates a duplicate document.

**Fix:** Keep the outcome the loop was trying to write, and replay that outcome instead of converting it into an ERROR. Widen the owed value to carry the full terminal write:
```python
# in _scan_job
job_result = JobResult(...)
state = job_state_for(result.outcome)
try:
    self._job_store.finish_job(job.id, state, result=job_result)
except Exception:
    with self._unrecorded_lock:
        self._unrecorded_failures[job.id] = _OwedWrite(state, job_result, None, None)
    raise
```
Then have `_best_effort_fail` skip ids already owed, and have the flush call `finish_job(job_id, owed.state, result=owed.result, error=owed.error, error_category=owed.category)`.

### WR-03: Startup generation replaces `default` in memory, but the file keeps the old one, so `default` changes after a restart (still present)

**File:** `src/saneless/worker.py:592-607`, `src/saneless/auto_profiles.py:530-532`

**Issue:** This code is unchanged. `is_bare_default` is True for a file that spells out `[profiles.default]` with default values. Generation then replaces the whole in-memory set, including a generated `default`. `write_profiles_to_config(..., force=False)` skips `default` because the key already exists. After a restart, `default` is the old file entry (for example `Flatbed`) and not the generated one (for example `ADF Front`), and nothing regenerates it. On an ADF-only scanner, `default` works until the first restart and then fails.

**Fix:** Make memory match what was persisted. Merge in memory only the names `write_profiles_to_config` actually wrote, plus the loaded `default` when the file already defines one. When nothing was persisted (no config path, or the write failed), replace the whole set.

### WR-04: Any persist exception outside `(OSError, ConfigError)` throws away the generated profiles, contrary to D-18 (still present)

**File:** `src/saneless/worker.py:599-607`, `src/saneless/worker.py:659-672`

**Issue:** This code is unchanged. `_persist_generated_profiles` runs before the in-memory swap and catches only `(OSError, ConfigError)`. A `tomlkit` `ParseError` (a `ValueError`), a `UnicodeDecodeError`, or a tomlkit container error escapes to `_run`'s backstop, and the swap never runs. D-18 requires that profiles which cannot be written are still used in memory.

**Fix:** Swap in memory first and persist afterwards. Or catch `Exception` in `_persist_generated_profiles`, which only logs, and log `type(exc).__name__`.

### WR-05: Single-flight re-check lets `invalidate` return stale data when a fetch was already in flight (still present)

**File:** `src/saneless/web/cache.py:91-103`, `src/saneless/web/cache.py:105-113`, `src/saneless/web/routes.py:516-519`

**Issue:** This code is unchanged. `invalidate` pops the entry without any generation marker. Suppose a fetch F1 started before the invalidate:
1. F1 `set()`s its pre-change list after the pop.
2. The refresh request's `get_or_fetch` waits on the key lock.
3. It re-checks, finds F1's entry (fresh by timestamp), and returns it.

The refresh button returns exactly the stale data it exists to bypass, and caches it for another TTL.

**Fix:** Use a per-key generation counter. `invalidate` bumps the counter under `_locks_guard`. `get_or_fetch` reads the generation before `fetch()`, and calls `set` only if the generation is unchanged. The re-check should also ignore entries stored under an older generation.

### WR-06: Every pipeline failure during shutdown is recorded as "server restarted", even ones shutdown did not cause (still present)

**File:** `src/saneless/worker.py:728-743`, `src/saneless/worker.py:1003-1027`, `src/saneless/worker.py:347-353`

**Issue:** This code is unchanged. `_failure_record` returns `RESTART_REASON` with no category whenever `_stopping` is set, whatever `exc` is. A genuine `PaperlessError`, a jam, or an operator Abort raised inside the 5 s join window is recorded as "The server restarted before this scan finished" with `error_category=NULL`, which breaks D-15's rule to never guess a cause.

The same text now also flows into `_unrecorded_failures` through `_best_effort_fail`, and the idle flush replays it.

**Fix:** Use the restart reason only when `stop()` itself claimed the flip answer. Record `self._shutdown_aborted_job = coordinator.job_id` when `coordinator.signal_abort()` returns True in `stop()`, and check `job.id == self._shutdown_aborted_job` in `_failure_record`. Otherwise use `str(exc), classify_error(exc)`.

### WR-07: The error handlers write raw request paths to the log, contradicting the module's own guarantee (still present)

**File:** `src/saneless/web/errors.py:198-203`, `src/saneless/web/errors.py:220-225`

**Issue:** This code is unchanged. The module docstring (lines 12-13) says no request input reaches a log line. `_validation_error` and `_unhandled_exception` both log `request.url.path` with `%s`, so a percent-encoded newline in the path can forge a log line. `cross_origin.py:149-160` uses `%r` for the same risk and has a test for it.

**Fix:** Use `%r` for `request.url.path` (and `request.method`) in both calls. Add a control-character log test for the 422 and 500 paths.

### WR-08: 405 responses lose their required `Allow` header (still present, re-reproduced)

**File:** `src/saneless/web/errors.py:124-163`, `src/saneless/web/errors.py:177-181`

**Issue:** This code is unchanged. `_http_exception` passes only the status code to `render_error`, which builds a fresh `headers` dict, so `exc.headers` is dropped. Re-reproduced in this review: `GET /api/scan` returns `405` with headers `{'content-length': '90', 'content-type': 'application/json'}`. RFC 9110 §15.5.6 requires `Allow` on a 405.

**Fix:**
```python
def render_error(request, rejection, *, status_code, refresh_history=False,
                 extra_headers: Mapping[str, str] | None = None) -> Response:
    headers: dict[str, str] = dict(extra_headers or {})
    ...
# _http_exception, non-RequestRejected branch:
return render_error(request, rejection_for_status(exc.status_code),
                    status_code=exc.status_code, extra_headers=exc.headers)
```

### WR-09: The cross-site docs promise more than the Origin/Host check delivers: DNS rebinding gets through (still present)

**File:** `docs/reference/web-api.md:203` ("a web page on another site cannot start a scan or answer a flip prompt in your browser"), `src/saneless/web/cross_origin.py:58-79`

**Issue:** This code and doc text are unchanged, and `docs/` contains no mention of rebinding. Branch 2 accepts any request whose `Origin` host equals its `Host`. A rebinding attacker controls both, so the guard passes and a hostile page can start scans on an unauthenticated service bound to `0.0.0.0`.

**Fix:** State the limitation in the Cross-site requests section and in the reverse-proxy how-to. Recommend a proxy that answers only its own hostname, or binding to a specific interface. Record a `Host` allow-list as a deferred idea.

### WR-10: A job-store fault that persists below the degraded threshold never reaches `/health`, so CR-01's stuck UI returns silently

**File:** `src/saneless/worker.py:801-810`, `src/saneless/worker.py:823-857`

**Issue:** `_idle_housekeeping` swallows every failed owed-write retry at `logger.debug` and never counts it. The docstring's reason is "the guard already counted the failure that created the debt". But that one count is 1 of `_DEGRADED_AFTER = 3`, and nothing else can add to it:
- The stranded row is active, so `scan_button.html` renders `#scan-btn` disabled and the operator cannot submit the scans that would fail and degrade the worker.
- The worker has no job, so no loop write happens.

With a store that keeps refusing writes (disk full, read-only remount, a lock held by another process) while reads still work, the row stays `PENDING`/`SCANNING` and the status area shows it as the live job. The button stays disabled and `/health` returns 200 indefinitely. The only log line after the first WARNING is at DEBUG. This is the exact CR-01 symptom ("/health stays 200, so nothing tells an orchestrator or operator"), now limited to faults that do not heal on their own.

Reproduced with a scratch script: `update_state` fails once, `finish_job` always fails, idle tick 10 ms. After 2 s: `finish_calls=197`, `health=HEALTHY`, row `PENDING`, `latest_run_job().is_active=True`, `_consecutive_loop_failures=1`.

A related gap: the flush aborts at the first raise, so one failing entry also blocks every entry after it in that tick.

**Fix:** Count a streak of failed flush ticks as one loop-level failure per `_DEGRADED_AFTER` ticks, or degrade directly after N consecutive failed flush ticks. The existing probe path then takes over, and `/health` tells the truth. Log the first failure of a streak at WARNING:
```python
else:
    try:
        self._flush_unrecorded_failures()
    except Exception:
        self._failed_flush_ticks += 1
        log = logger.warning if self._failed_flush_ticks == 1 else logger.debug
        log("Owed job store writes failed; retrying on the next idle tick", exc_info=True)
        if self._failed_flush_ticks >= _DEGRADED_AFTER:
            self._record_loop_failure()
    else:
        self._failed_flush_ticks = 0
```
Add a test in which `finish_job` never heals, and assert that `health` becomes `DEGRADED` within a bounded number of ticks.

### WR-11: `test_owed_failures_below_the_degraded_threshold_are_written_once_the_store_heals` races the flush's post-write delete

**File:** `tests/test_worker.py:2266-2273` (assertion at the `owed_after == {}` check), `src/saneless/worker.py:848-853`

**Issue:** `_flush_unrecorded_failures` commits `finish_job` first, then takes `_unrecorded_lock` and deletes the entry. The test waits with `wait_for_state` until the last row is ERROR, calls `store.latest_run_job()`, and then immediately runs `dict(worker._unrecorded_failures)`, without the lock and without waiting for the delete. If the worker is descheduled between the commit and the delete, `owed_after` is non-empty and the test fails spuriously.

The 26-17 SUMMARY (Deviation 2) found this exact race and fixed it in `test_owed_rejections_from_many_threads_are_all_written`, whose predicate also waits for `not worker._unrecorded_failures`. This test did not get the same fix. With a 50 ms delay injected before the delete, the same sequence reads back a non-empty dict (`{'4a19...': ('disk I/O error', UNKNOWN)}`). 50 unmodified runs passed, so this is latent, not frequent. It is the kind of failure that shows up on a loaded CI runner.

**Fix:** Wait for the dict to empty before reading it, and read under the lock:
```python
assert _wait_until(lambda: not worker._unrecorded_failures, _STATE_BUDGET)
with worker._unrecorded_lock:
    owed_after = dict(worker._unrecorded_failures)
```

## Info

### IN-01: `_set_profiles` is used only by tests, and the stress tests exercise it instead of the production swap (still present)

**File:** `src/saneless/worker.py:552-567`, `src/saneless/worker.py:600-607`
**Issue:** Production still rebinds `self._settings.profiles` inline at line 607. `_set_profiles` has no production caller, so the D-19 lock stress test proves a method production never runs.
**Fix:** Route the swap in `_generate_startup_profiles` through one locked helper that takes the re-check predicate, and test that helper. Otherwise delete `_set_profiles`.

### IN-02: `error_message(REJECTED)` advice is wrong for down and degraded rejections (still present)

**File:** `src/saneless/vocabulary.py:473-477`
**Issue:** "Wait for the current scan to finish, then try again" fits only QUEUE_FULL. REJECTED rows also cover worker-down and degraded refusals, where no scan is running.
**Fix:** Word it neutrally, or derive the advice from the row's `error` constant.

### IN-03: Tests that assert nothing happened can pass without the worker ever ticking (still present)

**File:** `tests/test_worker.py:2126`, `tests/test_worker.py:2722`
**Issue:** `time.sleep(10 * _FAST_TICK)` followed by an empty-calls assertion still passes if the thread never reached its first `queue.get` on a slow runner. `test_probe_is_not_called_while_not_degraded` is now what CR-01's fix leans on, so this matters more than it did.
**Fix:** Count idle ticks (spy on `_idle_housekeeping`) and wait for at least N ticks before asserting.

### IN-04: The vendor-asset test checks that each file matches its own hash, not that it is the upstream file (still present)

**File:** `tests/test_vendor_assets.py:70-90`
**Issue:** The test compares `base.html`'s `integrity` with the file's bytes. A wrong or tampered build whose integrity attribute was regenerated still passes.
**Fix:** Pin the published upstream SHA-384 digests as constants, and assert both the file and the template against them.

### IN-05: "Behind an HTTPS proxy, nothing extra is needed" is not true for every browser (still present)

**File:** `docs/how-to/deploy-docker-compose.md:115`
**Issue:** Browsers without Fetch Metadata (Safari before 16.4) send no `Sec-Fetch-Site` even over HTTPS. They fall back to Origin-vs-Host, so behind nginx's default `Host` rewrite they get a 403.
**Fix:** Recommend preserving `Host` (or setting `X-Forwarded-Host`) for every proxy, and describe HTTPS as "usually works without it".

### IN-06: An owed rejection lost at shutdown comes back after restart as "server restarted" and shows in the status area

**File:** `src/saneless/worker.py:280-302`, `src/saneless/worker.py:705-716`, `src/saneless/job.py:202-204`, `src/saneless/job.py:174-176`
**Issue:** `owe_rejection` entries live only in memory, and `_run` exits its loop without a final flush. A rejection owed shortly before shutdown (up to one 5 s tick, or longer while the queue is busy) leaves the row `PENDING`. The next startup's `fail_active_jobs(RESTART_REASON)` then records it with `error_category = NULL`, which has two effects:
- History says "The server restarted before this scan finished" for a scan that was never started.
- `latest_run_job` no longer skips the row, so after the restart the refused attempt appears in the status area as the last run job.

This goes against both D-06 and D-15. The `owe_rejection` docstring presents this as the intended fallback.
**Fix:** After the `while` loop in `_run`, try one best-effort `_flush_unrecorded_failures()`, swallowing and logging any exception. This is the worker thread's own final write, so D-07/D-09 still hold.

### IN-07: The concurrency tests do not exercise the lock or the delete-if-unchanged branch, or the degraded drain of an owed rejection

**File:** `tests/test_worker.py:2375-2432`, `src/saneless/worker.py:844-853`
**Issue:** `test_owed_rejections_from_many_threads_are_all_written` owes each id exactly once. Under the GIL it would pass with no `_unrecorded_lock` and an unconditional `del`, so it does not pin the snapshot/compare logic the 26-17 SUMMARY lists as mitigating T-26-69. No test drains an owed **rejection** through `_try_recover` with a worker that is actually degraded. In `test_web_errors.py`, the `degraded` parametrisation only patches `submit`, so the worker takes the non-degraded branch.
**Fix:** Add a deterministic test that re-owes an id with a different tuple while `finish_job` for that id is blocked on an Event, and assert that the second tuple survives and is written on the next tick. Add a `TestOwedRejections` case that degrades the worker first and asserts the rejection lands through the probe path before degraded clears.

### IN-08: A refused post-submit attempt can still render as a live PENDING job until its rejection lands

**File:** `src/saneless/web/routes.py:412-441`, `src/saneless/web/routes.py:90-121`, `src/saneless/worker.py:705-710`
**Issue:** The row from `create_job` is PENDING with no marker until `_reject_created_job` writes it, or until the worker's idle flush does if that write failed. While no job is current, a status poll in that window gets it from `latest_run_job` and renders "Starting scan..." with `#scan-btn` disabled. The owed case can last much longer than one tick. For QUEUE_FULL, idle ticks happen only after the queue has drained and stayed empty for `_IDLE_TICK_SECONDS`, so each gap between queued jobs can show the refused attempt as the live job. The row does heal eventually. The earlier WR-01 already noted the short window.
**Fix:** Have `latest_run_job` (or `_current_or_recent_job`) skip ids in the worker's owed set, for example through a `worker.is_owed(job_id)` read under `_unrecorded_lock`. Or accept and document the window.

---

_Reviewed: 2026-09-14T23:38:23Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
