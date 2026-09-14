---
phase: 25-manual-duplex
reviewed: 2026-09-14T17:41:29Z
depth: deep
files_reviewed: 27
files_reviewed_list:
  - docs/explanation/architecture.md
  - docs/getting-started/first-web-ui-scan.md
  - docs/how-to/set-up-adf-duplex.md
  - docs/reference/cli-commands.md
  - docs/reference/configuration.md
  - docs/reference/web-api.md
  - src/saneless/auto_profiles.py
  - src/saneless/cli.py
  - src/saneless/config.py
  - src/saneless/pipeline.py
  - src/saneless/scanner/sane_backend.py
  - src/saneless/vocabulary.py
  - src/saneless/web/app.py
  - src/saneless/web/routes.py
  - src/saneless/web/templates/partials/flip.html
  - src/saneless/web/templates/partials/status.html
  - src/saneless/worker.py
  - tests/test_auto_profiles.py
  - tests/test_browser.py
  - tests/test_cli.py
  - tests/test_config.py
  - tests/test_outcomes_e2e.py
  - tests/test_pipeline.py
  - tests/test_scanner.py
  - tests/test_vocabulary.py
  - tests/test_web.py
  - tests/test_worker.py
findings:
  critical: 0
  warning: 3
  info: 5
  total: 8
status: issues_found
---

# Phase 25: Code Review Report (re-review after gap closure 25-10..25-15)

**Reviewed:** 2026-09-14T17:41:29Z
**Depth:** deep
**Files Reviewed:** 27
**Status:** issues_found

## Narrative Findings (AI reviewer)

## Summary

This is a re-review of the gap-closure diff `07054af..HEAD`. I read the full files to follow
the cross-module paths: the routes and `_status_context`, then `ScanWorker` and
`WorkerFlipCoordinator`, then `FlipAnswerSlot`, then `_scan_manual_duplex`. I also followed
config into `cli()` and `configure_logging`, and the profile into `_resolve_source` and
`_resolve_feeder_source`.

The gates pass on HEAD:
- `ruff check .` is clean.
- `ty check` is clean.
- `pyrefly check src tests` reports 0 errors.
- 782 tests pass across the nine phase test modules.

**All 13 prior findings are closed in code.** The disposition table follows. I traced the CR-01
fix against the concurrency questions in the brief. It holds:

- **Arm before persist.** `_status_cb` arms before it writes `AWAITING_FLIP`
  (`worker.py:421-431`). Any observer that reads that row therefore finds the coordinator
  armed. Arming happens only after `scan_pages` for pass A has returned
  (`pipeline.py:938-948`), so a click during pass A is still dropped.
  - A window remains where the coordinator is armed but the row is not yet written. A click
    can only land in it by naming job X's id, and nothing has rendered a button for X at that
    point, so the UI cannot reach it.
- **Timeout vs. signal.** Both paths go through the slot lock. `offer` returns `False` once
  anything has claimed the answer. `settle` hands back whatever answer is in effect, so a
  Continue that lands between the wait expiring and `settle` is honoured.
- **Job-id snapshot.** `_signal_flip` reads `self._flip_coordinator` once and compares the
  posted id with that coordinator's own `job_id`. Every job gets its own coordinator. A
  snapshot taken just before `finally` clears the pointer can therefore only signal a dead
  coordinator, never the next job's.
- **Tests.** The regression tests are real, not vacuous.
  `test_the_coordinator_is_armed_before_awaiting_flip_is_persisted` wraps `update_state`. Its
  mock pipeline announces `AWAITING_FLIP` *before* it calls `wait_for_flip`, so the
  backstop arming inside `wait_for_flip` cannot mask a regression.

The new defects are at the edges of the fix, not in its core:

- A worker shut down while parked at the flip prompt leaves the job in `AWAITING_FLIP` for
  good, and the prompt's buttons do nothing (WR-01).
- The WR-05 fix sends the legacy-source warning to the log file, but it no longer reaches
  stderr. The warning is now invisible to CLI users and to the documented Docker deployment
  (WR-02).
- The flip endpoints now require a `job_id` that no documented API surface exposes (WR-03).

I did not flag the approved amendments to D-02 and D-03, or PEP 758 syntax.

## Prior findings disposition

| ID | Prior finding | Status | Evidence |
|----|---------------|--------|----------|
| CR-01 | Coordinator accepted signals before `AWAITING_FLIP`, with no job scoping | **Closed** | `WorkerFlipCoordinator(job_id)` with a one-way `_armed` latch (`worker.py:79-170`). It is armed in `_status_cb` before persisting (`worker.py:421-430`). `continue_flip`/`abort_flip` take `job_id` and compare it with the coordinator snapshot (`worker.py:275-321`). The routes require the `job_id` form field (`routes.py:356, 386`), and `flip.html:42-43` sends it through `hx-vals` with `tojson`, which escapes it for the attribute. Once answered, the partial shows an acknowledgment instead of the buttons (`status.html:13-17`, `routes.py:98-138`). Tests: `test_signals_during_pass_a_are_dropped`, `test_a_double_clicked_abort_cannot_abort_the_next_job`, `test_foreign_job_id_leaves_the_prompt_open`, and the browser test `test_flip_continue_click_answers_the_waiting_job`. |
| WR-01 | `flip_timeout_seconds` unbounded | **Closed** | `Field(default=600, ge=1, le=86_400)` (`config.py:172`). 86 400 is far below `threading.TIMEOUT_MAX`. The e2e tests moved to a 1 s budget with a matching margin. |
| WR-02 | Feeder resolution accepted `FEEDER_DUPLEX` like a simplex feeder | **Closed** | `_resolve_feeder_source` prefers `SourceKind.FEEDER`. It warns when it overrides a named `FEEDER_DUPLEX` source, and refuses when every feeder is duplex (`sane_backend.py:837-866`). This matches the approved D-02 amendment. |
| WR-03 | Manual duplex regressed on devices with no `source` option | **Closed** | `sane_backend.py:922-931` trusts `classify_source(requested).uses_feeder` when no source option exists. A legacy `"Manual Duplex"` classifies `FEEDER_DUPLEX`, which counts as a feeder, so it still runs. |
| WR-04 | `is_bare_default` ignored `duplex` | **Closed** | `settings.profiles["default"] == ProfileConfig()` (`auto_profiles.py:223`). I checked this against pydantic 2.12.5. Equality compares field values, not `model_fields_set`, so a TOML file that spells out the default values still counts as bare. |
| WR-05 | Legacy warning emitted before logging was configured | **Closed, with a new side effect (see WR-02 below)** | `warn_on_legacy_duplex_sources(settings)` is called after `configure_logging` (`cli.py:195-196`). I measured that the record now reaches `log_file`. |
| WR-06 | Exit-code table split by a paragraph | **Closed** | `cli-commands.md:31-38`: row 3 now follows row 2, and the paragraph comes after the table. |
| WR-07 | `web-api.md` promised safety properties the code lacked | **Closed** | The "cannot do this" and "buttons are gone" bullets are replaced by accurate job-scoping and acknowledgment bullets (`web-api.md:160-165`). The new text has one gap of its own; see WR-03. |
| WR-08 | Prompt-thread exception hung the CLI for the full timeout | **Closed** | `except Exception` logs the traceback and settles `ABORTED` right away (`cli.py:143-152`). The test asserts `elapsed < 5` against a 600 s bound. |
| IN-01 | Stale "non-None" docstring | **Closed** | `pipeline.py:74`. |
| IN-02 | Claim-once logic copied in two coordinators | **Closed** | Both coordinators now use `FlipAnswerSlot` (`pipeline.py:154-240`). Arming stays in the web coordinator. |
| IN-03 | "signal sent" logged even when the signal was dropped | **Closed** | `_signal_flip` logs "claimed", "dropped: not yet at the flip prompt", "dropped: already answered", or "dropped: not the job waiting" (`worker.py:292-321`). |
| IN-04 | Legacy source with non-manual `duplex` gave no warning | **Closed** | A second branch in `warn_on_legacy_duplex_sources` (`config.py:299-311`) covers it. |

## Warnings

### WR-01: A worker shut down while parked at the flip prompt leaves the job in `AWAITING_FLIP` for good, and its buttons do nothing

**File:** `src/saneless/worker.py:208-212`; `src/saneless/web/app.py:80-83`; `src/saneless/web/routes.py:131-138`

**Issue:** `ScanWorker.stop()` puts the sentinel on the queue and joins for 5 s. A worker
thread parked in `WorkerFlipCoordinator.wait_for_flip` never reads the queue, and that wait
can now last up to 86 400 s. I reproduced it with a scratch script against the real worker
and store:

```
stop took 5.0 alive True
row state after stop: AWAITING_FLIP
```

The lifespan then runs `job_store.close()` while the worker thread is still alive. In a
process that keeps running after the lifespan (tests, reload), the thread eventually wakes
and calls `finish_job` on a closed connection. That raises `sqlite3.ProgrammingError` from
inside the `except` handler, and the thread dies with a traceback.

In production the process exits and the row stays `AWAITING_FLIP`. `fail_active_jobs` has no
production caller (ROBU-06 belongs to Phase 26). After a restart, `_current_or_recent_job`
returns that row and `_status_context` finds no coordinator, so `flip_answer` is `None`. The
idle page then renders the Continue and Abort buttons with a 1 s poll. Every click is dropped
as "not the job waiting at the flip prompt", and the same buttons render again. This is the
same "click does nothing" symptom CR-01's acknowledgment work set out to remove, and it stays
until another job is created.

The Phase 26 fix will not cover this by itself. A stop flag does not wake an
`Event.wait` on the answer slot.

**Fix:** Resolve the live flip wait during shutdown, so the job ends through the normal
`ERROR` path before the join. Do it through `settle`, which ignores arming:

```python
# worker.py
class WorkerFlipCoordinator(FlipCoordinator):
    def cancel(self) -> None:
        """Resolve the wait as ABORTED regardless of arming (worker shutdown)."""
        self._slot.settle(FlipOutcome.ABORTED)

class ScanWorker:
    def stop(self) -> None:
        coordinator = self._flip_coordinator
        if coordinator is not None:
            coordinator.cancel()
        self._queue.put(None)
        self._thread.join(timeout=5)
```

Add a worker test that parks a manual-duplex job at `AWAITING_FLIP`, calls `stop()`, and
asserts that the thread has exited and the row is `ERROR`.

Also make the status partial stop offering buttons for an `AWAITING_FLIP` row that has no
live coordinator. For example, `_status_context` could pass `flip_live = worker.flip_armed(job.id)`,
and `status.html` could render the flip prompt only when that is true.

### WR-02: The WR-05 fix made the legacy-source warning invisible to CLI users and to Docker deployments

**File:** `src/saneless/cli.py:188-196`; `src/saneless/logging_config.py:41-66`

**Issue:** `configure_logging` attaches only a `RotatingFileHandler`. It adds a stderr handler
only with `-v`, or when the log file cannot be opened. Before the fix, the warning reached
stderr through `logging.lastResort`. It now goes to the log file and nowhere else. I measured
this with `saneless --config legacy.toml jobs`: nothing on stderr, and one record in
`log_file`.

Two groups lose the warning:

- **An interactive `saneless scan` user.** This is the person best placed to act on the
  message. They no longer see it.
- **The documented Docker deployment.** The `Dockerfile` runs `ENTRYPOINT ["saneless"]` /
  `CMD ["serve"]` without `-v`. It sets `SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless`, but
  `log_file` keeps its `$HOME/.local/state/...` default, which is outside the only declared
  `VOLUME`. `docker logs` used to show the warning. Now it lives only in a file inside the
  container.

D-18 makes this message the operator's only migration instruction. The fix traded one
audience for another instead of reaching both.

**Fix:** Also write the warning to stderr, independent of `-v`, while still logging it to
the file:

```python
# config.py
def legacy_duplex_source_warnings(settings: Settings) -> list[str]:
    """Build one message per legacy-looking profile (pure; no logging)."""
    ...

# cli.py, after configure_logging(...)
for message in legacy_duplex_source_warnings(settings):
    logger.warning("%s", message)
    click.echo(f"Warning: {message}", err=True)
```

Add a `CliRunner` test that asserts the message appears in `result.stderr` without `-v`.

### WR-03: The flip endpoints require a `job_id` that no documented API surface exposes

**File:** `docs/reference/web-api.md:136-156, 163`; `src/saneless/web/routes.py:355-410`

**Issue:** Both flip routes now reject a request without `job_id` (422). `web-api.md` tells a
direct API caller to "poll `/api/jobs/current/status` for `AWAITING_FLIP` before answering".
That endpoint, `POST /api/scan` and `/api/jobs/history` all return HTML, and none of them
shows a job id. The only place the id appears is inside the `hx-vals` attribute of the
flip partial's buttons. A script following the reference docs cannot build a valid request
without scraping that attribute, which is undocumented markup. The prior contract took no
fields, so this is a breaking API change with no documented way to migrate.

**Fix:** Pick one of these:

- Document where the id comes from. For example: "the `hx-vals` attribute of the Continue
  button in the `AWAITING_FLIP` partial". Weak, because it makes markup part of the contract.
- Expose the id in a stable way, for example `data-job-id="{{ job.id }}"` on
  `#status-area`, and document it.
- Return the id from `POST /api/scan` in an `HX-Trigger` or `X-Job-Id` response header, and
  document that.

Whichever you choose, add it to the `POST /api/scan` and `GET /api/jobs/current/status`
sections, and fix the "poll ... before answering" sentence so it says how to get the id.

## Info

### IN-01: "A call arriving just after the job ended reports that job" is false when another job is queued, including CR-01's own scenario

**File:** `docs/reference/web-api.md:141`; `src/saneless/web/routes.py:89-95`
**Issue:** `_current_or_recent_job` falls back to `list_recent(limit=1)`, which orders by
`created_at DESC`. When job 1 is aborted and job 2 is already queued, the response and the
following polls show job 2 as `PENDING`, not job 1's `ERROR`. That is exactly the
double-click scenario CR-01 describes. Job 1's abort result is never shown in the status
area, only in the history table. The behaviour is older than this phase, but the rewritten
paragraph keeps the claim.
**Fix:** Qualify the sentence ("...reports that job, unless a newer job has been queued").
Alternatively, have `_status_context` prefer the job named by `claimed[0]` when it rendered a
different job.

### IN-02: A broken CLI prompt is reported at the terminal as an operator abort

**File:** `src/saneless/cli.py:143-152`; `src/saneless/pipeline.py:956-958`
**Issue:** After WR-08 the operator sees `Scan error: Manual duplex scan aborted at the flip
prompt`, but they did not abort. Without `-v` the traceback that explains why goes only to
`log_file`, and the terminal gives no hint to look there. D-09 rules out a fourth outcome, so
the message cannot change through `FlipOutcome`.
**Fix:** In the `except Exception` arm, also
`click.echo("Flip prompt failed; see the log for details", err=True)` before settling.

### IN-03: The status poll reads the store and the coordinator separately, so a job that ends between the two reads briefly shows its buttons again

**File:** `src/saneless/web/routes.py:131-138`
**Issue:** The poll reads `AWAITING_FLIP` from the store. The worker then settles `ABORTED`
or `TIMED_OUT`, writes `ERROR`, and clears `_flip_coordinator` in `finally`. After that,
`worker.flip_answer(job.id)` returns `None`, and the partial renders Continue and Abort for
a job that has already ended. The `claimed` parameter covers this race for the route that
made the claim, but not for `current_job_status` or `index`. It is harmless: a click is
dropped and the next poll fixes the view. It is the last place where CR-01's "buttons that
do nothing" can still appear.
**Fix:** Read the coordinator first, or re-read the job after `flip_answer` returns `None`
and render from that second read.

### IN-04: Plan IDs in code docstrings will go stale

**File:** `src/saneless/worker.py:259-260`
**Issue:** "25-11's status rendering reads this..." names a planning artifact, not a code
path. Once `.planning/` is archived, the reference means nothing to a reader.
**Fix:** Name the caller instead: "`routes._status_context` reads this...".

### IN-05: A full queue blocks the event loop, so Continue and Abort cannot be served while the worker is parked at the flip prompt

**File:** `src/saneless/web/routes.py:241`; `src/saneless/worker.py:197, 222`
**Issue:** `start_scan` is `async def` and calls `worker.submit`, which is
`queue.Queue(maxsize=10).put`, a blocking call. While the worker waits at a flip prompt, ten
queued jobs plus one more submit block the event loop. The `/api/flip/*` routes then cannot
run, the prompt cannot be answered, and everything stays stuck until `flip_timeout_seconds`
(up to a day) expires. This belongs to Phase 26 (ROBU-02 429 backpressure, ROBU-05 blocking
routes). It is recorded here because the flip wait is what turns a slow submit into a
deadlock.
**Fix:** Make sure Phase 26's ROBU-02 test covers a worker parked at `AWAITING_FLIP`. Use
`put_nowait` and return 429 on `queue.Full`.

---

_Reviewed: 2026-09-14T17:41:29Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
