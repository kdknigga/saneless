---
phase: 26-worker-and-web-robustness
fixed_at: 2026-09-15T13:28:56Z
review_path: .planning/phases/26-worker-and-web-robustness/26-REVIEW.md
iteration: 1
findings_in_scope: 18
fixed: 18
skipped: 0
status: all_fixed
---

# Phase 26: Code Review Fix Report

**Fixed at:** 2026-09-15T13:28:56Z
**Source review:** .planning/phases/26-worker-and-web-robustness/26-REVIEW.md
**Iteration:** 1

**Summary:**
- Findings in scope: 18 (WR-02..WR-09, IN-01..IN-07, IN-09..IN-11)
- Fixed: 18
- Skipped: 0
- Already resolved before this run, not counted: WR-10, WR-11, IN-08

Every fix passed `ruff check`, `ruff format --check`, `ty check` and `pyrefly check src tests`. The prek commit hooks ran on every commit, and `prek run --all-files` and `prek run --stage pre-push --all-files` both pass at the end. Where a finding changed behaviour, the new test was also run against the code before the fix and failed there.

Six fixes change runtime logic. They are marked **fixed: requires human verification**: WR-02, WR-03, WR-05, WR-06, IN-06 and IN-09.

## Test results

| Run | Result |
|-----|--------|
| `pytest -m "not browser and not sane_hardware"` (the CI job) | 1432 passed |
| `pytest -m browser` (the CI job) | 48 passed |
| `pytest -m sane_hardware` (the CI job) | 5 passed |
| `pytest` with no markers, all tests in one process | 1483 passed, 1 failed, 1 error |

The single-process run has one failure and one error. Both were there before this run:
- **The failure** is `tests/test_cross_origin.py::test_rejection_log_escapes_control_characters_in_the_path`. It calls `asyncio.run` on the main thread, and a Playwright browser test earlier in the same process leaves an event loop running there. Neither that test nor `test_browser.py` changed in this run, and `pytest tests/test_browser.py tests/test_cross_origin.py` fails the same way.
- **The error** is at setup of the next test, `test_web_errors.py::test_htmx_rejection_is_retargeted_into_the_message_slot[CLIENT_ERROR]`. The failed test's un-awaited coroutine raises an unraisable-exception warning, and `filterwarnings = error` turns it into an error.

CI runs the browser tests in a separate job, so CI is not affected. The new WR-07 test first hit the same problem. Commit 40ce10e fixed it by running its handler on a separate thread. The older cross-origin test was left as it is because it is outside this review's scope. It needs the same one-line change.

## Fixed Issues

### WR-02: A failed success-path write records an ERROR for a document Paperless already accepted

**Status:** fixed: requires human verification
**Files modified:** `src/saneless/worker.py`, `tests/test_worker.py`, `docs/explanation/architecture.md`
**Commit:** cceb529
**Applied fix:**
- **Owed writes.** An owed write is now an `_OwedWrite` value that holds the whole `finish_job` call: state, result, error and category. Before, it held only an error and a category.
- **Owing before re-raising.** Both of the loop's own terminal writes go through `_finish_or_owe`, which records the write as owed before re-raising. That covers the DONE/FALLBACK write and the pipeline-failure ERROR write.
- **Replaying.** `_best_effort_fail` retries the owed write when there is one, and builds an ERROR only when nothing is owed. The idle flush replays the full owed write.
- **Rejections.** `owed_rejection_ids` now filters on `owed.category is ErrorCategory.REJECTED`.
- **ERROR path too.** The fix covers the pipeline-failure write as well, which the review did not ask for. Before, a failed ERROR write was also replaced by the store's `disk I/O error` text.
- **Tests.** A parametrized test checks that a DONE write that fails once or twice ends as DONE with its result. A second test checks that a failed ERROR write keeps the pipeline's own text. One existing call-shape assertion now includes `result=None`.

### WR-03: Startup generation replaces `default` in memory, but the file keeps the old one

**Status:** fixed: requires human verification
**Files modified:** `src/saneless/worker.py`, `tests/test_worker.py`
**Commit:** fd2425b
**Applied fix:**
- `_persist_generated_profiles` now returns the names it wrote, or `None` when nothing was persisted.
- A new helper, `_profiles_after_persist`, builds the in-memory set to match what the file will load after a restart:
  - each generated profile that was written;
  - the loaded profile for any name the file already defined, which keeps a spelled-out bare `default`;
  - the whole generated set when nothing was persisted.
- A new test loads a file with an explicit bare `[profiles.default]`. It checks that memory keeps that `default` and that memory matches the file.

### WR-04: Any persist exception outside `(OSError, ConfigError)` throws away the generated profiles

**Status:** fixed
**Files modified:** `src/saneless/worker.py`, `tests/test_worker.py`
**Commit:** 4ab6ae1
**Applied fix:**
- `_persist_generated_profiles` gains a second `except Exception` clause. It logs the exception class name with a traceback and returns `None`, so the generated profiles are still used for this run.
- A parametrized test covers a real tomlkit parse error (`UnexpectedCharError`) and a `UnicodeDecodeError`.

### WR-05: Single-flight re-check lets `invalidate` return stale data when a fetch was already in flight

**Status:** fixed: requires human verification
**Files modified:** `src/saneless/web/cache.py`, `tests/test_cache.py`
**Commit:** 36b5e6e
**Applied fix:**
- `MetadataCache` keeps a generation counter per key. `invalidate` increments it and removes the entry while holding `_locks_guard`.
- `get_or_fetch` records the generation before calling `fetch()`. It stores the result only if the generation has not changed, checked under the same lock.
- A new test invalidates while a fetch is held in flight. It checks that the refresh returns the fresh data and that the cache holds the fresh data.

### WR-06: Every pipeline failure during shutdown is recorded as "server restarted"

**Status:** fixed: requires human verification
**Files modified:** `src/saneless/worker.py`, `tests/test_worker.py`
**Commit:** af967f4
**Applied fix:**
- **Recording the claim.** `WorkerFlipCoordinator.abort_for_shutdown()` arms the coordinator, offers ABORTED and records `aborted_by_shutdown`, all under one lock. The worker thread reads the marker through the same lock, so it cannot read it before it is set. This replaces the review's suggestion of a plain `_shutdown_aborted_job` attribute, which would have been racy.
- **Callers.** `stop()` and the pass-A backstop in `_status_cb` both call `abort_for_shutdown()`.
- **Choosing the record.** `_failure_record(exc, coordinator)` returns `RESTART_REASON` only when that marker is set. Otherwise it returns `str(exc)` and `classify_error(exc)`.
- **Tests.** One test raises a Paperless error while the worker is stopping. Another sends an operator's Abort that claims the answer before shutdown. Both keep their own cause. The two existing shutdown tests still record `RESTART_REASON`.

### WR-07: The error handlers write raw request paths to the log

**Status:** fixed
**Files modified:** `src/saneless/web/errors.py`, `tests/test_web_errors.py`
**Commits:** 1ac1a39, 40ce10e
**Applied fix:**
- `_validation_error` and `_unhandled_exception` log `request.method` and `request.url.path` with `%r`.
- The module docstring now says what is logged and how it is escaped.
- A parametrized test drives both handlers with a scope whose method and path contain control characters. It checks that each log line shows them escaped. Commit 40ce10e moved the test's handler call onto a separate thread, so the test also passes when browser tests run in the same process.

### WR-08: 405 responses lose their required `Allow` header

**Status:** fixed
**Files modified:** `src/saneless/web/errors.py`, `tests/test_web_errors.py`
**Commit:** 1df5ffc
**Applied fix:**
- `render_error` accepts `extra_headers`, and `_http_exception` passes `exc.headers` for a plain `HTTPException`.
- A test on both the htmx and the JSON branch checks that `GET /api/scan` returns 405 with an `Allow` header that includes POST.

### WR-09: The cross-site docs promise more than the Origin/Host check delivers: DNS rebinding gets through

**Status:** fixed
**Files modified:** `docs/reference/web-api.md`, `docs/how-to/deploy-docker-compose.md`, `.planning/phases/26-worker-and-web-robustness/26-CONTEXT.md`
**Commit:** a976d71
**Applied fix:**
- **Reference docs.** The Cross-site requests section now says the check does not stop DNS rebinding and explains why. It lists two mitigations: a proxy that answers only its own hostname, with saneless reachable only through the proxy, or a DNS resolver with rebind protection.
- **How-to.** The reverse-proxy how-to gains an "Answering only your own hostname" section with an nginx default-server example and notes for Caddy and Traefik.
- **Deferred idea.** A `Host` allow-list is recorded under Deferred Ideas in 26-CONTEXT.md.
- **Code.** None changed, as the review asked.
- **Check.** `mkdocs build --strict` passes.

### IN-01: `_set_profiles` is used only by tests

**Status:** fixed
**Files modified:** `src/saneless/worker.py`, `tests/test_worker.py`
**Commit:** bed5550
**Applied fix:**
- `_set_profiles` takes an `only_if` check, which runs under the profile lock, and returns whether it swapped.
- Startup generation now swaps through `_set_profiles(..., only_if=is_bare_default)`.
- A test covers both an accepted swap and a refused one.

### IN-02: `error_message(REJECTED)` advice is wrong for down and degraded rejections

**Status:** fixed
**Files modified:** `src/saneless/vocabulary.py`, `tests/test_vocabulary.py`
**Commit:** f3e44c8
**Applied fix:**
- The REJECTED message now reads "This scan was not started. Check that saneless is ready to scan, then try again."
- The function is still not wired to any UI, so users see no change.
- The message is changed from the wording 26-01-PLAN pinned. That change follows from the finding.

### IN-03: Tests that assert nothing happened can pass without the worker ever ticking

**Status:** fixed
**Files modified:** `tests/test_worker.py`
**Commit:** 2f8746f
**Applied fix:**
- A new `_count_idle_ticks` helper wraps the worker instance's `_idle_housekeeping` and counts completed ticks.
- `test_idle_worker_does_not_prune_before_the_interval` and `test_probe_is_not_called_while_not_degraded` now wait for 10 completed ticks before they assert, instead of sleeping.

### IN-04: The vendor-asset test checks each file against its own hash

**Status:** fixed
**Files modified:** `tests/test_vendor_assets.py`
**Commit:** e1a31a1
**Applied fix:**
- **Source of the digests.** The htmx.org 2.0.8 and @picocss/pico 2.1.1 tarballs were downloaded from the npm registry. Each tarball's sha512 matched the registry's `dist.integrity`. The SHA-384 digests of `dist/htmx.min.js` and `css/pico.min.css` were then computed from the tarballs. Both match the vendored files and the existing `base.html` attributes.
- **The test.** The digests are pinned by hand in `UPSTREAM_SRI`. A new test compares both the files and the `base.html` integrity attributes against them.

### IN-05: "Behind an HTTPS proxy, nothing extra is needed" is not true for every browser

**Status:** fixed
**Files modified:** `docs/how-to/deploy-docker-compose.md`, `docs/reference/web-api.md`
**Commit:** 46b7032
**Applied fix:**
- Both documents now recommend that every proxy keeps `Host` or sets `X-Forwarded-Host`, over HTTPS as well as plain HTTP.
- HTTPS is described as usually working without it, since browsers without Fetch Metadata fall back to the Origin check. Safari before 16.4 is the example.
- Apple's Safari 16.4 release notes confirm that Fetch Metadata support was added in that version.

### IN-06: An owed rejection lost at shutdown comes back after restart as "server restarted"

**Status:** fixed: requires human verification
**Files modified:** `src/saneless/worker.py`, `tests/test_worker.py`, `docs/explanation/architecture.md`
**Commit:** 8d29aea
**Applied fix:**
- After its loop, `_run` calls `_flush_before_exit()`, which makes one attempt to flush the owed writes. A failure is logged at WARNING with a traceback.
- This is the worker thread's own write, so the lifespan's D-07/D-09 rule still holds.
- **Tests.** No idle tick can run during either test. One checks that an owed rejection is written when `stop()` runs. The other checks that a store refusing the final flush is logged and does not block `stop()`.
- The architecture doc's Shutdown paragraph mentions the final flush.

### IN-07: The concurrency tests do not exercise the delete-if-unchanged branch or the degraded drain of an owed rejection

**Status:** fixed
**Files modified:** `tests/test_worker.py`
**Commit:** bae8ec5
**Applied fix:**
- **Re-owe test.** A new deterministic test holds `finish_job` for an id on an Event while the id is owed again with different text. It checks that the newer write survives and lands. Changing the check to an unconditional delete makes the test fail, which was confirmed.
- **Recovery test.** The streak recovery test is now parametrized over `guard` and `rejection` debts. It checks that the probe ran, that the REJECTED row lands through recovery, and that `owed_rejection_ids()` is empty afterwards. A new `_owe_one_write` helper keeps the test under ruff's statement limit.

### IN-09: The owed-write streak survives a job whose store writes all landed

**Status:** fixed: requires human verification
**Files modified:** `src/saneless/worker.py`, `tests/test_worker.py`
**Commit:** a7d2a80
**Applied fix:**
- `_run`'s success branch now resets `_failed_flush_ticks` along with `_consecutive_loop_failures`.
- The attribute comment and the `_idle_housekeeping` docstring are updated.
- **Test.** No idle tick runs during the test, and the streak starts one short of the limit. The test checks that a job whose writes all land resets it to 0.
- The existing architecture text ("a store that heals sooner never degrades it") is now accurate as written.

### IN-10: `latest_run_job(exclude_ids: Collection[str])` silently accepts a bare job id

**Status:** fixed
**Files modified:** `src/saneless/job.py`, `tests/test_job.py`
**Commit:** fb07ed9
**Applied fix:**
- The parameter is now `AbstractSet[str]`, with a default of `frozenset()`.
- One test call that passed a list now passes a set.
- A scratch file calling `latest_run_job(exclude_ids="abc")` was rejected by both ty and pyrefly, and then deleted.

### IN-11: The below-limit streak test hard-codes the limit it claims to mirror

**Status:** fixed
**Files modified:** `tests/test_worker.py`
**Commit:** 65c452f
**Applied fix:** `threshold = worker_module._OWED_RETRY_DEGRADED_AFTER`.

## Already Resolved (not in scope, not counted)

### WR-10: A persistent job-store fault below the degraded threshold never reached `/health`

**File:** `src/saneless/worker.py`
**Reason:** Skipped. The review marks it resolved (26-18). Nothing was changed.

### WR-11: The below-threshold heal test raced the flush's post-write delete

**File:** `tests/test_worker.py`
**Reason:** Skipped. The review marks it resolved (26-18). Nothing was changed.

### IN-08: A refused post-submit attempt could render as a live PENDING job until its rejection landed

**File:** `src/saneless/web/routes.py`
**Reason:** Skipped. The review marks it resolved (26-19). Nothing was changed.

---

_Fixed: 2026-09-15T13:28:56Z_
_Fixer: Claude (gsd-code-fixer)_
_Iteration: 1_
