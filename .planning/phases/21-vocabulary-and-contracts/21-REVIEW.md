---
phase: 21-vocabulary-and-contracts
reviewed: 2026-09-10T00:00:00Z
depth: deep
files_reviewed: 23
files_reviewed_list:
  - docs/explanation/consume-directory-fallback.md
  - src/saneless/auto_profiles.py
  - src/saneless/cli.py
  - src/saneless/job.py
  - src/saneless/paperless.py
  - src/saneless/pipeline.py
  - src/saneless/scanner/base.py
  - src/saneless/scanner/sane_backend.py
  - src/saneless/vocabulary.py
  - src/saneless/web/app.py
  - src/saneless/web/templates/index.html
  - src/saneless/web/templates/partials/history.html
  - src/saneless/web/templates/partials/status.html
  - src/saneless/worker.py
  - tests/conftest.py
  - tests/test_cli.py
  - tests/test_paperless.py
  - tests/test_pipeline.py
  - tests/test_scanner.py
  - tests/test_vocabulary.py
  - tests/test_web.py
  - tests/test_web_state_rendering.py
  - tests/test_worker.py
findings:
  critical: 1
  warning: 5
  info: 6
  total: 12
status: resolved
---

# Phase 21: Code Review Report

> **RESOLVED 2026-09-10.** CR-01 was reviewed with the user and the shipped
> behaviour kept -- see `21-CONTEXT.md` D-11 (amended) and commit `e6de95e`.
> WR-01..WR-05 and IN-02, IN-04, IN-05, IN-06 are fixed in commit `da52230`.
> IN-01 (fallow public surfaces) and IN-03 (remaining hand-derived
> classification in `generate_profiles`) require no change this phase: the
> first is sanctioned by D-12/D-07, the second is Phase 24's explicit
> deferral. Suite after fixes: 507 passed, 8 browser, all five checks clean.

**Reviewed:** 2026-09-10
**Depth:** deep
**Files Reviewed:** 23
**Status:** issues_found

## Summary

The consolidation itself is real, not cosmetic. I verified the load-bearing claims
rather than taking them on trust:

- **Exhaustiveness is genuinely enforced, all three match shapes.** I built a probe
  enum with two unhandled members and ran this project's own `ty` and `pyrefly`
  against it. All three shapes used in the diff — bare member arms with `case _:
  assert_never(x)` (`vocabulary.state_label/progress_label/error_message`,
  `PipelineEvent.job_state`), the or-pattern arm (`SourceKind.uses_feeder`), and
  the capture pattern `case unhandled: assert_never(unhandled)`
  (`auto_profiles.source_to_slug`) — produce
  `error[type-assertion-failure] ... Inferred type of argument is Literal[S.C, S.D]`
  from `ty` and `ERROR ... is not assignable to parameter arg with type Never
  [bad-argument-type]` from `pyrefly`. No `case _: raise ValueError`, no `.value`
  patterns, no guarded arms anywhere. D-08's pattern is applied correctly.
- **The `"auto"` substring trap is avoided.** `classify_source` tests `lower ==
  "auto"` by equality (`scanner/base.py:89`) and the AUTO branch is first, so
  `"Automatic Document Feeder"` cannot be swallowed by it. Covered by a
  discriminating parametrised test.
- **User-facing strings are byte-preserved.** `&#8230;` survives in `index.html:59`;
  `progress_label` reproduces the literal three-ASCII-dot spelling; `state_label`
  reproduces all seven history labels. `tests/test_vocabulary.py` pins the literals
  and `tests/test_web_state_rendering.py` pins which state renders which markup.
- **Worker projection and event ordering.** `_status_cb` applies only
  `ACTIVE_STATES`, so `DONE` can no longer be written from inside the pipeline's
  `TemporaryDirectory` — and `tests/test_worker.py` now asserts that with a real
  discriminating assertion. `.clear()` still precedes `update_state(AWAITING_FLIP)`
  and `.set()` still follows for `ASSEMBLING`/`UPLOADING`.
- **Dependency direction and template safety hold.** `vocabulary.py` imports only
  `saneless.exceptions`; `scanner/base.py` imports nothing from the package; no
  `| safe` anywhere under `web/`; every route renders through the one
  `app.state.templates` environment that carries the `JobState` global.
- Gate is green: 498 passed / 8 deselected, `ruff check`, `ruff format --check`,
  `ty check` and `pyrefly check` all clean. No `# noqa` or `# type: ignore` added.

What the diff gets wrong is concentrated in one place: **the source-classification
unification reaches further than D-11 authorised.** D-11 licenses one behaviour
change — `"Automatic Document Feeder"` routing to `multi_scan()`. The implementation
also flips routing for every source name containing `"duplex"` but no feeder token,
which includes this project's own documented `source = "Manual Duplex"`. That is
CR-01. Beyond it, the new `ScanResult` contract misreports the duplex-mismatch path,
the worker's rewrite quietly widened `_transition_event` signalling, and the one
ordering invariant the context calls out by name has no test that would catch its
loss.

## Critical Issues

### CR-01: Source unification silently changes routing for the documented `"Manual Duplex"` profile — a second, unauthorised behaviour change

**File:** `src/saneless/scanner/base.py:91-94`, `src/saneless/scanner/sane_backend.py:492`

**Issue:**

D-11 authorises exactly one routing change and states the boundary explicitly:
*"Confining this phase's change to names that are unambiguously feeders is what
keeps it defensible."* The implementation exceeds that boundary.

`classify_source` tests `"duplex"` **before** the feeder tokens and maps any hit to
`FEEDER_DUPLEX`, whose `uses_feeder` is `True`:

```python
if "duplex" in lower:
    return SourceKind.FEEDER_DUPLEX      # base.py:91-92  -> uses_feeder True
if any(token in lower for token in _FEEDER_TOKENS):
    return SourceKind.FEEDER             # base.py:93-94
```

`sane_backend.py:492` now reads `classify_source(effective_source).uses_feeder`
where it previously read `_is_adf_source(source)` = `"adf" in source.lower()`.
The consequence for a source name that contains `"duplex"` but no `"adf"`:

| source string | before (`"adf" in ...`) | after (`uses_feeder`) |
|---|---|---|
| `Automatic Document Feeder` | `snap()` | `multi_scan()` — **authorised (D-11)** |
| `Manual Duplex` | `snap()` | `multi_scan()` — **not authorised** |
| `Card Duplex` | `snap()` | `multi_scan()` — **not authorised** |

`"Manual Duplex"` is not a hypothetical. It is the literal value in the project's
own how-to (`docs/how-to/set-up-adf-duplex.md:56-63`):

```toml
[profiles.manual-duplex]
source = "Manual Duplex"
```

Trigger path: `scan_pages` only keeps `settings.source` as `effective_source` when
the device reports no `source` option (`sane_backend.py:469` — otherwise it
substitutes `"Auto"` or raises). On such a device — the single-source flatbeds that
manual duplex exists to serve — each pass previously ran `dev.start()` / `dev.snap()`
and returned one page. It now enters `_scan_adf_pages`, whose very first action is
`dev.multi_scan()` wrapped in a bare `except Exception: raise FeederEmptyError`
(`sane_backend.py:373-376`). A flatbed that cannot honour `multi_scan()` therefore
turns a working documented configuration into a job that fails with *"No paper
detected in feeder."*

The test suite records this as intentional in a comment
(`tests/test_scanner.py`, the `("Manual Duplex", SourceKind.FEEDER_DUPLEX)` case:
*"Its exposure is narrow and Phase 25 deletes the source-overloading entirely"*),
but a comment in a test is not where a second behaviour change in a phase that
promised exactly one belongs. Phase 25 is a future promise; the how-to still tells
users to write that string today, and it was not updated.

**Fix:** keep the unification but stop the duplex rule from claiming names that
carry no feeder evidence. Require a feeder token *or* an explicit allowance, and
route the two known non-feeder duplex names away from `multi_scan()`:

```python
_FEEDER_TOKENS = ("adf", "document feeder", "feeder")

def classify_source(source: str) -> SourceKind:
    lower = source.strip().lower()
    if lower == "auto":
        return SourceKind.AUTO
    has_feeder_token = any(token in lower for token in _FEEDER_TOKENS)
    if "duplex" in lower and has_feeder_token:
        return SourceKind.FEEDER_DUPLEX
    if has_feeder_token:
        return SourceKind.FEEDER
    if "flatbed" in lower:
        return SourceKind.FLATBED
    # "Manual Duplex" / "Card Duplex": duplex intent, no feeder evidence.
    # Today's routing is the single-page path; changing it is Phase 24's bet.
    return SourceKind.UNKNOWN
```

Caveat the fix must handle: `source_to_slug` currently maps `"Manual Duplex"` to
`"adf-duplex"` (via `FEEDER_DUPLEX`), which is exactly what the pre-phase
implementation produced. Sending it to `UNKNOWN` would slug it `manual-duplex`
instead -- a profile-name change nobody asked for. Preserve the old slug by
special-casing it inside `source_to_slug`'s `UNKNOWN` arm, where naming and routing
are already allowed to disagree (that arm carries exactly that distinction for
`"ADF Back"`). `"ADF Duplex"` is unaffected either way: it carries `"adf"`.

If instead the wider classification is genuinely wanted, it must be raised as an
amendment to D-11, `docs/how-to/set-up-adf-duplex.md` must be corrected in the same
commit, and a test must assert the new `multi_scan()` routing for `"Manual Duplex"`
explicitly -- today only the `SourceKind` mapping is asserted, never the routing that
`SourceKind` drives.

## Warnings

### WR-01: `ScanResult.outcome` is hard-coded to `SUCCESS` on the duplex-mismatch path, even when both uploads only reached the consume directory

**File:** `src/saneless/pipeline.py:478-484`, `src/saneless/pipeline.py:225-238`

**Issue:** `_handle_duplex_mismatch` calls `paperless.upload_document` twice and
discards both `UploadResult`s. `run_pipeline` then returns
`ScanResult(outcome=ScanOutcome.SUCCESS, ...)` unconditionally. If paperless-ngx is
down, both PDFs land in the consume directory with `delivered_to_api=False` and the
new typed contract still reports `SUCCESS` — precisely the lie `ScanOutcome.FALLBACK`
was introduced to prevent. The pre-phase code returned an untyped
`{"status": "DONE", "warning": ...}`, so it made no outcome claim; the new code makes
a claim and gets it wrong. Nothing consumes `outcome` yet, which is why no test
catches it, but Phase 23 will build on this value.

**Fix:** derive the outcome from the two results the same way the main path does:

```python
fronts_result = paperless.upload_document(fronts_pdf, f"{title} (fronts)", ...)
backs_result = paperless.upload_document(backs_pdf, f"{title} (backs)", ...)
delivered = fronts_result.delivered_to_api and backs_result.delivered_to_api
```

and return that through `_handle_duplex_mismatch` so `run_pipeline` can pick
`ScanOutcome.SUCCESS` / `ScanOutcome.FALLBACK`. Add a test mirroring
`test_fallback_outcome_when_not_delivered_to_api` for the mismatch path.

### WR-02: The `_status_cb` rewrite widened `_transition_event` signalling and added a redundant state write, in a phase whose contract is "no behaviour change"

**File:** `src/saneless/worker.py:199-217`

**Issue:** The old `if`/`elif` chain named four events and did nothing for the other
two. The new membership-based dispatch acts on all six:

| event | before | after |
|---|---|---|
| `SCANNING` | *(no branch — nothing)* | `update_state(SCANNING)` **and** `_transition_event.set()` |
| `DONE` | *(no branch — nothing)* | `_transition_event.set()` |

`SCANNING` now issues a second `UPDATE jobs SET state=?, error=?, error_category=?`
+ `commit()` for a job that `_process_job:181` already set to `SCANNING` moments
earlier — redundant, and it also blanks `error`/`error_category` a second time.
`DONE` now sets `_transition_event` from *inside* `run_pipeline`, before the worker's
own `update_state(DONE)` at `:236`, so a concurrent `wait_transition()` is released
one step earlier than it used to be and observes the job still in `UPLOADING`.

Both are benign in today's call graph (`wait_transition` is only reached from
`POST /api/flip/continue`), which is exactly why they slipped through — no test
distinguishes them. But the phase contract is byte-level behaviour preservation, and
the in-code comment ("Nothing to persist from inside the pipeline") describes only
half of what the branch now does.

**Fix:** restore the old signal set explicitly rather than as a side effect of
membership testing:

```python
def _status_cb(event: PipelineEvent, _jid: str = job.id) -> None:
    logger.info("Pipeline event: %s", event.value)
    state = event.job_state
    if state is None:
        # SCANNING_REVERSE: prose only, but the old chain still signalled.
        self._transition_event.set()
        return
    if state not in ACTIVE_STATES:
        # DONE is terminal; the worker writes it after run_pipeline returns.
        return
    if state is JobState.SCANNING:
        return  # already written before the pipeline started
    ...
```

If the wider signalling is wanted, say so in the comment and add a test that pins it.

### WR-03: The one ordering invariant the phase context names by hand has no discriminating test

**File:** `src/saneless/worker.py:212-217`, `tests/test_worker.py:728-755`

**Issue:** 21-CONTEXT.md calls the clear-before-`AWAITING_FLIP` / set-after-`ASSEMBLING`
ordering *"a concrete instance of what 'no behaviour change' means here."* The only
test touching it is `test_wait_transition_returns_true`, which asserts
`worker.wait_transition(timeout=2.0) is True` after `continue_flip()`.

That test cannot fail if the invariant breaks. Delete the
`self._transition_event.clear()` at `worker.py:216` and the event is still set from
the earlier `SCANNING` write, so `wait_transition` returns `True` immediately and the
test passes. The whole point of the `clear()` — that the flip window is observable to
the one-second poll as *not busy* — goes untested.

**Fix:** assert the event state directly around the transition rather than only the
return value of `wait_transition`:

```python
# while AWAITING_FLIP, the transition event must be CLEAR
for _ in range(50):
    time.sleep(0.05)
    if _get(store, job.id).state == JobState.AWAITING_FLIP:
        break
assert not worker._transition_event.is_set()
worker.continue_flip()
assert worker.wait_transition(timeout=2.0) is True
```

Better still, exercise `_status_cb` directly with a stub `JobStore` that records
`(event_is_set, state)` pairs at write time, so ordering is asserted rather than
sampled.

### WR-04: Three tests pin `JobState` membership, one of them asserts the absence of future work

**File:** `tests/test_vocabulary.py:41-61`, `tests/test_vocabulary.py:91-97`

**Issue:** `test_job_state_has_exactly_seven_members`, `test_job_state_member_names`
and `test_job_state_has_no_future_phase_members` all pin the same fact three times,
and the third is an anti-test: it asserts `"FALLBACK" not in names` and
`"SCANNING_REVERSE" not in names`. `test_scan_outcome_has_no_failed_member` is the
same shape for `ScanOutcome`.

These do not protect anything — nothing can accidentally add an enum member — and
they invert the discipline D-09 asks for. D-09 is explicit: *"Parametrising over
`list(JobState)` — never a hand-written list of names — is what makes a new member
fail the suite automatically."* `test_job_state_member_names` is exactly a
hand-written list of names, and it additionally over-specifies declaration **order**,
which is not part of any contract. When Phase 23 adds `FALLBACK` it will have to
delete assertions whose only content was "Phase 23 has not happened yet."

**Fix:** keep the parametrised completeness tests (`test_state_label_is_complete`,
`test_every_state_is_active_xor_terminal`, `test_job_reports_activity`) — those are
the ones that force a decision on a new member. Delete
`test_job_state_has_no_future_phase_members`, `test_scan_outcome_has_no_failed_member`
and the order-sensitive `test_job_state_member_names`; if a member-count guard is
wanted at all, one is enough.

### WR-05: `UploadResult` can be constructed in a contradictory state, and `run_pipeline` silently downgrades to `FALLBACK` when it is

**File:** `src/saneless/paperless.py:26-33`, `src/saneless/pipeline.py:513-521`

**Issue:** `UploadResult(delivered_to_api: bool, task_uuid: str | None = None,
consume_dir_path: Path | None = None)` places no constraint between the flag and the
two payload fields. `UploadResult(delivered_to_api=True)` — no task UUID — is legal.
`run_pipeline` then takes the `else` branch:

```python
if upload_result.delivered_to_api and task_uuid is not None:
    paperless.poll_task(...); outcome = ScanOutcome.SUCCESS
else:
    outcome = ScanOutcome.FALLBACK          # pipeline.py:521
```

A document that *was* delivered to the API is reported as having only reached the
consume directory, and `consume_dir_path` is `None` so nothing downstream can even
notice. This replaced a sentinel whose whole defect was carrying no structure; the
replacement should not be able to lie about itself. (Note the adjacent live case:
`paperless.py:127-129` does `str(response.json())`, so a JSON `null` body yields the
string `"None"` — truthy and not `None` — and `poll_task("None")` is called. That is
pre-existing, but the new typed result is the natural place to reject it.)

**Fix:** make the two states mutually exclusive by construction, e.g. two
constructors and a `__post_init__` guard:

```python
@dataclass(frozen=True)
class UploadResult:
    delivered_to_api: bool
    task_uuid: str | None = None
    consume_dir_path: Path | None = None

    def __post_init__(self) -> None:
        if self.delivered_to_api != (self.task_uuid is not None):
            msg = "delivered_to_api and task_uuid must agree"
            raise ValueError(msg)
```

then `pipeline.py:514` can read `if upload_result.delivered_to_api:` alone and the
`task_uuid is not None` narrowing becomes a type-checker concern rather than a
silent behavioural fork.

## Info

### IN-01: Four new public surfaces have no production reader

**File:** `src/saneless/vocabulary.py:83`, `src/saneless/vocabulary.py:183`, `src/saneless/pipeline.py:107-115`, `src/saneless/paperless.py:31`

**Issue:** `TERMINAL_STATES` appears in `src/` only at its own definition and in
`__all__`; `error_message()` likewise. `ScanResult.pages_scanned` /
`pages_removed` / `pages_uploaded` / `warning` are never read — both callers of
`run_pipeline` (`worker.py:230`, `cli.py:133`) discard the return value.
`UploadResult.consume_dir_path` is written and never read.

`error_message` is explicitly sanctioned as fallow by D-12, and `TERMINAL_STATES` earns
its place as the documented partition companion. The `ScanResult` fields are the
"shape now, use later" half of D-07/CTR-02. Recording this so a later reviewer does
not mistake dead-on-arrival code for an oversight — and so Phase 23 knows the
counts have never been exercised against a real reader.

**Fix:** none required this phase. Phase 23 should wire at least one of the counts
into the worker's completion log line so the arithmetic gets a reader.

### IN-02: `BUSY_STATES` lacks the explicit annotation its two siblings carry

**File:** `src/saneless/vocabulary.py:90`

**Issue:** `ACTIVE_STATES: frozenset[JobState]` and `TERMINAL_STATES:
frozenset[JobState]` are annotated; `BUSY_STATES = ACTIVE_STATES - {...}` is not.
The inferred type is correct, so this is consistency only — but the three constants
are read as a group and one of them looking different invites a reader to wonder
whether the difference is meaningful.

**Fix:** `BUSY_STATES: frozenset[JobState] = ACTIVE_STATES - {JobState.AWAITING_FLIP}`.

### IN-03: `generate_profiles` still hand-derives source classification, and now disagrees with `classify_source` on whitespace

**File:** `src/saneless/auto_profiles.py:181-183`, `src/saneless/auto_profiles.py:193`

**Issue:** The same module whose `source_to_slug` was routed through
`classify_source` still spells the rule out twice more:
`if source.lower() == "auto":` (`:181`) and `"flatbed" in s.lower()` (`:182`, `:193`).
Wiring the remaining sites is D-11's explicit Phase 24 deferral, so this is not a
scope violation — but two concrete divergences were introduced today:

1. `classify_source` does `source.strip().lower()`; `:181` does not strip. A
   capability list reporting `"  Auto  "` classifies as `SourceKind.AUTO` and slugs
   to `auto-scan`, but never receives the `auto_source_mode="adf"` treatment.
2. `:193` picks the default profile with `"flatbed" in s.lower()`, which now
   disagrees with `classify_source` for any name carrying both `"flatbed"` and
   `"duplex"`/a feeder token — `classify_source` returns `FEEDER_DUPLEX`/`FEEDER`
   for those, so the same source would be slugged `adf-duplex` and simultaneously
   chosen as the flatbed default.

**Fix:** Phase 24 work. Replace both with
`classify_source(s) is SourceKind.AUTO` / `is SourceKind.FLATBED` when the remaining
sites are wired; note the whitespace divergence in that phase's context.

### IN-04: `source_to_slug`'s precedence order changed — flatbed no longer wins

**File:** `src/saneless/auto_profiles.py:64-81`

**Issue:** The deleted implementation tested `"flatbed"` *before* `"duplex"` and
`"back"` *before* the feeder tokens. `classify_source` tests duplex first, feeder
tokens second and flatbed last. No name in `tests/test_auto_profiles.py` distinguishes
the two orders, so the suite cannot see the change — but a source containing both
`"flatbed"` and `"duplex"` now slugs `adf-duplex` where it used to slug
`flatbed-scan`. The `"back"` reordering is harmless (its new position inside the
`FEEDER` arm produces the same string for names with and without a feeder token).

**Fix:** none required — the new order is the documented, deliberate one
(`scanner/base.py:65-76`). Worth a parametrised case pinning
`source_to_slug("Flatbed Duplex")` so the precedence is asserted rather than implied.

### IN-05: Two rendering assertions compare the template's output against the same function the template calls

**File:** `tests/test_web_state_rendering.py:134`, `tests/test_web_state_rendering.py:162`

**Issue:** `assert f">\n    {state_label(state)}\n  </td>" in response.text` and
`busy_line = f'<p aria-busy="true">{progress_label(state)}</p>'` both build the
expected string from the same function the Jinja filter invokes. Taken alone they
would pass for any label text whatsoever — they pin *which state reaches which
filter*, which is genuinely valuable, but not the label content their docstrings
claim ("byte for byte").

The literals are in fact pinned, in `tests/test_vocabulary.py:157-196`, so the pair
of files together does discriminate. Flagging so the docstrings are not read as a
stronger guarantee than the assertion gives.

**Fix:** adjust the docstrings to say "each state reaches its own filter", or inline
one literal expectation (e.g. `JobState.UPLOADING` -> `"Uploading to
paperless-ngx..."`) into the rendering test so it stands alone.

### IN-06: Test helper reaches into worker private state

**File:** `tests/test_web_state_rendering.py:123`

**Issue:** `_app(client).state.worker._current_job_id = job.id` writes a private
attribute of a live, started `ScanWorker` thread. It is safe today because no job is
ever submitted in these tests, so `_process_job`'s `finally` block can never race the
assignment — but the coupling means any change to how the worker tracks its current
job breaks six parametrised test families at once.

**Fix:** expose a narrow test seam (a settable `current_job_id`, or a
`worker.adopt_job(job_id)` used only by tests) or drive the state through the real
submit path with a stubbed `run_pipeline`.

---

_Reviewed: 2026-09-10_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
