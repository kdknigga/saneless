---
phase: 26-worker-and-web-robustness
reviewed: 2026-09-15T03:49:52Z
depth: deep
files_reviewed: 43
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
  warning: 8
  info: 10
  total: 18
status: issues_found
---

# Phase 26: Code Review Report (re-review after gap closure 26-18/19)

**Reviewed:** 2026-09-15T03:49:52Z
**Depth:** deep
**Files Reviewed:** 43
**Status:** issues_found

## Narrative Findings (AI reviewer)

## Summary

This re-review covers plans 26-18 and 26-19. The gap-closure diff (`538df02..HEAD -- src tests docs`) touches:
- `worker.py`: `_OWED_RETRY_DEGRADED_AFTER`, `_failed_flush_ticks`, and `owed_rejection_ids()`
- `job.py`: `latest_run_job(exclude_ids)`
- `routes.py`: `_current_or_recent_job`
- two docs paragraphs
- four test files

I read that diff line by line. I traced it through `_idle_housekeeping`, `_try_recover`, `_flush_unrecorded_failures`, `_best_effort_fail`, `_reject_created_job`, `_current_or_recent_job` and the `scan_button.html` / `status_response.html` render path. Every other file in scope is unchanged since `538df02` (confirmed with `git diff --stat`). For those files I re-checked each earlier finding against the current code and did not re-derive it.

**WR-10, WR-11 and IN-08 are fixed.**
- **WR-10.** Re-ran the earlier scratch reproduction (an owed write whose `finish_job` never heals, idle tick 50 ms). The worker is now `DEGRADED` after 3 calls and 0.155 s. The status lookup returns `None` for the owed refused attempt.
- **Timing tests.** The new tests passed 6 runs out of 6 with all 16 CPUs held busy by spinning processes. The web tests passed 5 runs out of 5 under the same load.

**Concurrency questions the orchestrator asked about:**
- **Lock discipline.** Every production access to `_unrecorded_failures` holds `_unrecorded_lock` (`worker.py:315-316`, `338-343`, `827-828`, `908-909`, `915-917`). No store call runs while that lock is held. The request path takes `_unrecorded_lock` and then, separately, the store `RLock`, never both at once. The flush does the same. There is no nested acquisition, so no lock-order inversion.
- **`_failed_flush_ticks`.** Only the worker thread reads or writes it (`_idle_housekeeping` and `_try_recover`, both called from `_run`). Request threads see only `_degraded`, an `Event`.
- **Reset order in `_try_recover`.** It is correct. Both counters are zeroed (`worker.py:957-958`) before `_degraded.clear()` (`959`). A test that waits for `HEALTHY` therefore always reads zeroed counters, and `test_a_worker_degraded_by_a_failed_retry_streak_recovers_once_the_owed_write_lands` depends on that.
- **Streak degrade and `_record_loop_failure`.** They cannot double-set degraded. The streak branch runs only while not degraded, so its "degraded" WARNING fires once per streak.
- **The IN-08 snapshot race.** It is sound in both directions:
  - An id leaves the owed set only after its `finish_job` commits, and by then `latest_run_job` skips the row by its REJECTED marker.
  - If `owe_rejection` lands after the poll's snapshot, the row is still PENDING and can render as live. This is the residual window documented in `_current_or_recent_job`'s docstring.
- **`LIMIT len(skip) + 1`.** It is correct even when excluded ids match no row, or were written REJECTED between the snapshot and the query. Those ids only widen the limit.

**New in this round (all Info):**
- **IN-09.** The owed-write streak survives a job whose writes all landed, which contradicts the new architecture text "a store that heals sooner never degrades it". Reproduced: after a clean job the streak was still 2 while `_consecutive_loop_failures` was 0.
- **IN-10.** `exclude_ids: Collection[str]` also accepts a bare `str`.
- **IN-11.** One new streak test hard-codes the threshold instead of reading the constant.

WR-02..WR-09 and IN-01..IN-07 are unchanged and carried forward with their original IDs. Line numbers are updated where `worker.py` shifted.

## Resolved since previous review

| ID | Status | Notes |
|----|--------|-------|
| CR-01 | Resolved (in the previous round) | Still resolved. The idle flush runs on every non-degraded tick (`worker.py:846-874`). |
| WR-01 | Resolved (in the previous round) | Still resolved. `create_rejected_job` is a single INSERT, and `_reject_created_job` owes a failed write. |
| WR-02 | **Still open** | Unchanged. See below. |
| WR-03 | **Still open** | Unchanged. |
| WR-04 | **Still open** | Unchanged. |
| WR-05 | **Still open** | Unchanged (`cache.py` has no diff). |
| WR-06 | **Still open** | Unchanged. |
| WR-07 | **Still open** | Unchanged (`errors.py` has no diff). |
| WR-08 | **Still open** | Unchanged (`errors.py` has no diff). |
| WR-09 | **Still open** | Unchanged. The doc anchor is now `web-api.md:205`. |
| WR-10 | **Resolved** | See below. |
| WR-11 | **Resolved** | See below. |
| IN-01 | **Still open** | Unchanged. |
| IN-02 | **Still open** | Unchanged. |
| IN-03 | **Still open** | Unchanged (`test_worker.py:2131`, `3004`). |
| IN-04 | **Still open** | Unchanged. |
| IN-05 | **Still open** | Unchanged. |
| IN-06 | **Still open** | Unchanged. `_run` still exits without a final flush. |
| IN-07 | **Still open** | Unchanged. No new test re-owes an id mid-write, or drains an owed *rejection* through `_try_recover`. |
| IN-08 | **Resolved** | See below. |

### WR-10 (resolved): A persistent job-store fault below the degraded threshold never reached `/health`

`_idle_housekeeping` (`src/saneless/worker.py:849-874`) now counts failed flush ticks in `_failed_flush_ticks`. It logs the first failure of a streak at WARNING with `exc_info` and later ones at DEBUG, and it sets `_degraded` once the streak reaches `_OWED_RETRY_DEGRADED_AFTER = 3`. A flush that returns resets the streak, and so does a successful `_try_recover` (`worker.py:958`).

`TestOwedWriteStreak` pins the behaviour: degrade within the limit for both a guard debt and a rejection debt, recovery once the owed write lands, and no degrade for streaks shorter than the limit. `test_health_reports_the_job_store_failing_while_an_owed_failure_cannot_be_written` pins it end to end through `/health`.

The earlier side note still holds: the flush stops at the first raise, so one failing entry blocks the entries after it for that tick. The streak now bounds that to a degraded worker, so it is no longer silent. It is not raised as a separate finding.

### WR-11 (resolved): The below-threshold heal test raced the flush's post-write delete

`tests/test_worker.py:2285-2287` now waits for `_unrecorded_failures` to empty, then copies it under `_unrecorded_lock`, which is the fix the earlier review proposed. The new tests use the same pattern (`test_worker.py:2631-2635`).

### IN-08 (resolved): A refused post-submit attempt could render as a live PENDING job until its rejection landed

`_current_or_recent_job` (`src/saneless/web/routes.py:128`) passes `worker.owed_rejection_ids()` (`worker.py:318-343`) to `JobStore.latest_run_job(exclude_ids=...)` (`job.py:891-925`). The snapshot lists only REJECTED entries, so a guard-owed failure for a job that ran is still reported under D-17. The three `test_a_refused_attempt_whose_rejection_is_still_owed_is_not_shown_as_the_live_job` cases first check that the store alone would pick the PENDING row, then that the poll shows "Ready to scan." with `#scan-btn` enabled. The window between `create_job` and `owe_rejection` is accepted and documented.

## Warnings

### WR-02: A failed success-path write records an ERROR for a document Paperless already accepted (still present)

**File:** `src/saneless/worker.py:1099-1109`, `src/saneless/worker.py:759-764`, `src/saneless/worker.py:801-828`, `src/saneless/worker.py:908-917`

**Issue:** This code is unchanged. The terminal `finish_job(DONE/FALLBACK, result=...)` runs after `run_pipeline` has returned, so the upload has already happened. If that write raises, `_run` sends the exception to `_best_effort_fail`, which records `JobState.ERROR` with `str(exc)` (for example `disk I/O error`) and `ErrorCategory.UNKNOWN`.

If the guard's write also fails, the idle flush retries the ERROR tuple until it lands. With 26-18, a store that does not heal also degrades the worker, but the eventual record is still an ERROR. The operator sees "Error: disk I/O error" for a scan that reached Paperless and will probably scan it again, which creates a duplicate document.

**Fix:** Keep the outcome the loop was trying to write, and replay that outcome instead of an ERROR. Widen the owed value to carry the full terminal write:
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
Then have `_best_effort_fail` skip ids that are already owed, and have the flush call `finish_job(job_id, owed.state, result=owed.result, error=owed.error, error_category=owed.category)`. `owed_rejection_ids` must then filter on `owed.category is ErrorCategory.REJECTED`.

### WR-03: Startup generation replaces `default` in memory, but the file keeps the old one, so `default` changes after a restart (still present)

**File:** `src/saneless/worker.py:634-649`, `src/saneless/auto_profiles.py:530-532`

**Issue:** This code is unchanged. `is_bare_default` is True for a file that spells out `[profiles.default]` with default values. Generation then replaces the whole in-memory set, including a generated `default`. `write_profiles_to_config(..., force=False)` skips `default` because the key already exists. After a restart, `default` is the old file entry and not the generated one, and nothing regenerates it.

**Fix:** Make memory match what was persisted. Merge in memory only the names `write_profiles_to_config` actually wrote, plus the loaded `default` when the file already defines one. When nothing was persisted, replace the whole set.

### WR-04: Any persist exception outside `(OSError, ConfigError)` throws away the generated profiles, contrary to D-18 (still present)

**File:** `src/saneless/worker.py:641`, `src/saneless/worker.py:701-714`

**Issue:** This code is unchanged. `_persist_generated_profiles` runs before the in-memory swap and catches only `(OSError, ConfigError)`. A `tomlkit` `ParseError` (a `ValueError`), a `UnicodeDecodeError`, or a tomlkit container error escapes to `_run`'s backstop at `worker.py:742-746`, and the swap at `648-649` never runs.

**Fix:** Swap in memory first and persist afterwards. Or catch `Exception` in `_persist_generated_profiles`, which only logs, and log `type(exc).__name__`.

### WR-05: Single-flight re-check lets `invalidate` return stale data when a fetch was already in flight (still present)

**File:** `src/saneless/web/cache.py:91-103`, `src/saneless/web/cache.py:105-113`, `src/saneless/web/routes.py:523-527`

**Issue:** This code is unchanged. `invalidate` pops the entry without a generation marker. Suppose a fetch F1 started before the invalidate:
1. F1 `set()`s its pre-change list after the pop.
2. The refresh request's `get_or_fetch` waits on the key lock.
3. It re-checks, finds F1's entry (fresh by timestamp), and returns it.

The refresh button returns the stale data it exists to bypass, and caches it for another TTL.

**Fix:** Use a per-key generation counter. `invalidate` bumps the counter under `_locks_guard`. `get_or_fetch` records the generation before `fetch()` and calls `set` only if the generation is unchanged. The re-check ignores entries stored under an older generation.

### WR-06: Every pipeline failure during shutdown is recorded as "server restarted", even ones shutdown did not cause (still present)

**File:** `src/saneless/worker.py:770-785`, `src/saneless/worker.py:1070-1085`, `src/saneless/worker.py:389-395`

**Issue:** This code is unchanged. `_failure_record` returns `RESTART_REASON` with no category whenever `_stopping` is set, whatever `exc` is. A genuine `PaperlessError`, a jam, or an operator Abort raised inside the 5 s join window is recorded as "The server restarted before this scan finished" with `error_category=NULL`, which breaks D-15's rule to never guess a cause. The same text also flows into `_unrecorded_failures` through `_best_effort_fail`.

**Fix:** Use the restart reason only when `stop()` itself claimed the flip answer. Record `self._shutdown_aborted_job = coordinator.job_id` when `coordinator.signal_abort()` returns True in `stop()`, and check `job.id == self._shutdown_aborted_job` in `_failure_record`. Otherwise use `str(exc), classify_error(exc)`.

### WR-07: The error handlers write raw request paths to the log, contradicting the module's own guarantee (still present)

**File:** `src/saneless/web/errors.py:198-203`, `src/saneless/web/errors.py:220-225`

**Issue:** This code is unchanged. The module docstring says no request input reaches a log line. `_validation_error` and `_unhandled_exception` both log `request.url.path` with `%s`, so a percent-encoded newline in the path can forge a log line. `cross_origin.py` uses `%r` for the same risk and has a test for it.

**Fix:** Use `%r` for `request.url.path` and `request.method` in both calls. Add a control-character log test for the 422 and 500 paths.

### WR-08: 405 responses lose their required `Allow` header (still present)

**File:** `src/saneless/web/errors.py:124-163`, `src/saneless/web/errors.py:177-181`

**Issue:** This code is unchanged. `_http_exception` passes only the status code to `render_error`, which builds a fresh `headers` dict (`errors.py:145`), so `exc.headers` is dropped. `GET /api/scan` returns 405 with no `Allow` header, and RFC 9110 §15.5.6 requires one on a 405.

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

**File:** `docs/reference/web-api.md:205`, `src/saneless/web/cross_origin.py:58-79`

**Issue:** This code and doc text are unchanged, and `docs/` still says nothing about rebinding. Branch 2 accepts any request whose `Origin` host equals its `Host`. A rebinding attacker controls both, so the guard passes and a hostile page can start scans on an unauthenticated service bound to `0.0.0.0`.

**Fix:** State the limitation in the Cross-site requests section and in the reverse-proxy how-to. Recommend a proxy that answers only its own hostname, or binding to a specific interface. Record a `Host` allow-list as a deferred idea.

## Info

### IN-01: `_set_profiles` is used only by tests, and the stress tests exercise it instead of the production swap (still present)

**File:** `src/saneless/worker.py:594-609`, `src/saneless/worker.py:642-649`
**Issue:** Production still rebinds `self._settings.profiles` inline at line 649. `_set_profiles` has no production caller.
**Fix:** Route the swap in `_generate_startup_profiles` through one locked helper that takes the re-check predicate, and test that helper. Otherwise delete `_set_profiles`.

### IN-02: `error_message(REJECTED)` advice is wrong for down and degraded rejections (still present)

**File:** `src/saneless/vocabulary.py:473-477`
**Issue:** "Wait for the current scan to finish, then try again" fits only QUEUE_FULL. REJECTED rows also cover worker-down and degraded refusals, including the new streak degrade, where no scan is running.
**Fix:** Word it neutrally, or derive the advice from the row's `error` constant.

### IN-03: Tests that assert nothing happened can pass without the worker ever ticking (still present)

**File:** `tests/test_worker.py:2131`, `tests/test_worker.py:3004`
**Issue:** `time.sleep(10 * _FAST_TICK)` followed by an empty-calls assertion still passes if the thread never reached its first `queue.get` on a slow runner.
**Fix:** Count idle ticks (spy on `_idle_housekeeping`) and wait for at least N ticks before asserting.

### IN-04: The vendor-asset test checks that each file matches its own hash, not that it is the upstream file (still present)

**File:** `tests/test_vendor_assets.py:70-90`
**Issue:** A wrong or tampered build whose integrity attribute was regenerated still passes.
**Fix:** Pin the published upstream SHA-384 digests as constants, and assert both the file and the template against them.

### IN-05: "Behind an HTTPS proxy, nothing extra is needed" is not true for every browser (still present)

**File:** `docs/how-to/deploy-docker-compose.md:115`
**Issue:** Browsers without Fetch Metadata (Safari before 16.4) send no `Sec-Fetch-Site`, even over HTTPS. They fall back to the Origin-vs-Host check, so behind nginx's default `Host` rewrite they get a 403.
**Fix:** Recommend preserving `Host` (or setting `X-Forwarded-Host`) for every proxy, and describe HTTPS as "usually works without it".

### IN-06: An owed rejection lost at shutdown comes back after restart as "server restarted" and shows in the status area (still present)

**File:** `src/saneless/worker.py:294-316`, `src/saneless/worker.py:747-758`, `src/saneless/job.py:204-206`, `src/saneless/job.py:174-176`
**Issue:** `owe_rejection` entries live only in memory, and `_run` exits its loop without a final flush. On the next startup, `fail_active_jobs(RESTART_REASON)` records the PENDING row with `error_category = NULL`. The IN-08 fix does not help after a restart: the new process's `owed_rejection_ids()` is empty, so `latest_run_job` returns the row. History shows "server restarted" for a scan that never started, and the refused attempt appears in the status area.
**Fix:** After the `while` loop in `_run`, try one best-effort `_flush_unrecorded_failures()`, swallowing and logging any exception.

### IN-07: The concurrency tests do not exercise the delete-if-unchanged branch, or the degraded drain of an owed rejection (still present)

**File:** `tests/test_worker.py:2398-2457`, `tests/test_worker.py:2521-2588`, `src/saneless/worker.py:908-917`
**Issue:** `test_owed_rejections_from_many_threads_are_all_written` owes each id exactly once, so it would pass with an unconditional `del`. `TestOwedWriteStreak`'s `rejection` case degrades the worker with an owed rejection but never heals the store. Only the guard debt is drained through `_try_recover` (`test_a_worker_degraded_by_a_failed_retry_streak_recovers_once_the_owed_write_lands`). As a result, no test proves that an owed REJECTED row lands through the probe path, or that `owed_rejection_ids()` empties there.
**Fix:**
- Add a deterministic test that re-owes an id with a different tuple while `finish_job` for that id is blocked on an Event. Assert that the second tuple survives and is written on the next tick.
- Parametrize the recovery test over `debt` the way the degrade test is.

### IN-08: resolved (see above)

### IN-09: The owed-write streak survives a job whose store writes all landed, contradicting the new docs (new)

**File:** `src/saneless/worker.py:765-768`, `src/saneless/worker.py:849-874`, `docs/explanation/architecture.md:56`
**Issue:** `_run` resets `_consecutive_loop_failures` after a job whose writes all land (`worker.py:768`), but nothing resets `_failed_flush_ticks` there. Only a flush that returns, or a recovery, resets it. Idle ticks happen only between jobs, so "three idle ticks in a row" can span one or more jobs that proved the store accepts writes.

Scratch reproduction: an owed rejection, two failed ticks, then the store heals and a job runs with every write landing. Afterwards `_failed_flush_ticks == 2`, `_consecutive_loop_failures == 0`, and the id is still owed. A single failed tick after that degrades the worker and makes `/health` return 503. The new architecture text says "a store that heals sooner never degrades it", which this sequence contradicts.

The impact is small: the next tick's probe and flush clear it once the fault is gone. But with an intermittent fault, such as a periodic external write lock, it is a false degrade, and the `_OWED_RETRY_DEGRADED_AFTER` comment's "about 15 s" bound does not describe it.
**Fix:** Reset the streak wherever the loop-failure run is reset, which also matches D-10's "a job whose store writes all landed breaks the run":
```python
else:
    self._consecutive_loop_failures = 0
    self._failed_flush_ticks = 0
```
Or reword the docs to say the ticks need not be consecutive in time.

### IN-10: `latest_run_job(exclude_ids: Collection[str])` silently accepts a bare job id (new)

**File:** `src/saneless/job.py:891`, `src/saneless/job.py:919`
**Issue:** A `str` is itself a `Collection[str]`, so `latest_run_job(exclude_ids=job.id)` passes both ty and pyrefly. It then builds `frozenset(job.id)`, a set of single characters, which excludes nothing and widens the `LIMIT` to 37. The only caller passes a `frozenset` today, so this is a trap for future callers, not a live bug.
**Fix:** Type the parameter as `AbstractSet[str]` (or `frozenset[str]`, which the only caller already passes), or reject `str` at runtime:
```python
if isinstance(exclude_ids, str):
    msg = "exclude_ids must be a collection of ids, not one id"
    raise TypeError(msg)
```

### IN-11: The below-limit streak test hard-codes the limit it claims to mirror (new)

**File:** `tests/test_worker.py:2672-2688`
**Issue:** `threshold = 3  # Mirrors _OWED_RETRY_DEGRADED_AFTER` sets up the `finish_job` failure pattern (`range(1, threshold + 1)` and `range(threshold + 2, 2 * threshold + 2)`). If the constant is raised to 4, the test still passes, but its streaks are two short of the limit instead of one, so the "one fewer than the limit" boundary goes untested. If the constant is lowered to 2, the test fails with a degraded worker, and nothing points at the stale mirror. The sibling test reads `worker_module._OWED_RETRY_DEGRADED_AFTER` directly (`test_worker.py:2576-2578`).
**Fix:** `threshold = worker_module._OWED_RETRY_DEGRADED_AFTER`.

---

_Reviewed: 2026-09-15T03:49:52Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
