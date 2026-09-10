# Phase 21: Vocabulary and Contracts - Context

**Gathered:** 2026-09-10
**Status:** Ready for planning

<domain>
## Phase Boundary

Every word the system uses about itself exists exactly once and is enforceable by ty and pyrefly: one `JobState`, one active/terminal classification, one short-label map, one progress-prose map, one `ErrorCategory` with one user-message map, one `classify_source()` returning `SourceKind`, and typed `ScanResult` / `UploadResult` replacing the `-> dict` and the `"fallback"` sentinel.

Requirements: **CTR-01**, **CTR-02**, **CTR-03**, **CTR-04**, **CTR-05**.

**In scope:** a new `src/saneless/vocabulary.py`; `JobState`/`ErrorCategory` re-exported from `job.py`; `SourceKind` + `classify_source()` in `scanner/base.py`; `ScanResult`/`ScanOutcome` from `run_pipeline`; `UploadResult` from `upload_document`; the two templates, `cli.py`, `web/app.py`, and `worker.py` rewired to read from the single source; the parametrised completeness tests; and the one doc sentence that claims a `FALLBACK` job status exists today.

**Out of scope:** `JobState.FALLBACK` (Phase 23), `JobState.SCANNING_REVERSE` (Phase 25), `poll_task` raising on `FAILURE`/timeout (Phase 23), PDF preservation (Phase 23), the `RLock` and migration ladder (Phase 22), plain-language error display (Phase 30), the Scan-button `hx-swap-oob` rework (Phase 26), and the `_is_manual_duplex` string-sniffing duplication (Phase 25 deletes it rather than centralising it).

**Behaviour change:** exactly one, deliberate and documented — see **D-11**. Everything else is a pure refactor and the existing 332-test suite is the evidence.

</domain>

<decisions>
## Implementation Decisions

The user delegated every gray area in this phase ("All good, no need to discuss. I trust you"; "Whatever you think is best"). Every decision below is Claude's, made against the roadmap's dependency spine and verified against the tree. They are locked for planning: a planner may implement them, not relitigate them.

### The shape of the vocabulary

- **D-01: `JobState` and `PipelineEvent` stay two enums; the mapping between them becomes total and machine-checked.** They are different concepts with different lifetimes — `JobState` is persisted as SQLite `TEXT` and read back through `JobState(row[3])` (`job.py:187`, `:262`); `PipelineEvent` is a transient progress notification emitted by a function that knows nothing about persistence. `JobState` owns `PENDING` and `ERROR`, which the pipeline never emits; `PipelineEvent` owns `SCANNING_REVERSE`, which has no persisted twin until Phase 25.
  - Add `PipelineEvent.job_state -> JobState | None`, implemented as a `match` with `assert_never`. `None` means "this event changes no persisted state" — today that is exactly `SCANNING_REVERSE`.
  - This is M-05's "give `PipelineEvent` a `job_state` property" branch, chosen over its "use one enum" branch. Collapsing them would force `SCANNING_REVERSE` into the persisted vocabulary — Phase 25's call (M-02) — and would put `PENDING`/`ERROR` in the pipeline's mouth.

- **D-02: the shared vocabulary lives in a new `src/saneless/vocabulary.py`.** `job.py` re-exports `JobState` and `ErrorCategory` and keeps both in its `__all__`, so every existing `from saneless.job import JobState` keeps resolving (CTR-01's explicit requirement, and success criterion 4). Rationale for a new module over keeping them in `job.py`: `job.py` is the SQLite module and Phase 22 rebuilds it around an `RLock` and a migration ladder; the CLI and the templates should not import the persistence layer to ask what a state is called. `vocabulary.py` must import nothing from `job.py` — the dependency runs one way only.
  - Name chosen to match the phase and the roadmap's own language. `states.py` is too narrow (the module also owns `ErrorCategory` and `ScanOutcome`); `contracts.py` is wrong because the result types live with their producers (D-05).

- **D-03: `SourceKind` and `classify_source()` go in `scanner/base.py`, not `vocabulary.py`.** CTR-04 names that location, and the scanner package must not depend on the job vocabulary.

- **D-04: three classifications of `JobState`, not two, and only two of them are hand-written.**
  - `ACTIVE_STATES: frozenset[JobState]` and `TERMINAL_STATES: frozenset[JobState]` are written out and must partition `set(JobState)` exactly — that is the reading of success criterion 1's "appears in exactly one active-state list": every member classified once, none in both, none missing.
  - `BUSY_STATES` is **derived** (`ACTIVE_STATES - {JobState.AWAITING_FLIP}`), never hand-written. It exists because `index.html:59` already needs it and the codebase currently spells it out as a fourth literal list. "The machine is working" and "we are waiting for the human" are genuinely different, and the button text and `aria-busy` depend on the difference.

- **D-05: result types live with their producers.** `ScanResult` in `pipeline.py` beside `PipelineRequest` (symmetric request/result pair); `UploadResult` in `paperless.py` beside `upload_document`. Only `ScanOutcome` goes in `vocabulary.py`, because Phase 23 maps it onto `JobState` and it is a word the system uses about itself.

### Which members land now

- **D-06: Phase 21 adds no new `JobState` member.** Not `FALLBACK`, not `SCANNING_REVERSE`.
  - A `JobState.FALLBACK` that nothing can write would need a label, an active/terminal classification, and a row in the criterion-1 parametrised test — a test asserting a label for a state the system cannot reach. That is dead vocabulary, the exact opposite of this phase's purpose.
  - Phase 23's success criterion 2 is "records the job as `FALLBACK` ... rendered distinctly in the status area, the history table, and `saneless jobs`". If 21 ships the member and the rendering, 23's criterion is half-satisfied in the wrong phase and the roadmap's "its own tests could not have passed before it" discipline breaks.
  - The payoff of this phase is that adding a state becomes a one-place edit. The way to prove that is to let Phase 23 make it.

- **D-07: `ScanOutcome` ships with `SUCCESS` and `FALLBACK` only; `FAILED` is Phase 23's.** Both members shipped here are reachable today — a polled task that succeeds, and a PDF that only reached the consume directory. `FAILED` is not reachable today (failures raise) and may never be a *returned* outcome, because C-03's prescribed fix is that `poll_task` **raises** `PaperlessError` on `FAILURE` and on timeout. Defining `FAILED` now would prejudge the decision Phase 23 exists to make.
  - CTR-02's wording names all three. It is satisfied here for **shape** — `run_pipeline` returns a typed `ScanResult` with an outcome enum instead of a bare `dict` — and completed by Phase 23, which adds `FAILED` together with the code path that produces it, if it produces one. Phase 21's success criterion 3 asks only for the typed result and the sentinel's removal, which matches this split.

### Enforceability — what the type checkers can and cannot carry

- **D-08: a `dict[JobState, str]` gives ZERO compile-time enforcement. M-05's suggested fix is wrong on this point and must not be copied.** Measured during discussion against this project's own toolchain: a `dict[S, str]` literal missing a member produces **no diagnostic** from either `ty` or `pyrefly`. The same enum in a `match` with `assert_never` is caught by **both** — `ty`: `error[type-assertion-failure] ... Inferred type of argument is Literal[S.C]`; `pyrefly`: `ERROR Argument Literal[S.C] is not assignable to parameter arg with type Never [bad-argument-type]` (pyrefly needs the project config to report it; run it from the repo root).
  - Consequence: **every lookup that must be total is a function using `match` + `assert_never`, not a dict.** That covers `PipelineEvent.job_state`, `state_label()`, `progress_label()`, and `error_message()`. A missed member then fails the Phase 20 gate at edit time, which is what "enforceable by the type checkers" in the phase goal has to mean.

- **D-09: the parametrised tests are the enforcement for the frozensets, and they are required regardless.** `@pytest.mark.parametrize("state", list(JobState))` asserting (a) `state_label(state)` returns a non-empty label that is not merely the raw member name, and (b) `(state in ACTIVE_STATES) ^ (state in TERMINAL_STATES)`. Same shape for `ErrorCategory` × `error_message`. Parametrising over `list(JobState)` — never a hand-written list of names — is what makes a new member fail the suite automatically.

- **D-10: `test_humanize_state_filter_unit` must change, deliberately.** `tests/test_web.py:378` asserts `humanize_state("UNKNOWN") == "UNKNOWN"`. M-05 names this test specifically as pinning the drift rather than preventing it. Replace the raw-fallback assertion with one that an unrecognised value raises. This is safe at runtime: `Job.state` is always a `JobState` because `job.py:187`/`:262` construct it through `JobState(row[3])`, which already rejects anything else.

### The one behaviour change

- **D-11: `classify_source()` becomes the only source-classification rule, and the resulting C-06 routing correction lands here — intentionally.** Success criterion 2 is unconditional: "it is the only classification rule in the codebase." Today two rules disagree — `sane_backend._is_adf_source` (`:158-160`) tests `"adf" in source.lower()`, while `auto_profiles.source_to_slug` (`:47-58`) also recognises `"document feeder"` and `"feeder"`. One rule cannot produce two answers, so satisfying the criterion necessarily changes what `sane_backend` does with `"Automatic Document Feeder"`: it starts taking the `multi_scan()` path and returns all the pages instead of one.
  - This is the C-06 fix and the phase goal says "no behaviour change", so it is called out here explicitly: **the planner and the verifier must treat this as planned work, not as an execution deviation.** It needs its own test (a feeder named `"Automatic Document Feeder"` yields N pages, not 1) — a test that could not have passed before, exactly as the roadmap's discipline requires.
  - **`SourceKind.UNKNOWN` keeps today's routing** — it falls to the single-page branch. C-06 also floats a safer default ("treat anything that is not the flatbed entry as multi-page"); that is a real bet about unseen scanners and it stays **Phase 24's** call. Confining this phase's change to names that are unambiguously feeders is what keeps it defensible.
  - Phase 24 still owns: wiring the classifier into the remaining sites, the `auto_source_mode` routing at `sane_backend.py:496-504`, real SANE error messages, geometry and read-back DPI, and fakes that model real python-sane.

### What stays invisible to users

- **D-12: `ErrorCategory` gets its single message map in this phase but is NOT wired to the UI.** CTR-05's "no template, route, or CLI output classifies errors by string matching" is **already true today** — nothing string-matches; `_categorize_error` uses `isinstance`. The live content of CTR-05 is therefore: move `ErrorCategory` into `vocabulary.py`, move `_categorize_error` off `ScanWorker` (it never touches `self`) to a module-level `classify_error(exc) -> ErrorCategory`, and define one `error_message()` behind `match`/`assert_never`.
  - The status partial keeps rendering `job.error` verbatim. Swapping in a generic category message now would *replace* today's specific error text with something vaguer, in the window before Phase 24 makes the underlying messages truthful — a user-visible regression. U-05's plain-language display is Phase 30's, and it depends on both Phase 24 and Phase 23 landing first.
  - `error_message()` may therefore have no production caller in this phase; its parametrised completeness test is a sufficient consumer. Wiring it into the worker's existing failure log line is acceptable if the planner wants a real reader, provided no test asserts that log text.

### Docs

- **D-13: success criterion 3's "the `"fallback"` magic string is absent from `docs/`" means the *sentinel*, not the English word.** The consume-directory fallback is a real feature whose name appears in `docs/explanation/consume-directory-fallback.md` (the filename itself), `docs/index.md:42`, `docs/explanation/architecture.md`, and `docs/reference/docker.md`. Deleting the feature's name from the documentation is not what the criterion asks for.
  - The one doc statement in scope is **`docs/explanation/consume-directory-fallback.md:66`** — "**The job status shows FALLBACK.**" That is false today (`JobState` has no such member — C-03 says so explicitly) and stays false until Phase 23. Rewrite it in this phase to describe what actually happens now; the same section already states the true consequence at `:62` (only the PDF is saved; title, tags, and correspondent are not applied). Phase 23 rewrites it again to the new truth when the state exists.
  - **Verification note for the planner:** a naive `grep -rn fallback docs/` will fail forever and does not test the criterion. Verify the sentinel instead — `grep -rn '"fallback"' src/ tests/` returning nothing — plus a targeted check that no doc claims a `FALLBACK` job status.

### Claude's Discretion

The user delegated the whole discussion, so D-01..D-13 are already discretionary calls; they are nonetheless **locked**. The following are left genuinely open to the planner:

- The exact prose of every label, subject to one hard constraint: **the strings users see today must not change.** `state_label` must keep `"Pending" / "Scanning" / "Waiting for flip" / "Assembling" / "Uploading" / "Complete" / "Failed"` (`web/app.py:36-42`), and `progress_label` must keep `"Starting scan..." / "Scanning..." / "Awaiting flip..." / "Assembling PDF..." / "Uploading to paperless-ngx..."` (`status.html:9-19`, `cli.py:111-115`) with their literal three-dot spelling.
- Whether `state_label` and `progress_label` are two functions or one function with a mode argument.
- Whether the Jinja filter keeps the name `humanize_state` (which `web/app.__all__` exports and `tests/test_web.py:36` imports) or is renamed with the call sites updated.
- Task and commit breakdown, provided `vocabulary.py` plus the `job.py` re-exports land before anything that imports them.
- Whether `classify_error` keeps a thin `ScanWorker._categorize_error` delegate for test compatibility or the tests move to the module-level function.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### The findings this phase resolves
- `.planning/reviews/2026-09-09-code-review.md` § M-05 (~line 427) — the finding behind CTR-01. Lists the duplication sites. **Its "Type the label map as `dict[JobState, str]` so ty and pyrefly flag a missing member" is measurably wrong — see D-08.** Its `hx-swap-oob` suggestion is Phase 26's, not this phase's.
- `.planning/reviews/2026-09-09-code-review.md` § C-03 (~line 201) — the finding behind CTR-02 and CTR-03. Read fix items 2 and 4 (typed `UploadResult`, kill the `-> dict`), which are this phase's. **Fix items 1, 3, and 5 — `poll_task` raising, `JobState.FALLBACK`, worker-level outcome tests — are Phase 23's; see D-06 and D-07.**
- `.planning/reviews/2026-09-09-code-review.md` § C-06 (~line 270) — the finding behind CTR-04, including the confirmed real-world feeder names and the experiment against the SANE `test` backend (ten-page feeder yielded one page). Its "safer default" for unrecognised sources is Phase 24's — see D-11.
- `.planning/reviews/2026-09-09-code-review.md` § N-38 (~line 921) — stringly-typed protocols defeating the type checkers.
- `.planning/reviews/2026-09-09-code-review.md` § N-09 (~line 855) — slug collisions in `source_to_slug`.
- `.planning/reviews/2026-09-09-code-review.md` § N-14 (~line 867) — `error_category` is written but never read; the basis for D-12.
- `.planning/reviews/2026-09-09-code-review.md` § U-05 (~line 1157) — raw exception text shown to family members. **Phase 30, not this phase.**
- `.planning/REQUIREMENTS.md` § "Contracts and Vocabulary" (lines 18-22) — CTR-01..CTR-05 verbatim.

### Phase and milestone framing
- `.planning/ROADMAP.md` § "Phase 21: Vocabulary and Contracts" — goal and the four success criteria.
- `.planning/ROADMAP.md` § Overview, ordering points 2 and 3 — why the vocabulary lands before the migration ladder and before typed results, and why scanner truthfulness is a separate later phase.
- `.planning/ROADMAP.md` § "Phase 23: Honest Outcomes and Never Lose a Scan" — read it to see exactly what D-06 and D-07 are holding back, and its note that the preservation `try/except` must span both `upload_document` and `poll_task`.
- `.planning/ROADMAP.md` § "Phase 24: Scanner Truthfulness" and § "Phase 25: Manual Duplex" — the boundary D-11 and the deferred `_is_manual_duplex` work sit against.
- `.planning/PROJECT.md` § "Current Milestone: v2.0 Prep for release" — milestone framing and the "No git tags" constraint.

### Standing constraints from Phase 20
- `.planning/phases/20-ci-gate/20-CONTEXT.md` § "Toolchain and supply chain" (D-16) and § "Established Patterns" — both type checkers are mandatory and equal; findings are fixed, never suppressed with `# type: ignore` or `# noqa`. `ty` and `pyrefly` are now at current versions. This governs how D-08's `assert_never` pattern must be applied.
- `.planning/phases/20-ci-gate/20-CONTEXT.md` § "Hang guard" — `timeout = 60` and `timeout_method = signal` are live in `pyproject.toml`; `filterwarnings = ["error"]`, `--strict-config`, and `--strict-markers` are in force, so any new import that warns turns the suite red.
- `CONTRIBUTING.md` — the five checks every change must pass.

</canonical_refs>

<code_context>
## Existing Code Insights

### The duplication, as measured (not as described)

M-05 undercounts. The tree currently holds:

- **Two enums:** `JobState` (`job.py:26-33`, 7 members) and `PipelineEvent` (`pipeline.py:37-45`, 6 members). Overlap is 5; `JobState` adds `PENDING`/`ERROR`, `PipelineEvent` adds `SCANNING_REVERSE`.
- **A hand-written mapping:** `worker.py:222-233`, an `if/elif` chain over four events inside `_status_cb`. `SCANNING` is set before the pipeline runs (`:203`) and `DONE` after it returns (`:257`), so they never pass through the chain.
- **Four label vocabularies, in two different kinds:**
  1. `web/app.py:36-42` `_STATE_LABELS` — short labels, keyed by **raw `str`**, with a silent raw-value fallthrough at `:48`. Used by the history table (`partials/history.html:8`).
  2. `cli.py:111-115` `_event_labels` — progress prose, keyed by `PipelineEvent`.
  3. `partials/status.html:9-19` — the **same progress prose as (2)**, re-spelled as an `if/elif` chain over `job.state.value` string literals.
  4. `index.html:59` — button text, a third spelling.
- **Three active-state lists, and they are not the same list:** `status.html:1` (5 members, includes `AWAITING_FLIP`), `index.html:58` (5 members, includes `AWAITING_FLIP`), and `index.html:59` (4 members, **excludes** `AWAITING_FLIP`). The third is `BUSY_STATES` — see D-04.
- **`static/app.js` does NOT hard-code state strings.** M-05 says "the JavaScript implies it again"; it does not. The file is HTMX-lifecycle driven (`app.js:1`). One fewer site to change, and the Scan-button rework stays Phase 26's.
- **Two disagreeing source classifiers:** `sane_backend.py:158-160` vs `auto_profiles.py:47-58`. See D-11.
- **A third, unrelated duplicated rule:** `"manual" in source and "duplex" in source` appears at `pipeline.py:96` and again inline at `worker.py:207`. **Deferred to Phase 25** — see Deferred Ideas.

### The sentinel, end to end
- `paperless.py:155` — `return "fallback"` after retries are exhausted and the PDF is copied to the consume directory. Documented as a return value at `:87-88`.
- `pipeline.py:432` — `if task_uuid != "fallback":` chooses between polling and synthesising `{"status": "FALLBACK", "path": ...}`.
- `pipeline.py:310` — `-> dict`; `:386` and `:443` are the two return sites. **Neither caller reads it:** `worker.py:250-256` discards it, `cli.py:132-137` discards it.
- Tests asserting the sentinel: `tests/test_paperless.py:231`, `:425`, `:452`.
- `docs/explanation/consume-directory-fallback.md:66` — the false `FALLBACK` status claim (D-13).

### Integration points
- New file `src/saneless/vocabulary.py` — the phase's primary artifact.
- `src/saneless/job.py` — enums move out, re-exports move in; `__all__` at `:19` stays as-is. `Job` gains `is_active` / `is_busy` properties.
- `src/saneless/pipeline.py` — `PipelineEvent.job_state`, `ScanResult`, the `-> dict` deleted, the sentinel comparison replaced.
- `src/saneless/paperless.py` — `UploadResult`, the `return "fallback"` at `:155`, and the docstring at `:87-88`.
- `src/saneless/worker.py` — `_status_cb`'s chain collapses; `_categorize_error` moves out.
- `src/saneless/cli.py` — `_event_labels` deleted, `status_callback` reads the shared vocabulary.
- `src/saneless/web/app.py` — `_STATE_LABELS` deleted, the filter re-backed.
- `src/saneless/web/templates/partials/status.html`, `index.html` — literal lists and the prose chain replaced.
- `src/saneless/scanner/base.py` — `SourceKind` + `classify_source()`; `sane_backend.py:158-160` and `auto_profiles.py:47-58` delegate to it.
- `docs/explanation/consume-directory-fallback.md` — line 66 only.

### Ordering the refactor must respect
- **`worker.py:222-233` clears `_transition_event` *before* `update_state(AWAITING_FLIP)` and sets it *after* `update_state` for `ASSEMBLING`/`UPLOADING`.** The web UI polls status every second (`status.html:4`), so this window is observable. Preserve the clear-before / set-after ordering exactly when the chain is replaced — this is a concrete instance of what "no behaviour change" means here.
- `vocabulary.py` must not import `job.py`, `pipeline.py`, or anything in `web/`. Dependencies run one way.

### Verified baseline (measured during discussion, 2026-09-10)
- Working tree clean on branch `autodev`; an interactive rebase that was in progress at the start of this session was aborted by the user, restoring the full `.planning/` tree.
- Type-checker exhaustiveness experiment: see D-08. `dict` → no diagnostic from either checker; `match` + `assert_never` → caught by both.
- Phase 20 baseline still stands: 332 tests pass in ~27s with `-m "not browser"`, 8 deselected.

</code_context>

<specifics>
## Specific Ideas

- The user delegated every decision in this phase without qualification ("I trust you; don't let me down"). The corresponding obligation is that D-01..D-13 each carry the reason they were chosen and, where a plausible alternative was rejected, why. A planner that disagrees with one should raise it rather than silently implement the other branch.
- Two of the review's own prescriptions were checked and found wrong for this codebase, and both are recorded so they are not restored from the review text: **M-05's `dict[JobState, str]` enforcement claim** (D-08, measured false against this project's `ty` and `pyrefly`) and **M-05's claim that `static/app.js` duplicates the state list** (it does not). This mirrors Phase 20's D-08/D-09/D-10, where the review's literal wording was also re-derived from what is genuinely uncertain.
- The phase goal's "no behaviour change" survived scrutiny with exactly one exception (D-11), and that exception is forced by success criterion 2 rather than chosen. It is flagged in three places here so neither the planner nor the verifier reads it as drift.

</specifics>

<deferred>
## Deferred Ideas

- **`JobState.FALLBACK` + its label, classification, and distinct rendering in the status area, history table, and `saneless jobs`** — Phase 23 (OUTC, success criterion 2). D-06.
- **`ScanOutcome.FAILED`** — Phase 23, added with the code path that produces it, if `poll_task` raising leaves one. D-07.
- **`JobState.SCANNING_REVERSE`** — Phase 25 (M-02, visible reverse pass). Until then `PipelineEvent.SCANNING_REVERSE.job_state` is `None` and the CLI keeps one explicit case for its prose; that case collapses when Phase 25 adds the member. This phase makes the seam typed and visible instead of hidden inside an `if/elif` chain.
- **The duplicated `"manual" in source and "duplex" in source` rule** (`pipeline.py:96`, `worker.py:207`) — Phase 25. Deliberately not centralised here: Phase 25 introduces a `duplex` profile field (DUPX-01) that deletes the string-sniffing entirely, so consolidating it now is work Phase 25 immediately throws away.
- **Routing `SourceKind.UNKNOWN` to the multi-page path** (C-06's "safer default") — Phase 24. A real bet about unseen scanners; it deserves that phase's scanner research. D-11.
- **Wiring `error_message()` into the status partial, history table, and CLI** — Phase 30 (U-05). Depends on Phase 24 making the underlying messages truthful and Phase 23 adding `FALLBACK`. D-12.
- **Rendering the Scan button from a single include with `hx-swap-oob`** (M-05's last suggestion) — Phase 26, with the C-10 fix and the front-end vendoring.
- **`poll_task` raising `PaperlessError` on `FAILURE` and on timeout, and returning something typed** — Phase 23. This phase leaves `poll_task`'s `-> dict[str, object]` alone; only `upload_document` gets a typed result here.
- **`source_to_slug` collision handling** (N-09) — the classifier lands here, but slug collisions dropping scanner sources is a separate defect. Not assigned to a phase by the roadmap; flag it during Phase 24 planning.

</deferred>

---

*Phase: 21-Vocabulary and Contracts*
*Context gathered: 2026-09-10*
