# Phase 25: Manual Duplex - Context

**Gathered:** 2026-09-13
**Status:** Ready for planning

<domain>
## Phase Boundary

Manual duplex actually works, and the job is honest about which pass it is in. Everything in
this phase lives in `src/saneless/config.py` (the new `duplex` field and its legacy
translation), `src/saneless/pipeline.py` (`_is_manual_duplex`, `_scan_manual_duplex`, the
dispatch in `run_pipeline`), `src/saneless/worker.py` (the duplicated rule and the
`_transition_event` protocol), `src/saneless/cli.py` (the flip prompt that does not exist),
`src/saneless/web/routes.py` (the three status routes), `src/saneless/vocabulary.py` (the
ninth `JobState`), `src/saneless/auto_profiles.py` (the `duplex` key on generated profiles),
and `src/saneless/scanner/` (feeder-source resolution), plus the ADF how-to rewritten
in-phase.

Concretely, this phase closes seven gaps that exist in the code today:

1. **`source` carries the scanning strategy.** `pipeline._is_manual_duplex(profile.source)`
   (`:383-385`) decides the mode, then the same string is handed to SANE at `:841`.
   `_resolve_source` (`sane_backend.py:792`) validates it against the device's real option
   list, so `"Manual Duplex"` either raises `ScanError` or — on a device exposing `Auto` —
   silently substitutes it and, with `auto_source_mode` defaulting to `flatbed`, takes one
   platen snapshot per pass. Either way the documented feature does not work (C-01).
2. **The rule is duplicated.** `worker.py:194` re-implements it as
   `"manual" in source and "duplex" in source`. If one drifts, the pipeline skips the wait —
   which is exactly the C-02 failure (N-07).
3. **The CLI supplies no coordinator.** `cli.py` builds `PipelineRequest` with no
   `flip_event`, and `pipeline.py:709` reads `if request.flip_event is not None:`, so pass B
   begins the instant pass A ends. On real feeder hardware pass B then calls `multi_scan()`
   on an empty tray and the unwind through `TemporaryDirectory` destroys the fronts (C-02).
4. **The flip wait is unbounded.** `request.flip_event.wait()` blocks forever, parking the
   single worker thread and queueing every later job behind it (M-07).
5. **Pass B is invisible.** `PipelineEvent.SCANNING_REVERSE.job_state` returns `None`
   (`pipeline.py:60-66`), so the job stays `AWAITING_FLIP` for all of pass B: the flip prompt
   stays on screen while pages feed, and its Abort button does nothing because the abort is
   checked once, before pass B. `wait_transition` (`worker.py:111`) additionally blocks the
   event loop for 2 s at `routes.py:297` (M-02).
6. **A union return is dispatched by `isinstance`.** `pipeline.py:871` type-switches on
   `_DuplexMismatch` (N-07).
7. **`auto-profiles` and the `default` profile.** DPLX-07's requirement — see the finding
   below; this is very likely already satisfied.

Requirements in scope: **DPLX-01 through DPLX-07**.

**Not in this phase:**
- **Cancelled-job vocabulary** — `ScanCancelledError`, a `CANCELLED` state, and not logging an
  abort at ERROR level are **Phase 28** (EXC-04, N-08). D-15 deliberately stops short.
- **Mid-pass abort** (aborting *during* pass B rather than at the prompt) — needs HARD-02's
  lazy page consumption, **Phase 29**. See D-16: the control disappears from the UI instead.
- **A `wait_for_state` polling helper for worker tests** — **Phase 26** (ROBU-03) explicitly
  owns that conversion. D-14 deletes the event protocol but must not build its replacement.
- **Owner-only flip prompt, Abort confirmation, front/back page counts during pass B** —
  **Phase 30** (APPL-09, APPL-03).
- **`extra="forbid"` on the new key, and `--force` merge semantics** — **Phase 27**
  (CFG-01, CFG-07), which must treat `duplex` as a generated key.
- **Blocking-route conversion and 429 backpressure** — **Phase 26** (ROBU-02, ROBU-05), even
  though D-17 edits the same three routes.

</domain>

<decisions>
## Implementation Decisions

All eighteen decisions below are **locked for planning**. A planner may implement them, not
relitigate them. A planner who believes one is wrong should raise it rather than silently
implement the other branch. One diverges from the option flagged "Recommended" during
discussion — **D-18** — and it is called out where it appears.

### The duplex field and the legacy source (DPLX-01, DPLX-02)

- **D-01: `ProfileConfig` gains `duplex: Literal["none", "hardware", "manual"] = "none"`, and
  `source` becomes a pure SANE value.** C-01's prescription. `source` is passed to the device
  verbatim and is never inspected to decide a scanning strategy anywhere in the codebase.
  `pipeline._is_manual_duplex` is deleted, and `worker.py:194`'s copy of the rule becomes
  `profile.duplex == "manual"` — or disappears entirely, since D-09 moves the coordinator
  decision to one place.

- **D-02: a legacy `source = "Manual Duplex"` translates to `duplex = "manual"` at config
  load, and the device source is resolved at scan time from the device's own source list.**
  The backend picks the first source classifying `FEEDER` or `FEEDER_DUPLEX` via Phase 24's
  `classify_source`. **Do not hardcode `"ADF"`** — the review suggests it, but Phase 24
  established that real feeders are named `"Automatic Document Feeder"` (Epson, Canon,
  Brother), and `_resolve_source` validates against the device's actual list, so a hardcoded
  `"ADF"` would fail on precisely the consumer hardware manual duplex exists for.
  - **The backend is the right place because it already holds the answer.** `scan_pages`
    fetches `raw_options` once at the top of its device context, with an explicit comment that
    a second `get_options()` would be a wasted round trip. Resolving in the pipeline instead
    would require a `get_capabilities` call — a whole extra `sane.open`/`dev.close()` — before
    every manual-duplex pass A, because `run_pipeline` never fetches capabilities today
    (`_resolve_device` only calls `get_devices()` when `scanner.device` is empty).
  - Rejected: keeping `source = "Manual Duplex"` and failing loudly with guidance. Most
    honest, but it conflicts with success criterion 1's "still loads **and scans**".

- **D-03: the translation lives in `ProfileConfig`, the named warning lives in `Settings`.**
  A `model_validator(mode="before")` on `ProfileConfig` performs the translation, so every
  construction path is covered — TOML, environment variable, and direct `ProfileConfig(...)`
  in code and tests (there are ~219 duplex-source references across the suite, including
  `tests/test_browser.py:140` and `tests/test_outcomes_e2e.py:97`). The existing
  `field_validator("profiles")` (`config.py:177`) then detects already-translated legacy
  profiles and emits the deprecation warning **naming the profile**, once per load.
  - `ProfileConfig` cannot name itself — it does not know its own key — which is the whole
    reason the roles are split.
  - Validators running after source merging is pydantic-settings' own contract (sources are
    merged into one dict, then the model validates). **Do not spend a test proving it.**

- **D-04: the deprecation warning is a `logger.warning`, not `warnings.warn`, and not
  pydantic's `Field(deprecated=...)`.** Measured constraints: `filterwarnings = ["error"]` is
  live in `pyproject.toml`, and there is **zero** `warnings.warn`/`DeprecationWarning`
  precedent anywhere in `src/` or `tests/`. Pydantic's built-in `deprecated` is wrong on two
  counts independently — it warns on *field access* rather than at load (the wrong trigger for
  DPLX-02), and under the live filter every access would become a test error.

- **D-05: all three `duplex` values ship, and `"hardware"` is declarative.** DPLX-01 names
  `none | hardware | manual` and the requirement stands as written. **Nothing reads
  `"hardware"`, and the code must say so in a comment.** Measured: `FEEDER_DUPLEX` appears in
  `src/` exactly three times, all inside `scanner/base.py` — the declaration (`:35`), an arm
  grouping it *with* `FEEDER` for `uses_feeder` (`:43`), and the classifier's return (`:91`).
  Nothing outside the enum's own definition branches on it, because the device decides
  duplexing from the source name it is handed. The value records operator intent and makes a
  profile self-describing; **Phase 30's APPL-05** (`label`/`description`, "Feeder,
  double-sided") is its eventual reader.
  - Rejected: shipping `none | manual` only. Every member would then have a reader — the
    `ScanOutcome.FAILED` precedent — but it amends DPLX-01 and rejects a user who accurately
    describes their hardware.
  - Rejected: validating that `"hardware"` only appears on a `FEEDER_DUPLEX` source. It would
    give the value a reader, but this codebase has deliberately never cross-validated profile
    fields — `auto_source_mode` is meaningful only when `source` is `Auto` and is never checked
    against it.

- **D-06: `auto-profiles` emits `duplex = "hardware"` for `FEEDER_DUPLEX` sources and omits the
  key everywhere else.** Follows the recorded Phase 16 precedent, "Only write
  `auto_source_mode` to TOML when non-default (keep config clean)". Note `auto-profiles` can
  never emit `"manual"`: manual duplex is not a device source, so those profiles are always
  hand-written. Phase 24's D-16 prune and Phase 27's CFG-07 merge must treat `duplex` as a
  generated key.

### Strategy dispatch (DPLX-03)

- **D-07: the `isinstance` dispatch becomes a total `match` with `assert_never`.**
  `pipeline.py:871` becomes `match duplex_result: case ScanBatch(): ... case
  _DuplexMismatch(): ... case _: assert_never(...)`. Phase 21's D-08 *measured* that only
  `match` + `assert_never` is enforced by both `ty` and `pyrefly`, so a third variant fails the
  Phase 20 gate at edit time.
  - **Half of N-07 is already done** — its prescription was "replace the tuple with a
    `DuplexMismatchError` or a tiny result dataclass", and the tuple became `_DuplexMismatch`
    in an earlier phase. **Add a comment recording that this dispatch satisfies N-07**, or a
    later reader comparing code to the review will read it as unaddressed.
  - Rejected: raising `DuplexMismatchError`. A mismatch is a *recovery* that successfully
    uploads two partial PDFs, not a failure; modelling it as an exception fights Phase 23's
    D-08 parity work, drags PIL images through a raise, and — if it subclassed `ScanError` —
    would make `classify_error` file a successful run under `SCANNER`.

- **D-08: the duplex-mismatch branch keeps bypassing `_drop_empty_pages`, and this is
  documented as deliberate.** The branch returns at `pipeline.py:~890` with a hardcoded
  `pages_removed=0`, before filtering at `:915`. That stays. A mismatched run is an anomaly
  routed to a human for manual review (Phase 23 D-08), and a blank back side is *evidence*
  about why the two passes disagreed — removing it destroys the diagnostic the partial PDFs
  exist to provide. `pages_removed=0` is then literally true rather than a lie.
  - **This must be stated in code and in the ADF how-to.** The profile's toggle visibly does
    not apply on this one path, and N-07 calls it a defect; without an explicit note a future
    reader will "fix" it.
  - Rejected: filtering each pass with the existing helper. `_drop_empty_pages` **raises
    `ScanError` when every page is empty** (`pipeline.py:326-327`), so an all-blank backs pass
    would destroy the fronts — converting a recoverable anomaly into the data loss Phase 23
    spent a phase removing.

### The flip coordinator (DPLX-04, DPLX-05)

- **D-09: `FlipCoordinator` is an ABC with one abstract method returning a total enum.**
  `wait_for_flip(timeout: float) -> FlipOutcome`, where `FlipOutcome` is
  `CONTINUED | ABORTED | TIMED_OUT`, consumed with `match` + `assert_never`. It replaces
  **both** `PipelineRequest.flip_event` and `abort_event`.
  - **ABC, not `typing.Protocol`, and the rule is observable in the tree:** `Protocol`
    describes shapes this project does not own (`SaneDevice` for python-sane's handle,
    `_SettingsFactory` for pydantic's constructor); `ABC` defines seams it implements
    (`ScannerBackend`, whose CLI stubs Phase 24's WR-08 deliberately moved from duck-typing to
    subclassing). DPLX-04's lowercase "protocol" means "contract".
  - **One atomic answer is the point.** Today `abort_flip()` sets *both* events
    (`worker.py:104-109`) so the waiter wakes and then separately inspects `abort_event` — the
    wake-then-ask-why two-step is where M-02's dead Abort lives.
  - The CLI implementation prompts; the web implementation wraps the existing worker events.

- **D-10: `OutputConfig.flip_timeout_seconds: int = 600`.** **This deliberately diverges from
  the codebase's own precedent**, and the divergence is the decision. Scan-side timeouts here
  are module constants consumed as default arguments —
  `_DEFAULT_PAGE_TIMEOUT_SECONDS: float = 120.0` (`sane_backend.py:66`, used at `:1034`) and
  `_MAX_ADF_PAGES: int = 500` (`:100`) — and Phase 24's D-04 explicitly *rejected* a scanner
  config knob. The reason to diverge: this is the only timeout that waits on a **human** rather
  than a machine, and ten minutes is a guess about someone else's household. It also matches
  M-07's literal prescription and the section where every existing timeout lives
  (`paperless_task_timeout`, `paperless_cache_ttl_seconds`).

- **D-11: the CLI uses `click.confirm`, plus an up-front refusal when stdin is not a TTY.**
  `click.confirm` is C-02's prescribed code and is testable through `CliRunner(input=...)`,
  which echoes prompts into output — exactly what makes C-02's assertion ("the prompt appears
  between `Scanning...` and `Scanning reverse sides...`") possible. When the profile is
  manual-duplex and stdin is not a terminal, refuse at the same early checkpoint as the
  missing-coordinator case, exiting **2** with a message saying manual duplex needs an
  interactive terminal.
  - **`click.pause` is disqualified, not merely dispreferred.** The click documentation states
    twice that it is a **no-op when not run through a terminal**. It is the only API matching
    the roadmap's "press Enter" wording literally, and using it would silently resume and
    re-scan the fronts as backs — C-02 verbatim — on the automation path
    `docs/how-to/cli-scripting.md` actively invites people onto. The suite would not catch it,
    because `CliRunner` is not a TTY either.
  - `cli.py` contains no `isatty`, no `click.confirm`, no `click.prompt` and no `input()`
    today; every interaction is one-way `click.echo`. This is the first interactive read in the
    CLI.

### Pass-B visibility (DPLX-06)

- **D-12: `JobState.SCANNING_REVERSE` is added as the ninth member.**
  `PipelineEvent.SCANNING_REVERSE.job_state` stops returning `None`, and the comment at
  `pipeline.py:60-66` reserving that seam is replaced by the answer.
  - **Verified: no migration is needed.** The schema declares `state TEXT NOT NULL`
    (`job.py:289`) with **no `CHECK` constraint**, so a ninth value persists and reads back
    without touching Phase 22's `PRAGMA user_version` ladder. **A planner must not invent a
    migration task.**
  - The ripple is gate-enforced and intended: the count guard at `tests/test_vocabulary.py:55`
    (`== 8` → `9`), whose docstring says adding a member *should* fail the parametrised tests
    "which force a label and a classification decision"; arms in `state_label` and
    `progress_label`; and membership in `ACTIVE_STATES`. `BUSY_STATES` is *derived*
    (`ACTIVE_STATES - {AWAITING_FLIP}`), so busy-ness follows automatically — which keeps
    `aria-busy` on and the Scan button disabled through pass B.
  - Rejected: mapping `SCANNING_REVERSE` to `JobState.SCANNING` (M-02's first suggestion). It
    fixes the bug with no new vocabulary, but the operator cannot tell pass A from pass B and
    Phase 30's APPL-03 would have to reintroduce the distinction.
  - Rejected: naming it `SCANNING_BACKS` (M-02's own wording). DPLX-06, the roadmap criterion,
    `PipelineEvent`, `cli.py:126` and `docs/explanation/architecture.md` all already say
    `SCANNING_REVERSE`.

- **D-13: `state_label` returns `"Scanning backs"`; `progress_label` returns
  `"Scanning reverse sides..."`.** The progress string is **byte-identical** to what
  `cli.py:131` already prints, so the CLI's `state is None` special case collapses into the
  general branch with no visible change to output. Both completeness tests assert
  `label != state.value`, so neither may echo the enum name. Register: short title-case for
  `state_label` (beside "Waiting for flip"), three ASCII dots for `progress_label`.

- **D-14: the entire `_transition_event` protocol is deleted, not just the public method.**
  `wait_transition`, the attribute, all five `set()` calls (`worker.py:189`, `:219`, `:231`,
  `:276`), both `clear()` calls (`:123`, `:236`), the call at `routes.py:297`, and the tests at
  `tests/test_worker.py:729`, `:758`, `:805` plus the usage at `:1400`.
  - **Measured justification:** `routes.py:297` is the **only** external reader, so after
    DPLX-06 removes it the event would be written five times and read never — dead machinery a
    future reader would mistake for load-bearing. This answers M-02's "the transition-event
    protocol is also more complex than it needs to be".
  - `tests/test_worker.py:758`'s own docstring documents the stale-signal flaw from the inside,
    so the existing tests pin the defect rather than guard against it. **Do not treat them as a
    safety net.**
  - **Do not build a `wait_for_state` polling helper here** — ROBU-03 (Phase 26) owns it
    explicitly, and its roadmap note says the conversion happens there.

### Abort semantics (DPLX-06, boundary with Phase 28)

- **D-15: an aborted flip raises `ScanError` with a clear message; the cancelled vocabulary is
  Phase 28's.** `FlipOutcome.ABORTED` produces a message naming the flip prompt as the cause;
  the job ends `ERROR` / `SCANNER` as it does today. Phase 25 owns making abort **work** — the
  CLI gains one for the first time; Phase 28's EXC-04 and N-08 own making it **classified**
  (`ScanCancelledError(ScanError)` or a `CANCELLED` state, "and do not log it at ERROR level").
  Splitting it here would leave EXC-04 half-implemented across two phases.

- **D-16: the coordinator answers once, and its answer is final.** A late Abort — arriving in
  the window after Continue but before the new state is persisted — finds the wait already
  resolved as `CONTINUED` and is dropped; the route returns current status rather than an
  error. Race-free by construction and honest: pass B has genuinely started, and stopping it
  mid-pass needs HARD-02's lazy consumption (Phase 29).
  - **The control disappears on its own, which is the real fix.** `status.html` renders
    `partials/flip.html` **only** in the `AWAITING_FLIP` branch; the busy branch is a bare
    `<p aria-busy="true">` with no buttons. Once `SCANNING_REVERSE` is its own state, Continue
    and Abort vanish during pass B. M-02's "Abort is a no-op during pass B" is therefore
    resolved by **removing the control**, not by making mid-pass abort work.
  - Rejected: having `continue_flip` render a busy partial without re-reading the job. It closes
    the window, but the route would assert a state the store has not recorded.
  - Rejected: keeping a short bounded wait. That is `wait_transition` renamed, which DPLX-06
    requires be deleted, and it re-blocks the event loop that ROBU-05 is coming to fix.

- **D-17: all three status routes share one "current job, else most recent" lookup.**
  `abort_flip` and `continue_flip` read `state.worker.current_job_id` directly, which the worker
  clears in its `finally` block (`worker.py:290`), so an Abort arriving as the job ends finds
  `None` and renders "Ready to scan." — reporting nothing for an abort that aborted nothing
  (M-02). `current_job_status` (`routes.py:186-192`) already does the right thing; extract it
  and use it in all three. Both flip routes are being rewritten in this phase anyway.

### Documentation (phase rule: correct what you changed)

- **D-18: the legacy `source = "Manual Duplex"` form is removed from the documentation
  entirely.** **DIVERGES from the recommendation**, which was to keep one short deprecation
  note. The how-to teaches only `source = "ADF"` + `duplex = "manual"`.
  - **Derived constraint, binding on D-04's message:** because DPLX-02 still translates the
    legacy key and warns, while no page mentions that key any more, **the warning text is the
    only migration instruction an operator will ever receive.** It must therefore state the
    replacement inline — set `duplex = "manual"` and a real device source — not merely announce
    deprecation. A warning pointing at documentation that no longer exists is the dead end this
    milestone's appliance goal exists to remove.

### Claude's Discretion

Genuinely open to the planner. Make the call and record the reasoning in the plan:

- **What `ScanSettings` carries to trigger feeder resolution** (D-02). Options weighed: a
  `DuplexMode` enum defined in `scanner/base.py` alongside `SourceKind`; an empty-string
  sentinel; a `prefer_feeder` boolean. Constraints: Phase 21's D-03 forbids the scanner package
  importing job vocabulary, so any enum lives in `scanner/`, not `vocabulary.py`; and if
  `duplex` ends up spelled both as a `ProfileConfig` Literal and a scanner-side enum there must
  be exactly one conversion point.
- **Where the "manual duplex without a coordinator" refusal fires** (DPLX-04). Hard constraint:
  it must precede any SANE contact, and `_resolve_device` calls `get_devices()` when
  `scanner.device` is empty. **Placing it inside `_scan_manual_duplex` is a trap** — that
  function scans pass A first, so refusing there burns a full feeder pass before failing.
- **What happens when a `duplex = "manual"` profile meets a device with no feeder source.**
  Hard constraint: the `"Auto"` substitution inside `_resolve_source` **must not be reachable
  for manual duplex** — that path is precisely C-01's mechanism, producing two snapshots of the
  platen and a green Complete. Raising before pass A in the existing "Device does not support
  ... Available: [...]" message shape is the expected shape.
- **Where `FlipOutcome` lives** — `vocabulary.py` is the leaf module every consumer already
  imports from, and it may not import `pipeline`.
- **Which doc sentences get rewritten**, beyond those named in `<canonical_refs>`.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements and roadmap
- `.planning/ROADMAP.md` § "Phase 25: Manual Duplex" — the goal and the five success criteria.
  **Criterion 2 needs amending** (see Specific Ideas).
- `.planning/ROADMAP.md` § Overview, ordering point 3 — why Phase 24 had to land first: the
  backend's own blank-page removal changes duplex page parity, and duplex tests written against
  untruthful SANE fakes assert a system that no longer exists.
- `.planning/REQUIREMENTS.md` lines 75-81 — DPLX-01 through DPLX-07 verbatim with their
  review-finding tags.
- `.planning/REQUIREMENTS.md` § EXC-04 (line 116), § HARD-02 (line 122), § ROBU-03 (line 86),
  § APPL-03 and APPL-09 (lines 131, 137) — the adjacent requirements this phase must **not**
  implement. Read them to know what to leave alone.

### The findings this phase resolves
- `.planning/reviews/2026-09-09-code-review.md` § C-01 (`:147`) — why `"Manual Duplex"` cannot
  reach real hardware, and the two failure paths (raise, or silent `Auto` substitution).
  **Its `device_source = "ADF"` suggestion is DECLINED by D-02 — do not implement it.**
- `.planning/reviews/2026-09-09-code-review.md` § C-02 (`:168`) — the CLI prompt, the
  prescribed `click.confirm` code, and the `CliRunner` test shape.
- `.planning/reviews/2026-09-09-code-review.md` § M-02 (`:397`) — the AWAITING_FLIP-through-pass-B
  defect, the dead Abort, the stale `current_job_id` lookup, and the transition-event critique.
- `.planning/reviews/2026-09-09-code-review.md` § M-07 (`:449`) — the unbounded flip wait and
  the ten-minute suggestion.
- `.planning/reviews/2026-09-09-code-review.md` § N-07 (`:851`) — the duplicated rule and the
  `isinstance` dispatch. **N-08, immediately below it, is Phase 28's — read it to know what not
  to build.**
- `.planning/reviews/2026-09-09-code-review.md` § 8, doc rows **1, 2, 22, 24** — the four false
  documentation claims this phase makes true.
- `.planning/reviews/2026-09-09-code-review.md` § 10, step 2 (`:1066`) — the review's own
  ordering for this exact work.

### Prior-phase decisions that bind this phase
- `.planning/phases/24-scanner-truthfulness/24-CONTEXT.md` — **D-01** (`classify_source` is the
  only rule; `UNKNOWN` keeps single-page routing, settled — do not re-open), **D-04**
  (`_MAX_ADF_PAGES` is per-`scan_pages` call, therefore naturally **per pass** — say so
  wherever duplex is written down), **D-08** (a partial integrity skip may break duplex parity;
  accepted and routed to `_handle_duplex_mismatch`), **D-12** (`ScanBatch` carries actual DPI
  and rejection count).
- `.planning/phases/23-honest-outcomes-and-never-lose-a-scan/23-CONTEXT.md` — **D-06** (the
  preservation guard spans upload+poll only, so a flip timeout raising before assembly
  preserves nothing, which is correct), **D-08** (the mismatch path has full
  outcome/warning/page-count parity — the basis for D-08 here), **D-20** (`.planning/UI-SPEC.md`
  is the design contract and is extended in-phase).
- `.planning/phases/21-vocabulary-and-contracts/21-CONTEXT.md` — **D-03** (the scanner package
  must not depend on the job vocabulary), **D-08** (the measured `match`/`assert_never` vs
  `dict` enforcement finding that D-07 and D-09 rest on), **D-09** (parametrise over
  `list(EnumType)`, never a hand-written list).
- `.planning/phases/20-ci-gate/20-CONTEXT.md` § "Hang guard" — `timeout = 60`,
  `filterwarnings = ["error"]`, `--strict-config` and `--strict-markers` are live. D-04 depends
  directly on the warning filter.
- `.planning/phases/23.1-dark-mode-and-the-commit-gate/23.1-CONTEXT.md` — **D-01/D-02** (type
  checks are `src`-only at commit and full at pre-merge-commit and pre-push, so a TDD RED commit
  needs no suppression), **D-10** (always run `uv run pyrefly check src tests`).

### Source files this phase changes
- `src/saneless/config.py` — `ProfileConfig` (`:63-80`) gains `duplex` and the before-validator;
  `OutputConfig` (`:82-128`) gains `flip_timeout_seconds`; `Settings.validate_default_profile`
  (`:177-187`) gains the named deprecation warning.
- `src/saneless/pipeline.py` — `_is_manual_duplex` (`:383-385`, deleted), `PipelineRequest`
  (`:110-135`, the two event fields become the coordinator), `_scan_manual_duplex`
  (`:668-735`), the dispatch at `:863-899`, and `PipelineEvent.job_state` (`:55-92`).
- `src/saneless/worker.py` — the duplicated rule (`:191-199`), `continue_flip`/`abort_flip`
  (`:98-109`), `wait_transition` (`:111-124`), `_status_cb` (`:215-238`), and every
  `_transition_event` touch.
- `src/saneless/vocabulary.py` — `JobState`, `ACTIVE_STATES`, `state_label`, `progress_label`;
  the likely home for `FlipOutcome`.
- `src/saneless/cli.py` — `scan` (`:105-156`), the `status_callback` and its `state is None`
  branch at `:126-133`, and the exit-code allocation.
- `src/saneless/web/routes.py` — `current_job_status` (`:179-198`), `continue_flip`
  (`:288-305`), `abort_flip` (`:307-325`).
- `src/saneless/scanner/sane_backend.py` — `_resolve_source` (`:792-847`) and the
  `ScanSettings`-derived signal; `src/saneless/scanner/base.py` — `ScanSettings` (`:171-179`).
- `src/saneless/auto_profiles.py` — `generate_profiles` (`:293-380`) for D-06.
- `src/saneless/web/templates/partials/status.html`, `partials/flip.html`, `index.html`.
- `tests/test_pipeline.py` (57 duplex refs), `tests/test_worker.py` (36),
  `tests/test_outcomes_e2e.py` (5, incl. `_DUPLEX_SOURCE = "Manual Duplex"` at `:97`),
  `tests/test_scanner.py`, `tests/test_browser.py:140`, `tests/test_vocabulary.py`,
  `tests/test_web_state_rendering.py`, `tests/fake_sane.py`.

### Docs whose claims this phase changes
- `docs/how-to/set-up-adf-duplex.md` — rewritten in-phase (the phase goal names it). `:52` and
  `:69` promise a CLI prompt that does not exist (doc row 1); `:56-67` teach
  `source = "Manual Duplex"` (doc row 2); `:64` documents the substring rule. D-18 removes the
  legacy form; D-08 adds the empty-page note.
- `docs/reference/web-api.md:124-136` — "Flip endpoints return HTML partial with updated job
  status"; Continue returns the unchanged flip prompt and abort may return "Ready to scan."
  (doc row 22, fixed by D-16/D-17).
- `docs/getting-started/first-web-ui-scan.md:42` — says to click **Cancel** and to place pages
  "face-up"; the button is "Abort scan" and the prompt says to flip the stack (doc row 24).
- `docs/how-to/cli-scripting.md` — enumerates the **eight** `JobState` values as "safe to
  compare against in a script"; a ninth state falsifies that list. The exit-code table also
  gains D-11's non-TTY case.
- `docs/reference/configuration.md:63` — the `source` row currently lists `ADF Manual Duplex`
  as a value; the new `duplex` key needs a row.
- `docs/explanation/architecture.md:28-33` and `:51` — the pipeline-stage list and the
  "worker blocks between pass A and pass B using a `threading.Event`" description.
- `.planning/UI-SPEC.md` — the status-area table (`:240-247`, which lists eight rows and omits
  `AWAITING_FLIP`) gains a `SCANNING_REVERSE` row; the humanized-label list (`:269`) gains
  "Scanning backs"; the htmx map (`:300-313`) reflects the flip-route changes. Note the `:197`
  summary claims "Nine presentations" — **re-check it against the table rather than trusting
  it.**

### External
- python-sane 2.9.2 at `.venv/lib/python3.14/site-packages/sane.py` — `SaneDev.__setattr__`
  (`:188-213`) reloads option descriptors on a source change, which is why D-11's source-first
  ordering in `_configure_device` is load-bearing.
- Click documentation — `pause()` is explicitly a no-op when not run through a terminal (the
  basis for D-11's disqualification); `confirm()` and `prompt()` raise `Abort` on interrupt;
  `CliRunner(input=...)` echoes prompts into output.
- Pydantic — `model_validator(mode="before")` receives the raw input dict before instantiation;
  `Field(deprecated=...)` warns on field *access*, which is why D-04 does not use it.

</canonical_refs>

<code_context>
## Existing Code Insights

### Verified facts (measured during discussion, 2026-09-13)

- `FEEDER_DUPLEX` occurs in `src/` **exactly three times**, all in `scanner/base.py`
  (`:35`, `:43`, `:91`). Nothing branches on it outside the enum's own definition.
- The jobs table is `state TEXT NOT NULL` (`job.py:289`) with **no `CHECK` constraint** — a
  ninth `JobState` needs no migration.
- `filterwarnings = ["error"]` is live, and `src/` and `tests/` contain **zero**
  `warnings.warn` / `DeprecationWarning` occurrences.
- `_DEFAULT_PAGE_TIMEOUT_SECONDS = 120.0` (`sane_backend.py:66`) and `_MAX_ADF_PAGES = 500`
  (`:100`) are the existing scan-side timeout/bound pattern: module constants used as default
  arguments, **not** config keys.
- `routes.py:297` is the **only** external reader of `_transition_event`.
- `cli.py` has no `isatty`, `click.confirm`, `click.prompt` or `input()` — no interactive read
  exists yet. Exit codes in use: 0, 1 (`ScanError`), 2 (config/profile), 3 (`PaperlessError`).
- `run_pipeline` never fetches device capabilities; `_resolve_device` calls `get_devices()`
  only when `settings.scanner.device` is empty.
- `scan_pages` opens and closes the device per call via `self._open_device(...)`, and
  acquisition is eager — **so the scanner is already released between pass A and pass B.**
  Criterion 3's "releases the scanner" is about unparking the single worker thread (M-07's
  actual complaint), not about dropping a held handle.
- Duplex-related references across the suite total ~219, concentrated in `tests/test_pipeline.py`
  (57) and `tests/test_worker.py` (36).

### Reusable Assets

- **`classify_source()` / `SourceKind` (`scanner/base.py:31-113`)** — already the only
  classification rule, already behind `match`/`assert_never`. D-02's feeder resolution consumes
  it; it is not rewritten.
- **`_resolve_source` (`sane_backend.py:792-847`)** already holds `raw_options` and already
  produces the "Device does not support source 'X'. Available: [...]" message shape that D-02's
  and the no-feeder refusal should reuse.
- **`ScanOutcome -> JobState` via `job_state_for` (`vocabulary.py`)** is the working precedent
  for `FlipOutcome`'s consumption: a total `match` with `assert_never` and a docstring
  explaining why it is not a dict.
- **`ScannerBackend` (ABC) vs `SaneDevice` / `_SettingsFactory` (Protocol)** is the observable
  house rule D-09 follows.
- **`current_job_status`'s "current job, else most recent" fallback (`routes.py:186-192`)** is
  the lookup D-17 extracts.
- **`httpx.MockTransport` via `_transport` and the `scan_batch(...)` helper in
  `tests/test_outcomes_e2e.py`** are the existing seams for driving the pipeline end to end;
  that module's `_build_scanner` already gives each pass its own page count via
  `side_effect`, which is how a duplex mismatch is simulated without real hardware.

### Established Patterns

- **Total lookups are functions with `match` + `assert_never`, never dicts** — measured in
  Phase 21 D-08 against this project's own `ty` and `pyrefly`.
- **Parametrise over `list(EnumType)`**, never a hand-written list, so a new member fails the
  suite by itself (Phase 21 D-09). This is how `SCANNING_REVERSE` is meant to be caught.
- **Optional profile keys are written to TOML only when non-default** (Phase 16) — the basis
  for D-06.
- **Fakes are concrete classes, not `MagicMock`**, and `tests/fake_sane.py` is imported as
  `from tests.fake_sane import ...` (the bare form raises `ModuleNotFoundError` — Phase 24).
- **TDD is enabled** (`workflow.tdd_mode: true`); since Phase 23.1 a RED commit lands with hooks
  enabled and **no suppression of any kind**.
- **Ruff `D` rules are on**; every public module, class and function needs a docstring.
  `_handle_duplex_mismatch` is already at `PLR0913`'s argument ceiling — do not add parameters
  to it.
- **Always `uv run pyrefly check src tests`** — never the bare form.

### Integration Points

- `run_pipeline`'s opening sequence (profile lookup → `_resolve_device` → `ScanSettings`) is
  where the coordinator refusal and the `duplex` branch land.
- `worker._process_job` (`:171-199`) constructs the coordinator instead of two events;
  `_status_cb` (`:215-238`) loses its `state is None` branch once `SCANNING_REVERSE` persists.
- `cli.scan`'s `status_callback` is where the CLI coordinator prompts — the review notes
  `run_pipeline` is synchronous and the callback runs on the same thread, which is what makes
  prompting from inside it correct.
- `status.html` gains a `SCANNING_REVERSE` presentation; the flip partial stays bound to
  `AWAITING_FLIP` only, which is what makes D-16 work.

</code_context>

<specifics>
## Specific Ideas

- **Roadmap success criterion 2 must be amended.** It states that `saneless scan` 'prompts
  "Flip the stack and press Enter" on stdin'. D-11 uses `click.confirm` plus a non-TTY refusal,
  because `click.pause` — the only API matching that wording literally — is a documented no-op
  off a terminal and would reintroduce C-02 invisibly. The criterion should describe a prompt
  that blocks until the operator answers, not the specific keystroke.
- **The user chose the conservative branch twice on device honesty and the aggressive branch on
  documentation.** Feeder sources are resolved from the device rather than guessed (D-02), the
  `Auto` fallback is forbidden for manual duplex, and a flatbed-only device must fail rather
  than snapshot the platen — while the legacy config *form* is removed from the docs outright
  (D-18) even though it keeps working. Read it as: be cautious with the operator's paper, and
  decisive about what the documentation teaches.
- **D-18 was chosen against a recommendation** to keep a deprecation note. The accepted cost is
  that the warning message becomes the sole migration instruction, which is why D-04's message
  content is now a binding constraint rather than a detail.
- **DPLX-07 looks already satisfied and should be verified, not rebuilt.** Phase 24 rewrote
  `generate_profiles` so a `default` profile is emitted whenever the device reports any source,
  backed by a flatbed or by the device's first reported source, with the `"default"` slug
  reserved up front to prevent a colliding source from stealing it. The docstring says as much
  and names `Settings.validate_default_profile` as the reason. **Expect DPLX-07 to reduce to
  the missing write-then-load round-trip test across flatbed-only, feeder-only and mixed
  devices.** If the planner finds emission logic genuinely missing, that is a finding worth
  raising loudly.
- The standing verification-rigor rule was applied throughout: the `FEEDER_DUPLEX` reader count,
  the absence of a `CHECK` constraint, the warning-filter setting, and `click.pause`'s non-TTY
  behaviour were each read from the tree or the vendor documentation rather than assumed. The
  pydantic-settings validator-ordering question was explicitly classified as *the library's own
  contract* and marked as not worth a test.

</specifics>

<deferred>
## Deferred Ideas

Nothing outside the phase domain was proposed. Recorded below are the boundary clarifications
made while deciding — work this phase deliberately does **not** do, so a planner does not pull
it forward and a verifier does not fail the phase for its absence.

- **`ScanCancelledError`, a `CANCELLED` job state, and not logging an abort at ERROR level** —
  Phase 28 (EXC-04, N-08). D-15 stops at a clear message deliberately.
- **Aborting during pass B rather than at the prompt** — Phase 29 (HARD-02). Requires lazy page
  consumption so the abort can be observed between pages. D-16 removes the misleading control
  instead.
- **A `wait_for_state` polling helper for worker tests** — Phase 26 (ROBU-03), which owns the
  conversion of tests that assumed a draining `stop()`.
- **Owner-only flip prompt, Abort confirmation, and front/back page counts during pass B** —
  Phase 30 (APPL-09, APPL-03). D-05's declarative `"hardware"` value is groundwork for
  APPL-05's generated labels.
- **`extra="forbid"` covering the new `duplex` and `flip_timeout_seconds` keys, and
  `--force` merge semantics for `duplex`** — Phase 27 (CFG-01, CFG-07).
- **Converting the flip routes to plain `def` handlers and adding 429 backpressure** — Phase 26
  (ROBU-02, ROBU-05), even though D-17 edits the same three routes.
- **Filtering blank pages on the duplex-mismatch path** — declined outright by D-08, not
  deferred. Recorded so it is not re-raised as an open question.
- **Hardcoding `"ADF"` as the translated device source** — declined outright by D-02.

</deferred>

---

*Phase: 25-manual-duplex*
*Context gathered: 2026-09-13*
