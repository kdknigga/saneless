# Phase 21: Vocabulary and Contracts - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-09-10
**Phase:** 21-vocabulary-and-contracts
**Areas presented:** Enum unification, Module home + re-exports, Template + JS access, Future members now?, classify_source wiring, ErrorCategory visibility, Docs rewrite scope, Enforceability + tests

---

## Session note

The session opened blocked: the repository was mid-`git rebase -i` (1 of 248 commits replayed, conflict on `docs/PRD.md`, detached HEAD), so `.planning/ROADMAP.md` did not exist in the working tree and `gsd-sdk query init.phase-op 21` returned `phase_found: false`. The user aborted the rebase, restoring branch `autodev` and the full planning tree, and the workflow resumed from `initialize`.

---

## All eight areas — delegated

Two multi-select questions were presented, covering eight gray areas in two clusters.

### Question 1 — "Structure" (shape of the vocabulary)

| Option | Description | Selected |
|--------|-------------|----------|
| Enum unification | One enum, or two with a mapping? `PipelineEvent` has `SCANNING_REVERSE` with no `JobState` twin; `JobState` has `PENDING`/`DONE`/`ERROR` the pipeline never emits. | |
| Module home + re-exports | New `vocabulary.py`/`states.py`, or keep the enums in `job.py`? Phases 22, 23, 25, 30 all import from whatever this becomes. | |
| Template + JS access | How `status.html`, `index.html`, `app.js` stop hard-coding state string literals. | |
| Future members now? | Does 21 define the complete final vocabulary including `FALLBACK` and `SCANNING_REVERSE`, or only what exists today? | |

**User's choice:** *(free text)* "All good, no need to discuss. I trust you; don't let me down."

### Question 2 — "Boundaries" (what behaviour, if any, changes)

| Option | Description | Selected |
|--------|-------------|----------|
| classify_source wiring | Criterion 2 wants one classification rule, but wiring it fixes C-06 — Phase 24's headline finding. Does the behaviour fix land here or there? | |
| ErrorCategory visibility | Build the message map and wire it to the UI, or define it and leave display to Phase 30 (U-05)? | |
| Docs rewrite scope | `consume-directory-fallback.md:66` claims a `FALLBACK` job status that will not exist until Phase 23. Correct, pre-write, or delete? | |
| Enforceability + tests | How much is carried by the type checkers vs runtime parametrised tests, and what proves "no behaviour change"? | |

**User's choice:** *(free text)* "Whatever you think is best. Please don't disappoint me."

**Notes:** Full delegation of both clusters. No area was discussed interactively; all eight were decided by Claude and written into CONTEXT.md as locked decisions D-01..D-13, each carrying its rationale and, where a plausible alternative was rejected, the reason for rejecting it.

---

## Claude's Discretion

Every area in this phase. Decisions and their rationale are in `21-CONTEXT.md` § Implementation Decisions:

| Area | Decision | Where |
|---|---|---|
| Enum unification | Two enums kept; `PipelineEvent.job_state` property via `match`/`assert_never` | D-01 |
| Module home | New `src/saneless/vocabulary.py`; `job.py` re-exports; `SourceKind` stays in `scanner/base.py` | D-02, D-03 |
| Template access | `ACTIVE_STATES`/`TERMINAL_STATES` partition + derived `BUSY_STATES`; `Job.is_active`/`is_busy` | D-04 |
| Result type homes | `ScanResult` in `pipeline.py`, `UploadResult` in `paperless.py`, `ScanOutcome` in `vocabulary.py` | D-05 |
| Future members | No new `JobState` member in 21; `ScanOutcome` ships `SUCCESS`+`FALLBACK` only | D-06, D-07 |
| Enforceability | `match`+`assert_never` for total lookups; parametrised tests for the frozensets | D-08, D-09, D-10 |
| classify_source | One rule, wired; the C-06 routing correction lands here as an accepted behaviour change; `UNKNOWN` routing unchanged | D-11 |
| ErrorCategory | Map defined and made total, but not wired to the UI | D-12 |
| Docs | Only `consume-directory-fallback.md:66`; the sentinel is banned, the English word is not | D-13 |

---

## Findings that changed a decision

Two claims in the code review were checked against the tree and found wrong for this codebase. Both are recorded in CONTEXT.md so they are not restored from the review text during planning.

| Claim (review § M-05) | Check performed | Result |
|---|---|---|
| "Type the label map as `dict[JobState, str]` so ty and pyrefly flag a missing member" | Wrote a `StrEnum` with a `dict[S, str]` literal missing one member, ran both checkers from the repo root | **False.** Neither checker emits a diagnostic. The same enum in a `match` with `assert_never` is caught by both (`ty`: `type-assertion-failure`, inferred `Literal[S.C]`; `pyrefly`: `bad-argument-type`). Drove D-08. |
| "the JavaScript implies it again" (`static/app.js` duplicating the active-state list) | Grepped `app.js` for state names | **False.** No state strings; the file is HTMX-lifecycle driven. One fewer site to change. |

Additionally, the duplication is **worse** than M-05 documents: four label vocabularies rather than three (`status.html:9-19` re-spells the CLI's progress prose as an `if/elif` chain), and three active-state lists that are not the same list — `index.html:59` excludes `AWAITING_FLIP` and is really `BUSY_STATES`. This drove D-04.

---

## Deferred Ideas

Recorded in full in `21-CONTEXT.md` § Deferred Ideas:

- `JobState.FALLBACK` and its rendering — Phase 23
- `ScanOutcome.FAILED` — Phase 23
- `JobState.SCANNING_REVERSE` — Phase 25
- The duplicated `"manual" in source and "duplex" in source` rule (`pipeline.py:96`, `worker.py:207`) — Phase 25, which deletes it rather than centralising it
- Routing `SourceKind.UNKNOWN` to the multi-page path — Phase 24
- Wiring `error_message()` into the UI and CLI — Phase 30
- Scan button via a single include with `hx-swap-oob` — Phase 26
- `poll_task` raising on `FAILURE`/timeout — Phase 23
- `source_to_slug` slug collisions (N-09) — unassigned; flag during Phase 24 planning
