---
phase: 25-manual-duplex
reviewed: 2026-09-14T15:09:53Z
depth: deep
files_reviewed: 28
files_reviewed_list:
  - src/saneless/auto_profiles.py
  - src/saneless/cli.py
  - src/saneless/config.py
  - src/saneless/pipeline.py
  - src/saneless/scanner/base.py
  - src/saneless/scanner/sane_backend.py
  - src/saneless/vocabulary.py
  - src/saneless/web/routes.py
  - src/saneless/worker.py
  - tests/conftest.py
  - tests/test_auto_profiles.py
  - tests/test_cli.py
  - tests/test_config.py
  - tests/test_outcomes_e2e.py
  - tests/test_pipeline.py
  - tests/test_scanner.py
  - tests/test_vocabulary.py
  - tests/test_web.py
  - tests/test_worker.py
  - docs/explanation/architecture.md
  - docs/getting-started/first-web-ui-scan.md
  - docs/how-to/cli-scripting.md
  - docs/how-to/configure-scan-profiles.md
  - docs/how-to/set-up-adf-duplex.md
  - docs/reference/cli-commands.md
  - docs/reference/configuration.md
  - docs/reference/environment-variables.md
  - docs/reference/web-api.md
findings:
  critical: 1
  warning: 8
  info: 4
  total: 13
status: issues_found
---

# Phase 25: Code Review Report

**Reviewed:** 2026-09-14T15:09:53Z
**Depth:** deep
**Files Reviewed:** 28
**Status:** issues_found

## Narrative Findings (AI reviewer)

## Summary

I reviewed the whole `42cb5ef..HEAD` diff and used the full files to follow calls across
modules: config → pipeline → worker/routes → scanner backend, plus the CLI coordinator. The
gates pass on HEAD: `ruff check .` is clean, `ty check` is clean, and 723 tests pass across
the nine phase test modules. The main structure is sound. There is one decision point in
`run_pipeline`, the `match`/`assert_never` dispatch is total, the no-coordinator refusal
comes before any SANE contact, and the `Auto` substitution cannot be reached from the feeder
branch.

I did not flag the deliberate choices in 25-CONTEXT.md: D-08 (mismatch pages kept), D-15
(abort is a plain `ScanError`), D-16 (first answer wins within one job), D-18 (legacy form
removed from the docs) and D-19 (the orphaned prompt thread keeps stdin after a timeout).

**Deferred item (early flip signal): CONFIRMED, and rated BLOCKER, not a minor API quirk.**
The deferred note says only direct API callers can reach it. That is wrong. A normal browser
double-click, or a retry, reaches it, because the flip routes re-render the stale
`AWAITING_FLIP` prompt, buttons included. I reproduced this against the real `ScanWorker`
and `run_pipeline`. An Abort click meant for job 1 ended job 2, which was queued, with
`ERROR: Manual duplex scan aborted at the flip prompt`. Nobody ever saw job 2's flip prompt.
See CR-01.

Other problems:
- `flip_timeout_seconds` has no bounds.
- Feeder resolution treats a hardware-duplex source the same as a simplex feeder.
- A device with no `source` option regressed: it can no longer do manual duplex.
- `is_bare_default` ignores the new `duplex` field.
- The legacy-source warning is emitted before logging is configured.
- `docs/reference/cli-commands.md` has a broken exit-code table.

## Critical Issues

### CR-01: The flip coordinator accepts answers from the moment the job starts, so a stale or early Continue/Abort lands on the wrong pass or the wrong job

**File:** `src/saneless/worker.py:171-183, 256`; `src/saneless/web/routes.py:309-345`; `src/saneless/pipeline.py:856-857`

**Issue:** `_process_job` creates a `WorkerFlipCoordinator` before pass A (`worker.py:256`).
`continue_flip`/`abort_flip` signal it whenever it exists, and `_resolve` keeps the first
answer permanently. Nothing ties a signal to the job's `AWAITING_FLIP` window or to a
particular job. There are three ways to reach this:

1. **Browser double-click or retry after Abort, with a queued manual-duplex job.**
   `abort_flip` signals, then renders `_current_or_recent_job` straight away. The worker
   thread has not yet woken and written `ERROR`, so the response is job 1 still in
   `AWAITING_FLIP`. The Continue and Abort buttons are rendered again, and the click looks
   like it did nothing. Job 2 starts within milliseconds and gets a fresh coordinator. A
   second Abort click during job 2's pass A is kept, and job 2 fails as soon as its fronts
   finish. Reproduction (scratch script against the real worker and pipeline, job 2's pass A
   held open by the fake scanner):
   ```
   route renders after 1st abort: one AWAITING_FLIP
   job2 state before 2nd click: SCANNING
   job1: ERROR Manual duplex scan aborted at the flip prompt
   job2: ERROR Manual duplex scan aborted at the flip prompt scan calls: 2
   ```
   Without the gate, job 2 reached `AWAITING_FLIP` within 100 ms and was aborted there by
   the click meant for job 1.
2. **Continue during pass A (API, or the same stale re-render when job 1 ends quickly).**
   `wait_for_flip` returns `CONTINUED` the moment pass A ends, and pass B starts on an
   unflipped stack. If the tray is empty, pass B raises `FeederEmptyError` and the fronts
   are thrown away with the `TemporaryDirectory`. If the operator has already reloaded the
   stack but not flipped it, the fronts are scanned again as "backs". The counts match, the
   pages are interleaved, and the job shows a green `DONE` for a wrong document. This is
   C-02, the failure this phase exists to close.
3. Any late POST retried by a proxy or by htmx lands on whichever manual-duplex job is
   current.

**Fix:** Arm the coordinator only when the pipeline announces the wait, and make the routes
name the job they are answering.
```python
# worker.py
class WorkerFlipCoordinator(FlipCoordinator):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._armed = False
        self._outcome: FlipOutcome | None = None

    def arm(self) -> None:
        with self._lock:
            self._armed = True

    def _signal(self, outcome: FlipOutcome) -> bool:
        with self._lock:
            if not self._armed or self._outcome is not None:
                return False          # early or late: dropped
            self._outcome = outcome
        self._event.set()
        return True

# in _status_cb, after persisting the state:
if state is JobState.AWAITING_FLIP and self._flip_coordinator is not None:
    self._flip_coordinator.arm()

# ScanWorker.continue_flip(job_id: str) / abort_flip(job_id: str):
if job_id != self._current_job_id: return
```
Put the job id in the flip partial (`hx-vals='{"job_id": "{{ job.id }}"}'`) and read it in
both routes. Add a worker test that sends an Abort during pass A and during the next job's
pass A, and asserts that both are dropped. Then remove the deferred-items entry and the
"call them only while AWAITING_FLIP" caveat in `web-api.md`.

## Warnings

### WR-01: `flip_timeout_seconds` is unbounded; 0 or a negative value fails every manual duplex job after pass A, and a very large value crashes

**File:** `src/saneless/config.py:167`

**Issue:** `flip_timeout_seconds: int = 600` accepts any int. I measured both edges.
`OutputConfig(flip_timeout_seconds=-5)` validates, and `Event.wait(-5)` returns at once.
Zero is a common way to write "no timeout", and with 0 or a negative value every manual
duplex job scans the whole stack, then fails with "timed out after 0 seconds" and throws
the fronts away. In the CLI it also leaves a prompt printed that nobody can answer. At the
other end, a large value meant as "effectively forever" (above `threading.TIMEOUT_MAX`,
about 9.2e9) makes `Event.wait` raise
`OverflowError: timestamp too large to convert to C PyTime_t`. That happens after pass A.
The web job ends `ERROR` with that text, and `saneless scan` shows a raw traceback, because
`OverflowError` is neither `ScanError` nor `PaperlessError`.

**Fix:**
```python
flip_timeout_seconds: int = Field(default=600, ge=1, le=86_400)
```
`test_unanswered_prompt_times_out_before_pass_b` depends on `0`. Switch it to building a
`ClickFlipCoordinator` directly and calling `wait_for_flip(0)`, which is the coordinator's
own contract, as the Ctrl-C test already does.

### WR-02: Feeder resolution treats a hardware-duplex source the same as a simplex feeder

**File:** `src/saneless/scanner/sane_backend.py:824-828`

**Issue:** `_resolve_feeder_source` accepts any source where `uses_feeder` is true, and
that includes `FEEDER_DUPLEX`. Two cases go wrong:

- When the operator's `source` is `"ADF Duplex"` (for example, an auto-generated duplex
  profile hand-edited to `duplex = "manual"`), it is used as-is.
  `test_a_reported_feeder_the_operator_named_is_honoured` pins exactly this.
- When `source` is not reported, the first feeder in device order wins, even if it is a
  duplex source listed before the simplex one.

On a duplex source, each pass feeds both sides of every sheet, so each pass returns 2N
pages. The counts agree, `_interleave_duplex` pairs a front+back sequence with a reversed
back+front sequence, and the job reports `DONE` with 4N pages in scrambled order. No
warning is raised. D-02 says to pick a FEEDER or FEEDER_DUPLEX source. It does not say the
two are equally good. Manual duplex on a source that already duplexes is never what the
operator wants.

**Fix:** Prefer `SourceKind.FEEDER`, and use `FEEDER_DUPLEX` only as a last resort with a
WARNING, or refuse it:
```python
kinds = {s: classify_source(s) for s in available_sources}
if kinds.get(requested) is SourceKind.FEEDER:
    return requested
for source, kind in kinds.items():
    if kind is SourceKind.FEEDER:
        return source
duplex_feeders = [s for s, k in kinds.items() if k is SourceKind.FEEDER_DUPLEX]
if duplex_feeders:
    msg = (f"Manual duplex needs a single-sided feeder; this device's feeders "
           f"all scan both sides ({duplex_feeders}). Use duplex = \"hardware\".")
    raise ScanError(msg)
```

### WR-03: Manual duplex regressed on devices that expose no `source` option

**File:** `src/saneless/scanner/sane_backend.py:879-886`

**Issue:** The feeder branch refuses whenever `available_sources` is empty, and that
includes devices with no `source` option at all (`has_source_option is False`). The comment
says such a device "cannot be told to feed". But a sheet-fed scanner with a single source
feeds without being told. On that same device a simplex `source = "ADF"` profile still
works: `effective_source` stays `"ADF"`, `classify_source` returns FEEDER, and
`multi_scan()` runs. Before this phase, `"Manual Duplex"` classified as FEEDER_DUPLEX and
also ran through `multi_scan()`. So manual duplex now fails on hardware where it used to
work, and where simplex feeding on the same device still works. The "no feeder" error is
also wrong for this case: the device reports no source *list*, not "no feeder".

**Fix:** When the device has no source option, trust the classifier on the configured name,
as the simplex path does:
```python
if resolve_feeder:
    if not has_source_option:
        if classify_source(requested).uses_feeder:
            return requested, False
        msg = ("Manual duplex needs a feeder source, and this device exposes no "
               f"source option to choose one; set source to its feeder (got {requested!r})")
        raise ScanError(msg)
    return _resolve_feeder_source(available_sources, requested), True
```
Update `test_a_device_with_no_source_option_is_refused` to match.

### WR-04: `is_bare_default` ignores `duplex`, so a manual-duplex `default` profile is replaced in memory by a generated flatbed profile

**File:** `src/saneless/auto_profiles.py:197-222` (used at `src/saneless/worker.py:209-224`)

**Issue:** Feeder resolution means `source` no longer has to name a feeder. So a config with
only `[profiles.default]` and `duplex = "manual"` is now valid and useful. But
`is_bare_default` compares only `source`, `resolution`, `mode` and `auto_generated`, so it
treats that profile as uncustomized. On the first web job, `_maybe_auto_generate` runs
before the worker looks up the profile. It overwrites `self._settings.profiles["default"]`
with a generated profile (`duplex = "none"`, flatbed source). The operator's first scan
then silently takes a single flatbed snapshot and reports `DONE`, with no flip prompt. The
TOML is not rewritten, because `default` is skipped without `--force`, so the in-memory and
on-disk configs disagree until restart.

**Fix:** Compare the profile as a whole rather than a hand-picked subset, so that no field
added later can repeat this:
```python
return default == ProfileConfig()
```
(or at least add `and default.duplex == bare.duplex`). Add a test with a `duplex = "manual"`
default.

### WR-05: The legacy-source deprecation warning is emitted before logging is configured, so it never reaches `log_file`

**File:** `src/saneless/config.py:258-292`; `src/saneless/cli.py:187-200`

**Issue:** `warn_on_legacy_duplex_source` runs inside `load_settings`. `cli()` calls that
before `configure_logging`, and no other code path configures logging first. At that point
no handlers are attached, so the record goes to Python's `lastResort` handler: bare
message, stderr only. It never reaches the configured rotating `log_file`. D-18 makes this
warning the operator's only migration instruction. An operator who reads the log file, as
the appliance setup expects, never sees it.

**Fix:** Configure logging from raw settings first and then re-emit, or collect the
legacy-profile names during validation and log them after `configure_logging`:
```python
# config.py: expose the detection instead of logging inside the validator
def legacy_manual_duplex_profiles(settings: Settings) -> list[str]: ...
# cli.py, after configure_logging(...):
for name in legacy_manual_duplex_profiles(settings):
    logger.warning(...)
```

### WR-06: The `scan` exit-code table in `cli-commands.md` is broken by a paragraph inserted mid-table

**File:** `docs/reference/cli-commands.md:34-38`

**Issue:** The new manual-duplex paragraph sits between the row for code 2 and the row for
code 3. Markdown ends the table at the blank line, so code 3 ("Paperless-ngx upload error")
renders as a stray `| 3 | ... |` line under the paragraph. The paragraph also reads as if
it belongs to exit codes.

**Fix:** Move the `| 3 | Paperless-ngx upload error ... |` row up to directly after row 2,
and put the paragraph after the table.

### WR-07: `web-api.md` says the web UI cannot send an early answer, and that the buttons are gone before a second click; both are false

**File:** `docs/reference/web-api.md:149-150`

**Issue:** "The web UI cannot do this" and "the buttons are gone before a second click can
land on them" are both contradicted by the CR-01 reproduction. The flip routes return
whatever the store holds straight after signalling, which is still `AWAITING_FLIP`, and
`partials/status.html` renders the Continue and Abort buttons again for that state. The
docs promise a safety property the code does not have.

**Fix:** Fixing CR-01 makes the "call them only while AWAITING_FLIP" caveat unnecessary;
then rewrite both bullets. If CR-01 is deferred, the bullets must at least say that a
repeated click can land on the next manual-duplex job.

### WR-08: An unexpected exception on the CLI prompt thread hangs `saneless scan` for the whole timeout and reports "nobody confirmed"

**File:** `src/saneless/cli.py:126-136`

**Issue:** `_prompt` handles only `click.Abort`. Anything else raised by `click.confirm` or
the read is not handled. For example, `UnicodeDecodeError` on non-UTF-8 terminal input, or
`OSError`/`EIO` on a lost terminal. The thread dies, `threading.excepthook` prints a
traceback, and the main thread keeps waiting for the full `flip_timeout_seconds` (10
minutes by default). It then fails with "nobody confirmed the stack was flipped", which is
false: the operator may have answered, and the prompt broke. The comment calls TIMED_OUT
"the right fallback", but a ten-minute hang followed by a wrong cause is not a good one.

**Fix:** Resolve at once, with a truthful outcome and a log line:
```python
try:
    flipped = click.confirm(_FLIP_PROMPT, default=True)
except click.Abort:
    self._resolve(FlipOutcome.ABORTED)
    return
except Exception:
    logger.exception("Flip prompt failed; treating it as an abort")
    self._resolve(FlipOutcome.ABORTED)
    return
```
(`BLE001` may need the concrete types, such as `OSError` and `UnicodeError`, instead of a
bare `Exception`.)

## Info

### IN-01: Stale "non-None" wording in `PipelineEvent.job_state`

**File:** `src/saneless/pipeline.py:72`
**Issue:** The return type is now `JobState`, never `None`, but the docstring still says
"Note that a non-``None`` result is not an instruction to write that state".
**Fix:** Change it to "Note that the returned state is not an instruction to write it".

### IN-02: The claim-once logic is copied in two coordinators

**File:** `src/saneless/cli.py:93-159`; `src/saneless/worker.py:57-116`
**Issue:** `__init__` (lock, event, outcome) and `_resolve` are the same concurrency code in
both places, with the same comments. A fix to one, such as CR-01's arming, will not reach
the other.
**Fix:** Move the single-answer slot into one private helper, for example a
`_OneShotAnswer` in `pipeline.py` next to `FlipCoordinator`, and have both coordinators use
it.

### IN-03: `continue_flip`/`abort_flip` log "signal sent" even when the answer is dropped

**File:** `src/saneless/worker.py:176, 183`
**Issue:** `_resolve`'s result is ignored, so a late Continue or Abort that was dropped (D-16)
still logs "Manual duplex: continue signal sent". During an incident the log then claims an
action that had no effect.
**Fix:** Have `signal_*` return whether it claimed the answer, and log "dropped (already
answered: X)" when it did not.

### IN-04: A legacy source with an explicit non-manual `duplex` goes to SANE unchanged, with no warning

**File:** `src/saneless/config.py:280-282`
**Issue:** `source = "Manual Duplex"` together with `duplex = "none"` (or `"hardware"`) is
not translated, which is correct because explicit configuration wins. But it is also not
warned about, because the warning requires `duplex == "manual"`. The string then reaches
`_resolve_source`. There it either raises, or on a device offering `Auto` it is swapped for
`Auto` and takes a flatbed snapshot, which is C-01's path.
**Fix:** Warn for every legacy-looking source whatever `duplex` says, and tailor the
message when `duplex` is not `"manual"`.

---

_Reviewed: 2026-09-14T15:09:53Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
