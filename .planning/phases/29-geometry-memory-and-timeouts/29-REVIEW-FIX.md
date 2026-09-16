---
phase: 29-geometry-memory-and-timeouts
fixed_at: 2026-09-15
review_path: .planning/phases/29-geometry-memory-and-timeouts/29-REVIEW.md
iteration: 1
findings_in_scope: 13
fixed: 13
skipped: 0
deferred_out_of_scope: 4
status: all_fixed
---

# Phase 29: Code Review Fix Report

**Source review:** `.planning/phases/29-geometry-memory-and-timeouts/29-REVIEW.md`
**Scope:** both BLOCKERs and all 11 WARNINGs. The 4 INFO findings were out of scope.

**Summary:**

- Findings in scope: 13 (CR-01, CR-02, WR-01..WR-11)
- Fixed: 13
- Skipped: 0
- Not attempted (out of scope): IN-01, IN-02, IN-03, IN-04

One commit per finding, thirteen in all, each one hooks-clean:

| Commit | Finding |
|---|---|
| `7a30a58` | CR-02 reset the process-global SANE guard around every test |
| `caefda0` | CR-01 keep the spooled pages when a duplex mismatch cannot assemble |
| `c479aad` | WR-01 record a spooled page before the thumbnail callback can raise |
| `ba0a340` | WR-02 name what a half-finished preservation did keep |
| `478dc3c` | WR-03 keep the page files when the partial PDF cannot be built |
| `3f34d14` | WR-04 refuse `get_devices()` while a read is still outstanding |
| `69579d8` | WR-05 do not run the cancel tail when the reader never started |
| `c7450b2` | WR-06 stop claiming a preserved page directory is in document order |
| `f962ffe` | WR-07 stop two tests preserving into the suite's shared `failed/` |
| `4156abe` | WR-08 run the cross-origin guard off the main thread's event loop |
| `0cacfbc` | WR-09 give the CLI a job id, so both docstrings become true |
| `090c35d` | WR-10 make the flatbed cancel grace injectable |
| `62a7a43` | WR-11 translate an `OSError` from the spool's free-space measurement |

Every finding was independently confirmed in the source before being fixed. None
turned out to be an over-call; the reviewer was right on all thirteen.

---

## Fixed Issues

### CR-02: the process-global SANE init guard leaked between test modules — `7a30a58`

**Files:** `tests/conftest.py`, `tests/test_scanner.py`, `tests/test_sane_hardware.py`

The reset moved out of `tests/test_scanner.py`'s module-local `sane_init_guard`
and into a suite-wide autouse fixture, `conftest.sane_process_state`, so no test
module can leak the guard by forgetting to add one. That is the "cannot silently
regress" shape the brief asked for: a module-local fixture only ever fixes the
module that remembers it.

`reset_sane_process_state()` also finishes off the one case `shutdown()`
documents as a refusal — a read still recorded as outstanding leaves the guard
armed, because `sane_exit()` closes every open handle — and it does so
**without** calling `sane_exit()`, which would be unsafe for exactly the reason
`shutdown()` declined to. (The wedge is cleared and `_INIT` re-armed by hand in
that branch only.)

Ordering: the fixture requests `monkeypatch` and does not use it. `monkeypatch`
is a single instance per test, so any fixture depending on it is torn down
before its `undo` runs — the final reset therefore still finds the fake `sane`
module in place, not the real library the undo restores. This is the same
ordering trick the deleted `sane_init_guard` used, and its docstring is carried
over.

`tests/test_scanner.py`'s old fixture is deleted, so the two do not fight.

Two new tests pin both branches of the reset (`TestTheSuiteResetsTheProcessGlobalSaneState`),
and `test_enumeration_lists_the_test_device` now asserts the guard is clear
*before* it constructs a backend — the reviewer's preferred option, so a future
leak fails by naming its cause instead of by an unexplained empty device list.

**Verification:** the deterministic repro
`uv run pytest -q -p no:randomly "tests/test_pipeline.py::TestManualDuplexOverTheSharedFake" tests/test_sane_hardware.py`
went from `1 failed, 7 passed` to `8 passed`. The full single-process suite went
from 3 failed / 2104 passed to 2 failed / 2107 passed at this commit.

### CR-01: a duplex-mismatch assembly failure destroyed every scanned page — `caefda0`

**Files:** `src/saneless/pipeline.py`, `tests/test_pipeline.py`

`_preserving_page_files` now wraps the two `assemble_pdf` calls inside
`_handle_duplex_mismatch`, and stops there.

This is the narrow variant the review offered as its alternative, chosen over
hoisting the guard above `run_pipeline`'s `match` because of the brief's
constraint that the same halves must not be preserved twice. Hoisting would put
the upload inside the page-file guard as well, so a `PaperlessError` would file
the two passes once as `(fronts)`/`(backs)` PDFs (via `_preserving`, D-08) and
again as the page files they were built from. D-08's "one guard over both
halves" for the upload path is untouched.

`run_pipeline`'s comment at the dispatch claimed the mismatch path "carries its
own `_preserving` guard" — true, but not of assembly; it now says which guard
covers what. `_handle_duplex_mismatch`'s `Raises:` gained the `PdfError` case.

Three tests, in `TestDuplexMismatchDelivery`:

- fronts half fails → one `failed/<job>/` holding `a-0001..a-0003` and
  `b-0001..b-0002`, message names the count and the directory, type stays `PdfError`;
- backs half fails → a guard around only the first call would pass the test
  above, so the failure is moved to the second;
- upload failure → positive assertion that `_preserved_page_dirs(failed_dir)`
  is still empty, which is what stops a future "tidy-up" from nesting the guards.

**Falsified:** with `src/saneless/pipeline.py` reverted, the first two fail
(`assert 0 == 1`) and the third passes, which is exactly what each is for.

### WR-01: a raising thumbnail callback orphaned page 1 — `c479aad`

**Files:** `src/saneless/spool.py`, `tests/test_spool.py`

`self._records.append(record)` moved above the callback. The callback stays
unwrapped — that decision was deliberate and is preserved — but a failure in it
can no longer unsee a page that is already on disk. The comment explains the
ordering is load-bearing, and names the two real triggers (`sqlite3.Error` from
the worker's `update_thumbnail`, `OSError` from `generate_thumbnail`).

`test_a_raising_thumbnail_callback_still_records_the_page` asserts the record
*and* the file, because what is being pinned is that the two agree. It fails at
the parent commit with `assert 0 == 1`.

### WR-02: preservation reported total failure after partial success — `ba0a340`

**Files:** `src/saneless/pipeline.py`, `tests/test_pipeline.py`

`_preservation_failure_message(exc, failure, destination, kept)` is now the one
place the sentence is built, and all **three** guards use it — the two the
review named plus `_preserving` itself, which has the identical accumulate-then-
disown shape (fronts PDF moved, backs move fails). Leaving one of the three
inconsistent would have been strange in a fix whose whole subject is uniform
honesty.

The nothing-survived wording is **byte-identical** to what it was, because that
sentence was true; only the half-way case gains
`could NOT be FULLY preserved … Only <paths> was kept`. That keeps the phase's
own recorded message shapes (29-09-SUMMARY) valid for the case they describe.

`_preserve_partial_passes` takes the destination list as an out-parameter: a
return value the caller never received cannot tell it what landed.

Three tests (`TestPreservationNamesWhatItDidKeep`): the half-preserved partial
scan, the half-moved page directory, and a positive assertion that a total loss
still reads exactly as it did — without which the new wording could quietly
widen over every preservation failure. The first two fail at the parent commit.

### WR-03: a partial-scan assembly failure lost the pages D-10 exists to keep — `478dc3c`

**Files:** `src/saneless/pipeline.py`, `tests/test_pipeline.py`

`_move_page_files(spool_dir, destination, moved)` is now a plain function both
guards call, as the review suggested, and
`_preserve_page_files_after_partial_failure` is the last resort
`_preserving_partial_scan`'s `(OSError, PdfError)` handler falls back to.

It never raises: it reports on a failure the caller is already raising, so its
own failure is logged and folded into "nothing extra survived" rather than
replacing the error the operator needs. `_spool_dir_of(tmp_path)` composes the
spool path once for the three places that now need it.

**One deliberate redundancy, recorded in the docstring:** a pass that *did*
assemble is preserved twice, once as its partial PDF and again as its page
files. On a double-failure path that is the right trade — sorting out one
redundant copy is a minute's work, and the alternative is deciding which half of
an already-broken job to throw away.

Three tests: simplex, duplex (both passes' `a-`/`b-` files land in one
directory), and a positive assertion that **a cancel still keeps nothing**
(D-10) — the fallback sits below the `ScanCancelledError` re-raise, and that
test is what keeps it there. The first two fail at the parent commit.

### WR-04: `get_devices()` was outside the wedge refusal — `3f34d14`

**Files:** `src/saneless/scanner/sane_backend.py`, `tests/test_scanner.py`, `tests/fake_sane.py`

`_refuse_if_wedged("the scanners", "list")` now opens `get_devices`. The
refusal names "the scanners" as its subject because enumeration is the call that
finds out which devices exist; `_refuse_if_wedged` names the *wedged* device
from its own record, so the message still says which scanner is holding things
up. D-13's wording did not need amending: it says the next call refuses and that
no SANE call is made, and this is a superset.

The existing D-13 refusal test now covers all three entry points rather than
two, and `FakeSaneModule` counts its enumerations so "no SANE call was made" is
asserted rather than inferred. It fails at the parent commit with
`DID NOT RAISE`.

### WR-05: the interrupt guard created a wedge that could never clear — `69579d8`

**Files:** `src/saneless/scanner/sane_backend.py`, `tests/test_scanner.py`

The review's suggested fix (move `reader.start()` out of the `try`) was **not**
taken as written: it reinstates precisely the window 29-07 deliberately closed,
where an interrupt landing after the thread exists abandons it with no cancel
ever fired. `reader.start()` stays inside the guarded block; the handler instead
asks whether there *is* a reader before running the D-12 tail.

`started` and `reader.is_alive()` answer different halves of that window — the
flag for an interrupt after `start()` returned, the liveness check for one that
landed inside it after the thread had reached the OS. The docstring paragraph
that asserted the old (false) safety property is rewritten to describe what the
code actually does.

`test_an_interrupt_before_the_reader_starts_wedges_nothing` interrupts **only**
the reader thread, so the cancel thread would still start if the handler reached
for it — which makes `cancel_calls == 0` a real assertion and not one the stub
satisfies by accident. At the parent commit it fails with `cancel_calls == 1`,
after burning the whole injected 5 s grace and logging both CRITICAL lines of a
permanent wedge.

### WR-06: the docs claimed a preserved page directory keeps document order — `c7450b2`

**Files:** `docs/explanation/consume-directory-fallback.md`,
`docs/reference/docker.md`, `docs/how-to/troubleshoot-a-failed-scan.md`

Option (c) — correct the documentation — was chosen over (a) a manifest and
(b) renaming to document position. The reasoning, recorded in the commit
message:

- **(b) renaming** would destroy the `a-`/`b-` pass labels D-02 chose for
  debuggability, and has no answer at all for the WR-03 fallback path, where an
  interrupted job's document order is not known.
- **(a) a manifest** needs the record list at three call sites, one of which is
  that same partial-scan fallback with no complete order to write down.
- Neither is needed to make the documentation true, and the names already carry
  enough information to reconstruct the order once the rule is stated.

All three sentences now state the acquisition-order rule, say that simplex and
document order coincide while manual duplex does not, and spell out the
interleave (`a-0001, b-000N, a-0002, b-000N-1, …`) so the directory can be
assembled by hand. `mkdocs build --strict` is clean, including the new
cross-reference anchors.

A manifest remains the better long-term fix and is noted as such, but it is a
feature, not a correction.

### WR-07: tests preserving into the shared `/tmp/saneless-test/data/failed/` — `f962ffe`

**Files:** `tests/test_worker.py`, `tests/test_cli.py`, `tests/conftest.py`

The phase-introduced leak
(`test_a_pass_a_failure_after_stop_claimed_the_flip_stays_a_failure`) was fixed
by repointing `tmp_dir` and `data_dir`, as instructed — not by deleting the
assertion. Both leaking tests additionally gained a **positive** assertion that
the artefact landed in their own directory, so they pin more than they did.

The worker test needed an `isolated_duplex_settings` fixture because the extra
`tmp_path` argument put it over ruff's `PLR0913` limit.

A second, **pre-existing** leaker had to be fixed in the same commit or the new
guard could never go green: `test_cli.py::test_scan_paperless_error` preserves
the assembled PDF on upload failure, into the same shared directory. That is the
`…-test.pdf` the 29-09 summary noted as already present at `f0610e1`.

The new session-scoped autouse `_suite_leaves_the_shared_failed_dir_alone` fails
the run if anything new appears there, naming the files and the fix — the same
shape as `_suite_leaves_cwd_config_alone`. **Verified in both directions:** it
fired on the remaining leak before the CLI fix, and the suite now leaves the
directory empty. That directory held 22 entries on this machine, i.e. the
`_warn_if_failed_dir_growing` flake the review predicted was already live.

### WR-08: `asyncio.run()` on a loop Playwright left running — `4156abe`

**Files:** `tests/test_cross_origin.py`

The sibling's `ThreadPoolExecutor` workaround, copied verbatim with its comment.
This was cheap after all, so it was fixed rather than deferred.

It is worth recording that this one failure caused **three**: the coroutine it
left unawaited was collected inside later, unrelated tests, where
`filterwarnings = ["error"]` turned the `RuntimeWarning` and the
`PytestUnraisableExceptionWarning` into a failure and an error in
`tests/test_pipeline.py` and `tests/test_worker.py`.

`uv run pytest -q -p no:randomly tests/test_browser.py tests/test_cross_origin.py`
goes from `1 failed, 108 passed` to `109 passed`.

### WR-09: `build_pdf_filename`'s uniqueness argument did not hold for the CLI — `0cacfbc`

**Files:** `src/saneless/cli.py`, `src/saneless/pipeline.py`, `src/saneless/pdf.py`, `tests/test_cli.py`

`scan()` now mints a `uuid4()`, which is the option that makes both docstrings
true rather than the one that weakens them. Both docstrings are corrected to say
both entry points supply one.

`build_pdf_filename`'s docstring additionally now states the bound the guarantee
*really* carries, which neither docstring did before: only the first
`_JOB_ID_LENGTH` (8) characters of the id are used, so it is a
1-in-4-billion coincidence per same-second same-title pair, not an absolute.
That is the same truncation the 29-09 summary flagged and left for plan 08.

`test_two_cli_scans_of_one_title_preserve_as_two_files` drives two scans back to
back, asserts two distinct preserved files, **and asserts the two runs really
landed in the same second** — without that last line the timestamp alone would
have separated them and the test would prove nothing. It fails at the parent
commit with 1 file instead of 2.

### WR-10: `_snap_flatbed` could not be given a grace — `090c35d`

**Files:** `src/saneless/scanner/sane_backend.py`, `tests/test_scanner.py`

The review's literal shape — a sixth parameter, matching `_acquire_pages` —
breaks ruff's `PLR0913` at its five-argument ceiling, and CLAUDE.md forbids both
raising the limit and suppressing the rule. The two values therefore travel as
one frozen `_PageBudget(timeout, grace)`, which is the answer
`pipeline._DeliveryContext` already gives to the identical constraint. Both
defaults are the ADF path's own constants, so D-14's "same constant, no new
config key" is expressed *in* the default rather than merely asserted about it.
`_DEFAULT_PAGE_BUDGET` is a module constant, not an inline call in the
signature, because a call in a default argument is what ruff's `B008` rejects.

`test_flatbed_did_not_respond_to_cancel` is the mirror of
`test_did_not_respond_to_cancel` the review asked for. It bounds its own elapsed
time: without that it would pass in ten seconds if the grace stopped being
forwarded, which is the regression the parameter exists to prevent. The existing
signature assertion in `test_flatbed_timeout_matches_the_adf_message_shape` now
pins the grace as well as the timeout.

### WR-11: a raw `OSError` could escape `SpooledPageSink.add` — `62a7a43`

**Files:** `src/saneless/spool.py`, `tests/test_spool.py`

`shutil.disk_usage` now gets the same translation the write has, naming the page
and the spool path. `add`'s D-07 promise is now true of the measurement as well.
The docstring records both escape routes the review traced — mislabelled as
"Scanner error on page N" on the ADF path, and wholly untranslated on the
flatbed path, whose `sink.add` sits outside its `try`.

The test removes the spool directory *after* the sink is built, which is the
case production sees: a spool that existed when the scan started and did not
when the page arrived. It fails at the parent commit with a bare
`FileNotFoundError`.

---

## Not attempted (out of scope)

The four INFO findings were excluded by the brief and were not touched. Two are
worth a line each for whoever picks them up:

- **IN-01** (`_release_wedge` closes before dropping the iterator) is a one-line
  reordering and is genuinely worth doing; it is safe today only by a
  two-library coincidence, in a module whose `_retain_iterator` docstring goes
  out of its way to explain that this exact `__del__` is a hazard.
- **IN-03** (`docker.md` calls a compressed size "uncompressed") sits in the
  very bullet WR-06 rewrote. It was left alone deliberately, to respect the
  stated scope — it is a three-word correction whenever the INFO tier is picked
  up.

**IN-04**, the `test_paperless.py` poll-deadline flake, was not fixed and did
not reproduce in any of four consecutive full-suite runs after WR-08 landed.
The most likely explanation is that it was collateral of WR-08's unawaited
coroutine (the same mechanism that produced the other two spurious failures),
but that is not proven, and it remains an order-dependent parametrisation
outside this phase.

---

## Verification

Everything below was run at `62a7a43` with a clean working tree, and every
number read from raw redirected output rather than from the `rtk` hook's
summary.

| Check | Result |
|---|---|
| `uv run pytest -q` (full, single process — the release gate) | **2123 passed**, 0 failed |
| the same, three further consecutive runs (random order each time) | **2123 passed** each |
| `uv run pytest -m "not browser and not sane_hardware"` | 2049 passed |
| `uv run pytest -m browser` | 68 passed |
| `uv run pytest -m sane_hardware` | 6 passed |
| `uv run ruff check .` | No issues found |
| `uv run ruff format --check .` | 55 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --all-files` | exit 0 |
| `uv run prek run --stage pre-push --all-files` | exit 0, including `ty (full)` and `pyrefly (full)` |
| `/tmp/saneless-test/data/failed` after a full run | empty |
| `git status --porcelain` | clean |

**The release gate is now fully green**, which is better than the brief's
expectation. Both failures the orchestrator identified as pre-existing are gone:
WR-08's was fixed directly, and IN-04's did not reproduce in four runs (see
above).

**Suppression baseline unchanged** — per-file `# noqa` counts against
`9510cba`:

| File | base | now |
|---|---|---|
| `src/saneless/scanner/__init__.py` | 1 | 1 |
| `src/saneless/scanner/sane_backend.py` | 3 | 3 |
| `src/saneless/config.py` | 2 | 2 |
| `tests/test_cli.py` | 1 | 1 |
| `tests/test_web.py` | 1 | 1 |

`grep -rn 'type: *ignore' --include='*.py' src tests` returns nothing. No rule
was disabled, no `--no-verify` was used, no `SKIP=` was set; every one of the
thirteen commits ran the hooks.

**Locked decisions honoured.** An operator cancel still preserves nothing
(D-10) — asserted directly by a new test in WR-03's commit, because that is the
fix most likely to have widened it. `PageRecord` still holds facts and not
verdicts. A wedged handle is still strongly referenced and unclosed. `_preserving`'s
broad-`Exception`-narrow-span shape is unchanged. Nothing was keyed on
`PageRecord.sequence`; the two new job-keyed directory names both go through
`build_pdf_filename`, as D-10 and the 29-09 summary require.

**Not touched:** `STATE.md` and `ROADMAP.md`, as instructed.

---

_Fixed: 2026-09-15_
_Fixer: Claude (gsd-code-fixer)_
_Iteration: 1_
