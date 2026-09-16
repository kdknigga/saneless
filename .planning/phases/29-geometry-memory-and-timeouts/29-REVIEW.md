---
phase: 29-geometry-memory-and-timeouts
reviewed: 2026-09-15T00:00:00Z
depth: deep
files_reviewed: 33
files_reviewed_list:
  - src/saneless/spool.py
  - src/saneless/scanner/base.py
  - src/saneless/scanner/sane_backend.py
  - src/saneless/pipeline.py
  - src/saneless/pdf.py
  - src/saneless/pages.py
  - src/saneless/cli.py
  - src/saneless/web/app.py
  - tests/conftest.py
  - tests/fake_sane.py
  - tests/test_spool.py
  - tests/test_scanner.py
  - tests/test_pipeline.py
  - tests/test_pdf.py
  - tests/test_pages.py
  - tests/test_outcomes_e2e.py
  - tests/test_cli.py
  - tests/test_worker.py
  - tests/test_app_lifespan.py
  - tests/test_sane_hardware.py
  - tests/test_deployment_config.py
  - tests/test_web.py
  - tests/test_web_errors.py
  - tests/test_web_state_rendering.py
  - tests/test_cross_origin.py
  - tests/test_browser.py
  - docs/explanation/architecture.md
  - docs/explanation/consume-directory-fallback.md
  - docs/how-to/set-up-adf-duplex.md
  - docs/how-to/troubleshoot-a-failed-scan.md
  - docs/reference/configuration.md
  - docs/reference/docker.md
  - docs/reference/environment-variables.md
findings:
  critical: 2
  warning: 11
  info: 4
  total: 17
status: issues_found
---

# Phase 29: Code Review Report

**Reviewed:** 2026-09-15
**Depth:** deep
**Files Reviewed:** 33
**Status:** issues_found

## Summary

The concurrency centre of this phase — `_acquire_with_timeout`, `_cancel_and_settle`,
`_mark_wedged` / `_release_wedge` / `_refuse_if_wedged`, and the `_INIT` guard — was traced
hop by hop for the interleavings that matter, and it holds up better than expected. The
check-and-write pairs are inside one critical section on both sides, the reader identifies
its own acquisition by `done` so a late wake-up cannot clear a newer wedge, the lock ordering
is consistently `_INIT_LOCK` → `_WEDGE_LOCK` with no reverse edge, and the fear that
`_release_wedge` closes the handle before dropping `_WEDGE.iterator` (running
`_SaneIterator.__del__` → `cancel()` on a closed handle) is safe only because `__del__`
swallows and `_sane` refuses a closed handle. Two claimed hazards were tested and turned out
not to be defects: `pikepdf.Job` does **not** hold every single-page PDF open (600 merged
cleanly under `RLIMIT_NOFILE=1024`), and `ImageOps.contain` always returns a fresh image so
`generate_thumbnail` does not mutate the caller's page. `ruff`, `ty` and `pyrefly` are clean.

What did not hold up is the preservation story and the test isolation of the new
process-global state.

The phase's own headline rule — "a failure keeps everything it can" — has a hole big enough
to lose a whole duplex job: the duplex-mismatch delivery path runs **outside both**
preservation guards, so an assembly failure there deletes every spooled page with the
workspace. Two smaller preservation defects sit beside it: a partial-scan assembly failure
loses the pages that D-10 says the page-file fallback keeps, and both new guards report "the
scan could NOT be preserved" while artefacts they already moved sit in `failed/` — which is
precisely the "actively misinformed" failure `_preserving`'s own docstring warns against.

Separately, the new process-global `_INIT` guard (D-17) leaks out of
`tests/test_pipeline.py::TestManualDuplexOverTheSharedFake` and poisons every later
`SaneBackend()` in the process. `uv run pytest` — the exact command
`.github/workflows/release.yml` runs as the release gate — now fails a test that passes at
`f0610e1`. This was verified by running the suite at both commits.

No security vulnerabilities were found. The qpdf argv is all literals and
self-written scratch paths, `sanitise_title_for_filename` remains an allow-list on both the
title and the job-id segment, and the wedge/refusal messages name only the device id
(no host, no credential).

## Critical Issues

### CR-01: A duplex-mismatch assembly failure destroys every scanned page

**File:** `src/saneless/pipeline.py:1875-1902`, `src/saneless/pipeline.py:1222-1233`

**Issue:** `run_pipeline` closes `_preserving_partial_scan` at line 1881, *before* the
`match acquired:` dispatch. The `_DuplexMismatch` arm at line 1895-1900 therefore calls
`_finish_duplex_mismatch` → `_handle_duplex_mismatch` with **no** preservation guard in
scope at all, and that function's first two statements are `assemble_pdf(fronts, ...)` and
`assemble_pdf(backs, ...)` (lines 1222 and 1228). `_preserving` inside it (line 1245) opens
only around the upload and the poll, and `_preserving_page_files` is never reached because
the mismatch arm returns early.

So: a `PdfError` from either of those two `assemble_pdf` calls — a full disk, a page file
Pillow can no longer read, a qpdf refusal — propagates out of `run_pipeline`, the workspace
`TemporaryDirectory` unwinds, and **every spooled page of both passes is deleted**. Nothing
lands in `failed/`.

This is the worst path to lose, not the least likely one: a duplex mismatch is already an
anomaly the whole recovery exists to hand a human, and `_handle_duplex_mismatch`'s own
docstring calls it "the one most likely to be holding a document the user actually needs".

It also makes two statements untrue that this phase shipped:

- D-10: "A `PdfError` during assembly … the spooled page files themselves move into a
  `failed/<job-keyed-name>/` directory named in the error."
- `docs/explanation/architecture.md`: "**A failure to assemble the PDF at all** keeps the
  spooled page files themselves, moved into a job-keyed directory under `failed/`, named in
  the error." — stated without qualification.

There is no test for it. `tests/test_pipeline.py` covers the mismatch path only for *upload*
failures (`test_duplex_mismatch_preserves_both_halves_when_the_fronts_fail` at :3943,
`…_when_the_backs_fail` at :3975); neither exercises an assembly failure.

**Fix:** put the mismatch delivery inside the page-file guard, so the one failure mode that
has no PDF to preserve still keeps the pages. In `run_pipeline`, hoist the page-file guard
above the dispatch and let both arms share it:

```python
        page_dir = settings.output.failed_dir / Path(pdf_filename).stem

        match acquired:
            case ScanBatch():
                batch = acquired
            case _DuplexMismatch():
                with _preserving_page_files(spool_dir, page_dir):
                    return _finish_duplex_mismatch(
                        acquired, tmp_path, paperless, request, settings
                    )
            case _:
                assert_never(acquired)
```

`pdf_filename` is composed at line 1927 today, below the dispatch; it has to move above it
(it already carries the "composed once, not twice" comment, so the move is free). Guard
against double-preservation: `_preserving_page_files` returns untouched when `spool_dir` is
empty, and a successful mismatch delivery leaves the spool intact, so wrapping the upload
half too is harmless — but if that is unwanted, wrap only the two `assemble_pdf` calls
inside `_handle_duplex_mismatch`. Add a test that makes `assemble_pdf` raise on the
`(fronts)` half of a mismatched job and asserts a `failed/<job>/` directory holding every
`a-*.png` and `b-*.png`.

### CR-02: The new process-global SANE init guard leaks between test modules and breaks the release gate

**File:** `tests/test_pipeline.py:1822-1945`, `src/saneless/scanner/sane_backend.py:821-895`

**Issue:** `TestManualDuplexOverTheSharedFake` monkeypatches `sane_backend_mod.sane` with a
`FakeSaneModule` and constructs a real `SaneBackend()` (lines 1891 and 1942). That sets the
module-level `_INIT.done = True`. Unlike `tests/test_scanner.py`, this module has **no**
`sane_init_guard` fixture, so `_INIT.done` stays `True` for the rest of the process after
`monkeypatch` restores the `sane` name.

Every later `SaneBackend()` in that process then takes `_ensure_initialised`'s early return
at line 860 and **never calls the real `sane.init()`**, while `require_sane()` has meanwhile
re-imported the genuine module. The backend proceeds to make real SANE calls against a
library that was never initialised — in `tests/test_sane_hardware.py` that surfaces as an
empty device list.

Reproduced deterministically:

```
$ uv run pytest -q -p no:randomly \
    "tests/test_pipeline.py::TestManualDuplexOverTheSharedFake" tests/test_sane_hardware.py
E       AssertionError: assert 'test:0' in []
tests/test_sane_hardware.py:132
1 failed, 7 passed
```

The same pair passes at `f0610e1`, because before D-17 each `SaneBackend()` called
`sane.init()` itself. This is phase-introduced.

Consequence beyond the test: `uv run pytest` — the whole suite in one process — is exactly
what `.github/workflows/release.yml:16` runs as the gate in front of `publish-pypi` and
`publish-docker`. At HEAD that command reports `3 failed, 2104 passed`; at `f0610e1` it
reports `2 failed, 2001 passed` (the two are pre-existing, see WR-08 and IN-04). CI's
`ci.yml` splits the suite by marker into three processes and therefore hides this
completely.

The production-side lesson is worth recording too: the guard records "initialised" without
any way to notice that the `sane` it initialised is not the `sane` in effect. In production
the module is never swapped, so this is a test defect — but it is the guard's design that
makes a swapped module silently skip the real init.

**Fix:** promote the reset to a shared autouse fixture so no test module can leave
`_INIT` set. In `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _sane_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts and ends with an uninitialised, unwedged SANE (D-17, D-13)."""
    _ = monkeypatch  # ordering: tear down before any sane-module patch is undone
    sane_backend_mod.shutdown()
    yield
    sane_backend_mod.shutdown()
```

and delete the now-redundant `sane_init_guard` from `tests/test_scanner.py:462`. Note
`shutdown()` returns early without resetting `_INIT` when `_read_outstanding()` is true
(`sane_backend.py:946-952`), so the fixture must also clear a leaked wedge — reuse the
teardown `TestSaneBackendCancelSequence._clear_wedge` already performs
(`tests/test_scanner.py:2060-2072`) rather than leaving that class-local.

Add a regression test that asserts `sane_backend_mod._INIT.done is False` after a module
that builds a `SaneBackend` over the fake, or — better — make
`tests/test_sane_hardware.py::test_enumeration_lists_the_test_device` fail loudly on a
pre-set guard rather than on an empty list.

## Warnings

### WR-01: A raising thumbnail callback orphans page 1 — written to the spool but absent from `records`

**File:** `src/saneless/spool.py:147-182`

**Issue:** `add()` writes the PNG, builds the `PageRecord`, fires the thumbnail callback at
lines 165-169, and only *then* appends the record at line 171. The deliberate decision not
to wrap the callback (recorded in the comment, and accepted) is not the problem; the
ordering is. If the callback raises:

- `a-0001.png` exists on disk but `sink.records` is empty;
- `_SpoolLedger.page_count()` returns 0, so `_preserving_partial_scan`
  (`pipeline.py:817-821`) takes the "nothing reached the spool" branch and re-raises with no
  preservation — while a real scanned sheet sits in the workspace about to be deleted;
- the exception is not a `SanelessError`, so it escapes `run_pipeline` raw and the worker
  classifies it as an UNKNOWN error ("saneless bug") rather than a scan failure.

This is reachable: the web worker's callback is
`self._job_store.update_thumbnail(_jid, thumb)` (`src/saneless/worker.py:1289-1290`), which
raises `sqlite3.Error` on a locked or closed database. `generate_thumbnail` itself raises
`OSError` for any mode JPEG cannot encode.

`tests/test_spool.py::TestSpooledPageSinkThumbnail` has no test for a callback that raises.

**Fix:** append before firing, so a failed thumbnail cannot unsee a page that is already on
disk:

```python
        self._records.append(record)

        if sequence == 1 and self._thumbnail_callback is not None:
            # Not wrapped in a suppression: a thumbnail is a visible part of
            # what the operator sees, so a failure here should surface rather
            # than leave the strip silently blank.  The record is appended
            # first so a raise here still leaves the page preservable.
            self._thumbnail_callback(generate_thumbnail(image))
```

Add `test_a_raising_thumbnail_callback_still_records_the_page`.

### WR-02: Preservation reports total failure after partial success, hiding files it did move

**File:** `src/saneless/pipeline.py:822-830`, `src/saneless/pipeline.py:890-918`

**Issue:** `_preserve_partial_passes` (`:730-742`) returns `destinations` only on complete
success. If the `(fronts)` half moves into `failed/` and the `(backs)` half then fails to
assemble, the exception propagates and `_preserving_partial_scan`'s handler at line 826-830
reports:

```
… The scan could NOT be preserved to /var/lib/saneless/failed: <second failure>
```

while the fronts PDF is sitting in that very directory. `_preserving_page_files` has the
same shape: it accumulates into `moved` (line 891, 901) and its `except OSError` handler at
lines 905-909 never mentions it, so a move that fails on page 7 of 12 tells the operator
nothing was kept when seven PNGs were.

This is the exact failure mode `_preserving`'s own docstring calls out — "a user told only
that the upload failed while the scan was also destroyed has been actively misinformed" —
inverted: told nothing was saved when some of it was. An operator who believes the message
rescans and never looks in `failed/`, and saneless never prunes it.

**Fix:** have both handlers name what did survive. Give `_preserve_partial_passes` an
out-parameter (or accumulate into a caller-owned list) so the handler can read it, then:

```python
        except (OSError, PdfError) as keep_exc:
            kept = (
                f" Only {', '.join(str(d) for d in destinations)} was kept."
                if destinations
                else ""
            )
            msg = (
                f"{exc}. The scan could NOT be fully preserved to {failed_dir}: "
                f"{keep_exc}.{kept}"
            )
```

and the mirror change in `_preserving_page_files`, where `moved` is already in scope.

### WR-03: A partial-scan assembly failure loses the very pages D-10's page-file fallback exists to keep

**File:** `src/saneless/pipeline.py:822-830`, `src/saneless/pipeline.py:692-742`

**Issue:** D-10 wires the page-file fallback ("the spooled page files themselves move into a
`failed/<job-keyed-name>/` directory") only around `run_pipeline`'s main `assemble_pdf`
(line 1933-1941). When the *partial* assembly inside `_preserve_partial_passes` raises
`PdfError`, `_preserving_partial_scan` catches it at line 826, reports both failures, and
lets the workspace unwind — so the N pages that the whole guard exists to keep are deleted.

The realistic trigger is the same one that caused the partial in the first place: D-07's
per-page disk check refuses a page because the disk is full, `_preserving_partial_scan`
fires, `assemble_pdf` then also cannot write, and every page is lost at exactly the moment
the operator most needs them.

**Fix:** fall back to moving the page files when the partial PDF cannot be built, inside
`_preserving_partial_scan`'s `(OSError, PdfError)` handler — the same treatment CR-01 asks
for on the mismatch path. Both call sites want one helper; consider making
`_preserving_page_files`'s body a plain function that both guards call.

### WR-04: `get_devices()` is the one SANE entry point not covered by the wedge refusal, and the scan path reaches it

**File:** `src/saneless/scanner/sane_backend.py:1979-2003`, `src/saneless/pipeline.py:1739`

**Issue:** `scan_pages` (:2127) and `get_capabilities` (:2026) both open with
`_refuse_if_wedged`. `get_devices` does not, so it calls `sane.get_devices()` while a reader
thread is still inside `sane_read` on an open handle. The phase's own rule, quoted verbatim
in `29-CONTEXT.md`, is "Never call another SANE operation while one is outstanding".

This is not a hypothetical path: `pipeline._resolve_device` calls
`scanner.get_devices()` whenever `settings.scanner.device` is empty — the documented
auto-detection default — and it does so *before* `scan_pages`, i.e. before the refusal that
would otherwise stop the job. On the `net` backend `sane_get_devices` is an RPC on the same
control wire the stuck read is on.

**Fix:** guard it too. `_refuse_if_wedged` already takes the device name for its message;
pass an empty name or add a no-device overload:

```python
    def get_devices(self) -> list[DeviceInfo]:
        _refuse_if_wedged("the scanner", "list the scanners on")
        try:
            raw_devices = sane.get_devices()
        ...
```

If enumeration is deliberately considered safe while a handle is wedged, say so in the
docstring and in D-13's wording, because the current asymmetry reads as an omission.

### WR-05: `_acquire_with_timeout`'s interrupt guard covers `reader.start()`, creating a wedge that can never clear

**File:** `src/saneless/scanner/sane_backend.py:1298-1306`

**Issue:**

```python
    try:
        reader.start()
        finished = done.wait(timeout)
    except KeyboardInterrupt:
        _settle_or_wedge(dev, done, grace, page_label)
        raise
```

The docstring claims "`reader.start()` is inside the guarded block so there is no window in
which the interrupt could land after the thread exists but before the handler could cancel
it." It creates the opposite window instead. If `KeyboardInterrupt` arrives before
`_thread.start_new_thread` returns, the reader never runs, so `done` is never set. The
handler then:

1. fires `dev.cancel()` on a handle with no read in progress,
2. blocks for the full `grace` — 10 s by default — on an event nothing will ever set,
3. `_mark_wedged` succeeds, and because `_release_wedge` is only ever called from the
   reader's `finally` (line 1296), **the wedge can never be cleared**: every subsequent
   `scan_pages` / `get_capabilities` refuses and `shutdown()` permanently skips
   `sane.exit()`.

The window is a handful of bytecodes and `KeyboardInterrupt` reaches only the main thread,
so the web worker is unaffected and a CLI process is exiting anyway. It is narrow, not
impossible — and the docstring asserting safety here is worse than the bug.

**Fix:** move `reader.start()` out of the guarded block (the thread cannot be interrupted
before it exists, which is the case the current shape mishandles), or make the handler
tolerate a reader that never started:

```python
    reader.start()
    try:
        finished = done.wait(timeout)
    except KeyboardInterrupt:
        if reader.is_alive():
            _settle_or_wedge(dev, done, grace, page_label)
        raise
```

and correct the docstring paragraph either way.

### WR-06: A preserved page directory loses the document order of a manual-duplex job, and the docs claim it keeps it

**File:** `src/saneless/pipeline.py:893-901`, `docs/explanation/consume-directory-fallback.md:68`

**Issue:** `_preserving_page_files` moves `sorted(spool_dir.iterdir())` into
`failed/<job>/`, which yields `a-0001 … a-000N, b-0001 … b-000N`. For an interleaved manual
duplex job the document order is `a-0001, b-000N, a-0002, b-000N-1, …` — the reversal this
phase's own `_interleave_duplex` docstring stresses "no longer sort into document order at
all" (`pipeline.py:1366-1369`). Nothing in the preserved directory records which convention
applies, and the record list that *was* the proof of order is discarded with the workspace.

The new documentation states the opposite as fact:

> Each is named after the job and holds one PNG per sheet, **in scan order**.
> — `docs/explanation/consume-directory-fallback.md:68`

`docs/reference/docker.md:73` tells the operator to "assemble or rescan it", which for a
duplex job they cannot do correctly from the names alone.

**Fix:** either (a) write a small manifest beside the pages — one line per record, in
document order — when the job is duplex, or (b) rename the moved files to their document
position (`0001.png`, `0002.png`, …) using the record list, which is still in scope at the
`run_pipeline` call site. Failing both, correct the two doc sentences to say the files are
in *per-pass acquisition* order and state the interleave rule.

### WR-07: A worker test writes a preserved partial PDF into the shared `/tmp/saneless-test/data/failed/`

**File:** `tests/test_worker.py:1910-1960`

**Issue:** `test_a_pass_a_failure_after_stop_claimed_the_flip_stays_a_failure` uses
`default_settings` unmodified, so `tmp_dir` and `data_dir` are `conftest.py`'s process-wide
`/tmp/saneless-test` and `/tmp/saneless-test/data`. With this phase's partial preservation
the pass-A jam now preserves the fronts, and a real PDF is written outside pytest's
`tmp_path`, where nothing ever removes it.

Measured — one file per suite run, accumulating across runs:

```
/tmp/saneless-test/data/failed/20260916-025508-e3533df2-jammed-in-pass-a-fronts.pdf
/tmp/saneless-test/data/failed/20260916-025747-b486f067-jammed-in-pass-a-fronts.pdf
/tmp/saneless-test/data/failed/20260916-025846-ec8f6e9a-jammed-in-pass-a-fronts.pdf
```

At `f0610e1` the same run leaves only the pre-existing `…-test.pdf`, so this leak is
phase-introduced. Beyond untidiness it is a live flake source: once 20 artefacts accumulate,
`_warn_if_failed_dir_growing` starts emitting its WARNING inside unrelated tests that assert
on captured log records.

`tests/test_pipeline.py` already has the right pattern — `_isolate_dirs` (:2320-2340) and the
explicit comment at :125-129. This test never got it.

**Fix:** redirect both directories, as the pipeline tests do:

```python
        settings = _manual_duplex_settings(default_settings)
        settings.output.tmp_dir = str(tmp_path)
        settings.output.data_dir = str(tmp_path / "state")
```

Then add a session-scoped autouse guard in `tests/conftest.py` that fails the run if
`Path(_TEST_DATA, "failed")` gained a file — the same shape as
`_suite_leaves_cwd_config_alone` — so the next such leak is caught at the source rather than
found by hand.

### WR-08: `test_cross_origin.py` calls `asyncio.run()` on a loop Playwright left running

**File:** `tests/test_cross_origin.py:459`

**Issue:** `test_rejection_log_escapes_control_characters_in_the_path` calls
`asyncio.run(guard(scope, receive, send))` on the main thread. After the browser tests have
run in the same process, a loop is still running there and `asyncio.run` refuses:

```
$ uv run pytest -q -p no:randomly tests/test_browser.py tests/test_cross_origin.py
E   RuntimeError: asyncio.run() cannot be called from a running event loop
1 failed, 108 passed
```

`tests/test_web_errors.py:458-465` already solved exactly this, with the comment "a
Playwright session earlier in the same run can leave an event loop running on this one,
where `asyncio.run` refuses", and routes the call through a `ThreadPoolExecutor`.
`test_cross_origin.py` — which this phase touched, for the `StubScannerBackend` migration —
did not get the same treatment.

This failure is **pre-existing** (it reproduces at `f0610e1`), but it is one of the three
that make `uv run pytest`, the release gate, red. It is cheap to fix while CR-02 is being
fixed.

**Fix:** copy the sibling's workaround:

```python
    with (
        caplog.at_level(logging.WARNING, logger=GUARD_LOGGER),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        pool.submit(lambda: asyncio.run(guard(scope, receive, send))).result()
```

### WR-09: `build_pdf_filename`'s uniqueness argument does not hold for the CLI, and both docstrings say it does

**File:** `src/saneless/cli.py:564-573`, `src/saneless/pipeline.py:332-345`, `src/saneless/pdf.py:106-121`

**Issue:** `PipelineRequest.job_id` is documented as: "The worker always supplies `job.id`;
the empty default exists only so the dozens of tests that construct a request from a profile
name and a title need not invent one." That is not true — `saneless scan` constructs its
`PipelineRequest` at `cli.py:564-573` without `job_id`, so every CLI run composes
`{timestamp}-{title-slug}.pdf` with no job segment at all.

`build_pdf_filename`'s docstring then rests the whole collision argument on a segment the
CLI never supplies:

> **Uniqueness comes from the job id, not from the timestamp.** … a collision is not cosmetic
> here: `shutil.move` onto an explicit destination path overwrites silently, so two
> same-named PDFs preserved into `failed/` would destroy one scan.

Same-second collisions are unlikely for a scan, but this phase multiplied the artefacts
written into `failed/` from one (a delivery failure) to four kinds (complete PDF, `(partial)`,
`(fronts)`/`(backs)`, page directory) and made them reachable from every mid-scan fault, so
the guarantee is doing more work than it was.

Pre-existing, but the two docstrings are now load-bearing for three new paths.

**Fix:** give the CLI a job id — a `uuid4()` at the top of `scan()` costs nothing and makes
both docstrings true — or correct both docstrings to say the CLI relies on the timestamp
alone, and say what happens when two CLI scans of the same title land in the same second.

### WR-10: `_snap_flatbed` cannot be given a grace, so the flatbed unresponsive-cancel path is untestable without a 10 s wait

**File:** `src/saneless/scanner/sane_backend.py:1757-1763`, `src/saneless/scanner/sane_backend.py:1812`

**Issue:** `_acquire_pages` takes `grace` and threads it through
(`sane_backend.py:1328, 1417`), and its docstring explains why: "Injectable for the same
reason `timeout_per_page` is: a test proving the unresponsive-cancel path must not wait out
the module's real ten seconds." `_snap_flatbed` takes `timeout` but not `grace`, and its
call at line 1812 omits the argument, so the flatbed path always uses the real
`_CANCEL_GRACE_SECONDS = 10.0`.

D-14's whole claim is that "one sheet is one sheet, whichever way it was presented", and
`tests/test_scanner.py::test_flatbed_timeout_matches_the_adf_message_shape` asserts the
timeout default matches — but there is no flatbed equivalent of
`test_did_not_respond_to_cancel`, and there cannot be a fast one while `grace` is not
injectable.

**Fix:** add the parameter and forward it, matching `_acquire_pages`:

```python
def _snap_flatbed(
    dev: SaneDevice,
    device_id: str,
    sink: PageSink,
    crop: Callable[[Image.Image], Image.Image],
    timeout: float = _DEFAULT_PAGE_TIMEOUT_SECONDS,
    grace: float = _CANCEL_GRACE_SECONDS,
) -> PageRecord:
    ...
    image = _acquire_with_timeout(dev, start_and_snap, _page_label(0), timeout, grace)
```

Then mirror `test_did_not_respond_to_cancel` for the flatbed, which is what actually proves
HARD-04's "same path" claim.

### WR-11: A raw `OSError` can still escape `SpooledPageSink.add`, which D-07 forbids

**File:** `src/saneless/spool.py:210`

**Issue:** `add()`'s docstring promises "No raw OSError escapes this method (D-07)", and
`_write` honours it. `_check_room_for` does not: `shutil.disk_usage(self._directory)` at
line 210 sits outside any handler. A spool directory that has been removed or whose mount
went away raises `FileNotFoundError` / `OSError` straight out of `scan_pages`, past
`_acquire_pages`' `except ScanError` ladder, into
`_acquire_pages`' generic `except Exception` — where it *is* wrapped, but as
`"Scanner error on page N"`, blaming the scanner for a disk fault, and only on the ADF path.
On the flatbed path `_snap_flatbed`'s `sink.add` call at line 1842 is outside its
`try`, so the raw `OSError` escapes untranslated.

`tests/test_spool.py::test_write_oserror_becomes_a_chained_scan_error` covers the write but
not the check.

**Fix:** wrap the measurement in the same translation the write uses:

```python
        try:
            free_mb = shutil.disk_usage(self._directory).free // _BYTES_PER_MB
        except OSError as exc:
            msg = (
                f"Could not measure free space for page {sequence} in "
                f"{self._directory}: {describe(exc)}"
            )
            raise ScanError(msg) from exc
```

## Info

### IN-01: `_release_wedge` closes the handle before dropping the iterator reference

**File:** `src/saneless/scanner/sane_backend.py:1077-1086`

**Issue:** `dev.close()` runs at line 1078 and `_WEDGE.iterator = None` at line 1084. If the
wedge record holds the last reference, that assignment runs `_SaneIterator.__del__`, which
calls `device.cancel()` on a handle that was closed six lines earlier. It is safe today only
because `_sane.SaneDev.cancel` refuses a closed handle and `__del__` swallows the exception
(`sane.py:118-123`) — a two-library coincidence, in a module whose `_retain_iterator`
docstring goes out of its way to explain that this exact `__del__` is a hazard.

**Fix:** drop the iterator reference first, then close, so the ordering is deliberate rather
than survivable:

```python
        _WEDGE.iterator = None
        try:
            dev.close()
        except Exception:
            logger.warning("Could not close the released scanner", exc_info=True)
```

### IN-02: The settle race can produce a timeout message that contradicts what happened

**File:** `src/saneless/scanner/sane_backend.py:1209-1218`, `src/saneless/scanner/sane_backend.py:1959-1977`

**Issue:** If the reader sets `done` in the instant after `_mark_wedged` wins the lock, the
reader's own `_release_wedge` closes the handle and clears the record, but
`_settle_or_wedge` has already returned `False`. The caller then raises
`"… the scanner did not respond to the cancel, so saneless is still waiting for that read to
return"` when the read did return and the handle *was* closed, and `_open_device`'s `finally`
takes the not-wedged branch and calls `cancel()`/`close()` on the already-closed handle,
logging a spurious `Could not close scanner …` WARNING. State is correct; only the two
messages lie. The window is a few instructions wide.

**Fix:** have `_settle_or_wedge` re-check `done.is_set()` before reporting "did not respond",
or have `_mark_wedged` return a tristate so the caller can distinguish "wedged" from "the
reader beat us after we marked".

### IN-03: `docs/reference/docker.md` calls a compressed size "uncompressed"

**File:** `docs/reference/docker.md:70-72`

**Issue:** "Those PNGs are uncompressed-document-scale: roughly 13 MB per A4 300 DPI colour
page." 13.7 MB is the figure `spool.py`'s module docstring measured for a PNG at compression
level 6 — i.e. compressed. The uncompressed figure for that page is ~26 MB
(`sane_backend.py:159`). The hyphenated line break also renders as the odd compound
"uncompressed-document-scale".

**Fix:** "Those PNGs are document-scale even after compression: roughly 13 MB per A4 300 DPI
colour page."

### IN-04: `test_paperless.py` poll-deadline parametrisation is an order-dependent flake

**File:** `tests/test_paperless.py` (not in this phase's scope)

**Issue:** `TestPollTaskFailureTranslation::test_poll_deadline_after_transport_errors_names_the_last_error`
fails on a different parameter each full-suite run (`[connect-error]` at HEAD,
`[remote-protocol-error]` at `f0610e1`), and passes when the module runs alone. Pre-existing
and outside this phase, recorded only so it is not mistaken for CR-02's fallout when the
release gate is next looked at.

---

_Reviewed: 2026-09-15_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
