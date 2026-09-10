# Phase 21: Vocabulary and Contracts - Research

**Researched:** 2026-09-10
**Domain:** Python 3.14 enum modelling, exhaustiveness checking under `ty` + `pyrefly`, Jinja2 template contracts, SANE source-string classification, mechanical-refactor sequencing
**Confidence:** HIGH (every load-bearing claim below was executed against this repository's own toolchain on 2026-09-10)

---

## Summary

This phase is a pure-refactor with one deliberate behaviour change (D-11). Everything the planner needs falls into four buckets, and three of them were settled by running experiments rather than by reading documentation.

**First, the enforcement mechanism has an exact shape, and the obvious spelling of it fails CI.** CONTEXT.md D-08 already established that `dict[JobState, str]` gives zero enforcement while `match` + `assert_never` is caught by both checkers. That reproduces. But the naive way to write a total function over the 7-member `JobState` — a `match` with seven `return` statements — trips **ruff `PLR0911` (too many return statements, 7 > 6)**, which is enabled in this project and which the project rule forbids suppressing. The form that satisfies ruff, `ty`, and `pyrefly` simultaneously is assign-in-each-arm plus a single trailing `return`, with `case _: assert_never(state)` as the last arm. Additionally, `ty` and `pyrefly` **disagree** about which pattern syntaxes narrow a `StrEnum`: raw string patterns (`case "DONE":`), `.value` patterns (`case JobState.DONE.value:`), and guarded arms (`case X if cond:`) all narrow under `ty` but **not** under `pyrefly`, so all three are unusable here. Only bare member patterns, or-patterns, and `is`/`==` if-chains work under both.

**Second, the refactor's blast radius on the test suite was measured, not estimated.** Simulating the `upload_document -> UploadResult` signature change breaks **18 tests** across `tests/test_pipeline.py` (17) and `tests/test_cli.py` (1), plus 3 more in `tests/test_paperless.py` that assert the sentinel directly. There are **five** independent `upload_document` stub sites in `tests/`, not one. This is the single largest atomicity constraint in the phase and it is directly analogous to Phase 20's `20-01` lesson.

**Third, `classify_source()` has one pitfall that would silently reintroduce the very bug it fixes**: `"Automatic Document Feeder".lower()` **contains the substring `"auto"`**. A classifier that tests `"auto" in lower` before the feeder rules returns `AUTO`, which falls to the single-page branch — C-06, restored. The `AUTO` check must be an exact match (`lower == "auto"`), exactly as `auto_profiles.source_to_slug` already does it. Real SANE source strings were harvested from the installed man pages and from the live `test` backend on this machine; the full inventory and its three genuine ambiguities are below.

**Fourth, there is a trap in collapsing `worker._status_cb`.** The current `if/elif` chain handles four of six `PipelineEvent` members and **silently ignores `SCANNING` and `DONE`** — but `run_pipeline` *does* emit both. A `job_state`-driven rewrite that applies every mapped state would set the job `DONE` from inside the pipeline's `with tempfile.TemporaryDirectory()` block, before `run_pipeline` returns. That is a real, user-observable behaviour change on a UI that polls once per second.

**Primary recommendation:** Land `vocabulary.py` + `job.py` re-exports as a pure addition first, then rewire consumers in the order Web → CLI/Worker → ErrorCategory → Classifier → `UploadResult` → `ScanResult` → docs, writing every total lookup as assign-then-single-return `match` with `case _: assert_never(x)`.

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| `JobState`, `ErrorCategory`, `ScanOutcome` definitions | Domain vocabulary (`vocabulary.py`) | — | Leaf module; must import nothing from the app (D-02) |
| `ACTIVE_STATES` / `TERMINAL_STATES` / `BUSY_STATES` | Domain vocabulary | — | Derived and hand-written classifications belong beside the enum |
| `state_label()` / `progress_label()` / `error_message()` | Domain vocabulary | — | Presentation *strings* are domain vocabulary here; presentation *markup* stays in templates |
| `Job.is_active` / `Job.is_busy` | Persistence model (`job.py`) | Template consumer | The template must not import a frozenset; it asks the object it was handed |
| Rendering decisions (which markup for which state) | Jinja2 templates | — | Structural branching (flip include, history-refresh div) is not a label lookup |
| `SourceKind` + `classify_source()` | Scanner package (`scanner/base.py`) | — | CTR-04 names the location; the scanner package must not depend on job vocabulary (D-03) |
| Feeder-vs-flatbed routing | Scanner backend (`sane_backend.py`) | Classifier | Backend asks one question of one classifier |
| Profile slug naming | `auto_profiles.py` | Classifier | Slugging is a *different question* from classification — see the Pitfall section |
| `ScanResult` | Pipeline (`pipeline.py`) | — | Result lives with its producer, symmetric with `PipelineRequest` (D-05) |
| `UploadResult` | Paperless client (`paperless.py`) | — | Result lives with its producer (D-05) |
| `PipelineEvent -> JobState` mapping | Pipeline (`pipeline.py`) | Worker consumer | Transient event vocabulary owns the projection onto persisted vocabulary (D-01) |

---

<user_constraints>
## User Constraints (from CONTEXT.md)

The user delegated every gray area in this phase ("All good, no need to discuss. I trust you"; "Whatever you think is best"). D-01..D-13 are **Claude's decisions, and they are locked for planning**. A planner that disagrees with one should raise it, not silently implement the other branch.

### Locked Decisions

- **D-01: `JobState` and `PipelineEvent` stay two enums; the mapping becomes total and machine-checked.** Add `PipelineEvent.job_state -> JobState | None`, implemented as a `match` with `assert_never`. `None` means "this event changes no persisted state" — today that is exactly `SCANNING_REVERSE`.
- **D-02: the shared vocabulary lives in a new `src/saneless/vocabulary.py`.** `job.py` re-exports `JobState` and `ErrorCategory` and keeps both in its `__all__`. `vocabulary.py` must import nothing from `job.py` — the dependency runs one way only.
- **D-03: `SourceKind` and `classify_source()` go in `scanner/base.py`, not `vocabulary.py`.**
- **D-04: three classifications of `JobState`, not two, and only two of them are hand-written.** `ACTIVE_STATES: frozenset[JobState]` and `TERMINAL_STATES: frozenset[JobState]` are written out and must partition `set(JobState)` exactly. `BUSY_STATES` is **derived** (`ACTIVE_STATES - {JobState.AWAITING_FLIP}`), never hand-written.
- **D-05: result types live with their producers.** `ScanResult` in `pipeline.py`; `UploadResult` in `paperless.py`. Only `ScanOutcome` goes in `vocabulary.py`.
- **D-06: Phase 21 adds no new `JobState` member.** Not `FALLBACK`, not `SCANNING_REVERSE`.
- **D-07: `ScanOutcome` ships with `SUCCESS` and `FALLBACK` only; `FAILED` is Phase 23's.**
- **D-08: a `dict[JobState, str]` gives ZERO compile-time enforcement. M-05's suggested fix is wrong on this point and must not be copied.** Every lookup that must be total is a function using `match` + `assert_never`, not a dict. That covers `PipelineEvent.job_state`, `state_label()`, `progress_label()`, and `error_message()`.
- **D-09: the parametrised tests are the enforcement for the frozensets, and they are required regardless.** `@pytest.mark.parametrize("state", list(JobState))` asserting (a) `state_label(state)` returns a non-empty label that is not merely the raw member name, and (b) `(state in ACTIVE_STATES) ^ (state in TERMINAL_STATES)`. Same shape for `ErrorCategory` × `error_message`. Parametrise over `list(JobState)` — never a hand-written list of names.
- **D-10: `test_humanize_state_filter_unit` must change, deliberately.** Replace `tests/test_web.py:378`'s raw-fallback assertion with one that an unrecognised value raises.
- **D-11: `classify_source()` becomes the only source-classification rule, and the resulting C-06 routing correction lands here — intentionally.** `sane_backend` starts taking the `multi_scan()` path for `"Automatic Document Feeder"`. **The planner and the verifier must treat this as planned work, not as an execution deviation.** It needs its own test (a feeder named `"Automatic Document Feeder"` yields N pages, not 1). `SourceKind.UNKNOWN` keeps today's routing — it falls to the single-page branch.
- **D-12: `ErrorCategory` gets its single message map in this phase but is NOT wired to the UI.** Move `ErrorCategory` into `vocabulary.py`, move `_categorize_error` off `ScanWorker` to a module-level `classify_error(exc) -> ErrorCategory`, and define one `error_message()` behind `match`/`assert_never`. The status partial keeps rendering `job.error` verbatim.
- **D-13: success criterion 3's "the `"fallback"` magic string is absent from `docs/`" means the *sentinel*, not the English word.** The one doc statement in scope is **`docs/explanation/consume-directory-fallback.md:66`**. Verify with `grep -rn '"fallback"' src/ tests/` returning nothing, plus a targeted check that no doc claims a `FALLBACK` job status.

### Claude's Discretion

- The exact prose of every label, subject to one hard constraint: **the strings users see today must not change.** `state_label` must keep `"Pending" / "Scanning" / "Waiting for flip" / "Assembling" / "Uploading" / "Complete" / "Failed"` (`web/app.py:36-42`), and `progress_label` must keep `"Starting scan..." / "Scanning..." / "Awaiting flip..." / "Assembling PDF..." / "Uploading to paperless-ngx..."` (`status.html:9-19`, `cli.py:111-115`) with their literal three-dot spelling.
- Whether `state_label` and `progress_label` are two functions or one function with a mode argument.
- Whether the Jinja filter keeps the name `humanize_state` or is renamed with the call sites updated.
- Task and commit breakdown, provided `vocabulary.py` plus the `job.py` re-exports land before anything that imports them.
- Whether `classify_error` keeps a thin `ScanWorker._categorize_error` delegate for test compatibility or the tests move to the module-level function. *(Research resolves this — see Regression Risk below: **no test references `_categorize_error`**, so no shim is needed.)*

### Deferred Ideas (OUT OF SCOPE)

- **`JobState.FALLBACK`** + its label, classification, and distinct rendering — Phase 23.
- **`ScanOutcome.FAILED`** — Phase 23.
- **`JobState.SCANNING_REVERSE`** — Phase 25. Until then `PipelineEvent.SCANNING_REVERSE.job_state` is `None` and the CLI keeps one explicit case for its prose.
- **The duplicated `"manual" in source and "duplex" in source` rule** (`pipeline.py:96`, `worker.py:207`) — Phase 25.
- **Routing `SourceKind.UNKNOWN` to the multi-page path** (C-06's "safer default") — Phase 24.
- **Wiring `error_message()` into the status partial, history table, and CLI** — Phase 30.
- **Rendering the Scan button from a single include with `hx-swap-oob`** — Phase 26.
- **`poll_task` raising `PaperlessError` on `FAILURE` and on timeout, and returning something typed** — Phase 23. This phase leaves `poll_task`'s `-> dict[str, object]` alone.
- **`source_to_slug` collision handling** (N-09) — flag during Phase 24 planning.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| **CTR-01** | There is exactly one `JobState` enum, one active-state list, and one state-to-label map, shared by the worker, web templates, and CLI; `job.py` re-exports them so existing imports keep working [M-05] | § "The Exhaustiveness Pattern" gives the exact function form that survives ruff + `ty` + `pyrefly`. § "Duplication Inventory" gives all 9 call sites with line numbers. § "Jinja2 Integration" gives the template contract. § "Regression Risk" names the 5 tests that must change. |
| **CTR-02** | The pipeline returns a typed `ScanResult` (outcome enum SUCCESS/FALLBACK/FAILED, pages scanned, pages removed as blank, pages uploaded, warning text) instead of a value nobody reads [C-03, N-38] | § "Typed Results" recommends `@dataclass` with rationale against `NamedTuple`/Pydantic and lists every field's data source inside `run_pipeline`. § "Regression Risk" names the 4 assertions that read the dict today. D-07 scopes `FAILED` out. |
| **CTR-03** | `upload_document` returns a typed `UploadResult`; the `"fallback"` magic string and every reader of it in `src/`, `tests/`, and `docs/` are deleted [C-03, N-38] | § "Sentinel Inventory" lists all 8 sites. § "Regression Risk" gives the **measured** 18-test blast radius and the 5 distinct stub sites. D-13 gives the doc verification command. |
| **CTR-04** | A single `classify_source()` in `scanner/base.py` returns a `SourceKind` (FLATBED, FEEDER, FEEDER_DUPLEX, AUTO, UNKNOWN) for any SANE source string, including "Automatic Document Feeder", "ADF Front", "ADF Duplex", and vendor variants, and is the only classification rule in the codebase [C-06, N-09] | § "SANE Source Strings" gives the harvested inventory from installed man pages plus the live `test` backend, the `"auto" in "Automatic"` pitfall, the recommended rule order, and a `source_to_slug` refactor that keeps all 9 existing slug tests green. |
| **CTR-05** | `ErrorCategory` lives with `JobState` and is the input to a single user-message map; no template, route, or CLI output classifies errors by string matching [N-14, U-05] | D-12 scopes this to the move + `error_message()`. § "Regression Risk" confirms **zero** tests reference `_categorize_error`, so the move is free. |
</phase_requirements>

---

## Project Constraints (from CLAUDE.md)

These are as authoritative as the locked decisions. The planner must not recommend anything that contradicts them.

| Directive | Consequence for this phase |
|-----------|---------------------------|
| Python 3.14, `uv` (not pip/poetry/conda) | All commands are `uv run ...`. Verified: Python 3.14.2. |
| **All four checks must pass with zero errors:** `ruff check`, `ruff format --check`, `ty check`, `pyrefly check` | The `PLR0911` finding below is a hard CI blocker, not a style nit. |
| **"Fix reported issues properly. Do not suppress errors with `# type: ignore`, `# noqa`, or by disabling rules."** | The `PLR0911` fix must be structural (single-return form). Raising `pylint.max-returns` in `pyproject.toml` is "disabling a rule" and is forbidden. |
| No mypy or pyright | Only `ty` and `pyrefly` verdicts matter — and **they disagree on match-pattern narrowing**. See the pattern table. |
| Ruff `D` rules require docstrings on all public modules, classes, and functions | `vocabulary.py` needs a module docstring; every enum, function, and dataclass needs one. |
| Prefer external packages over reimplementing solved problems | Not applicable — this phase adds **zero** dependencies. Everything uses `enum`, `dataclasses`, and `typing` from the stdlib. |
| Context7 for library docs | Used for Jinja2 (filter/global/test registration semantics). |
| Playwright for browser validation; never mark browser checks "manual-only" | Templates change in this phase. The 8 `browser`-marked tests in `tests/test_browser.py` are deselected in the standard run — the planner should run `uv run pytest -m browser` after the template edits. |

**From `CONTRIBUTING.md`** — the five checks every change must pass:
```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pyrefly check
uv run pytest -m "not browser"
```

**From `pyproject.toml` (Phase 20, live):** `--strict-config`, `--strict-markers`, `xfail_strict = true`, `filterwarnings = ["error"]`, `timeout = 60`, `timeout_method = "signal"`.

---

## Standard Stack

This phase adds **no dependencies**. Everything is stdlib or already pinned.

### Core (already present, no version change)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `enum.StrEnum` | stdlib 3.14.2 | `JobState`, `ErrorCategory`, `ScanOutcome`, `SourceKind`, `PipelineEvent` | Already the codebase's idiom (`job.py:24`, `pipeline.py:37`); persists to SQLite `TEXT` unchanged. `[VERIFIED: executed]` |
| `typing.assert_never` | stdlib 3.14.2 | Compile-time exhaustiveness; raises `AssertionError` at runtime | The **only** construct that both `ty` and `pyrefly` honour for enum exhaustiveness in this repo. `[VERIFIED: executed against ty 0.0.80 + pyrefly 1.2.0]` |
| `dataclasses.dataclass` | stdlib 3.14.2 | `ScanResult`, `UploadResult` | Codebase idiom: all 5 existing value types are plain `@dataclass` (`job.py:46`, `pipeline.py:51`, `scanner/base.py:23,33,43`). `[VERIFIED: grep]` |
| `frozenset` | stdlib | `ACTIVE_STATES`, `TERMINAL_STATES`, `BUSY_STATES` | Immutable, set-algebra for the derived `BUSY_STATES` (D-04). |
| Jinja2 | 3.1.6 | Template filters | Already registered at `web/app.py:102`. `[VERIFIED: importlib.metadata]` |
| FastAPI / Starlette | 0.135.1 / 0.52.1 | `Jinja2Templates` | Unchanged. `[VERIFIED: importlib.metadata]` |

### Toolchain (verified on this machine, 2026-09-10)

| Tool | Version |
|------|---------|
| Python | 3.14.2 |
| ruff | 0.15.7 |
| ty | 0.0.80 |
| pyrefly | 1.2.0 |
| pytest | 9.0.2 |
| pytest-timeout | 2.4.0 |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `@dataclass` for `ScanResult`/`UploadResult` | `NamedTuple` | Rejected. A `NamedTuple` is index-accessible and unpackable, which invites positional bugs when Phase 22/23 extend it (STOR-03 adds six columns that mirror these fields). It is also truthy-by-length, so a future empty-result variant would be falsy. Dataclasses with keyword defaults extend cleanly. |
| `@dataclass` | Pydantic `BaseModel` | Rejected. Pydantic is used in this codebase **only** for config (`config.py`), where the input is external and untrusted. `ScanResult` is an internal value object constructed by code that already knows the types; runtime validation buys nothing and costs per-scan overhead. |
| plain `@dataclass` | `@dataclass(frozen=True, slots=True)` | Defensible either way. `frozen=True` matches the "immutable snapshot" semantics and prevents a later phase mutating a result in place. But **no existing dataclass in this codebase is frozen**, and consistency has value. Planner's call; a mild preference for `frozen=True` on the two *new* result types, since nothing needs to mutate them. |
| `match` + `assert_never` | `dict[JobState, str]` | Rejected — **measured to give zero enforcement** (D-08, reproduced below). |
| `match` + `assert_never` | `if/elif` chain + `assert_never` | Both work under both checkers (verified). `match` reads better for a pure lookup; `if/elif` with `is` is fine where the arms do side effects. |
| `frozenset` partition | `@property` on `JobState` (e.g. `JobState.DONE.is_terminal`) | Rejected for this phase — D-04 locks the frozensets, and a property returning `bool` per member is a total function needing its own `assert_never`, which is more machinery than the parametrised test D-09 already mandates. |

**Installation:** none. `uv sync` unchanged, `uv.lock` unchanged.

---

## Package Legitimacy Audit

**This phase installs no external packages.** Every construct used is Python standard library (`enum`, `dataclasses`, `typing`, `frozenset`) or an already-pinned, already-audited dependency (`jinja2`, `fastapi`) whose version does not change.

| Package | Registry | Disposition |
|---------|----------|-------------|
| *(none)* | — | No installs in this phase |

**Packages removed due to slopcheck [SLOP] verdict:** none — nothing to check.
**Packages flagged as suspicious [SUS]:** none.

If the planner finds itself reaching for a new dependency, that is a signal the design has drifted from D-02/D-05; raise it rather than installing.

---

## Architecture Patterns

### System Architecture Diagram

```
                       ┌──────────────────────────┐
   (leaf module,       │  vocabulary.py           │
    imports nothing    │  ──────────────────────  │
    from the app)      │  JobState (StrEnum)      │
                       │  ErrorCategory (StrEnum) │
                       │  ScanOutcome (StrEnum)   │
                       │  ACTIVE_STATES  ─┐       │
                       │  TERMINAL_STATES │ D-04  │
                       │  BUSY_STATES ◄───┘derived│
                       │  state_label()    ─┐     │
                       │  progress_label() │ all  │
                       │  error_message()  │ total│
                       │  classify_error() ─┘     │
                       └────────┬─────────────────┘
                                │ (one-way)
             ┌──────────────────┼───────────────────┬─────────────────┐
             ▼                  ▼                   ▼                 ▼
      ┌─────────────┐   ┌──────────────┐    ┌──────────────┐   ┌────────────┐
      │  job.py     │   │ pipeline.py  │    │  worker.py   │   │  cli.py    │
      │ re-exports  │   │ PipelineEvent│    │ _status_cb   │   │ status_    │
      │ JobState,   │   │  .job_state ─┼───►│  applies only│   │  callback  │
      │ ErrorCateg. │   │ ScanResult   │    │  ACTIVE ones │   │  reads     │
      │ Job.is_active│  │ (D-05)       │    │ classify_    │   │  progress_ │
      │ Job.is_busy │   └──────┬───────┘    │  error()     │   │  label()   │
      └──────┬──────┘          │            └──────────────┘   └────────────┘
             │                 │ calls
             │                 ▼
             │          ┌──────────────┐
             │          │ paperless.py │
             │          │ UploadResult │◄── replaces `return "fallback"`
             │          │  (D-05)      │    (paperless.py:155)
             │          └──────────────┘
             │
             ▼ (Job objects passed into template context by routes.py)
      ┌───────────────────────────────────────────────────────┐
      │  Jinja2 Environment  (web/app.py:102)                  │
      │   filters: state_label, progress_label                 │
      │                                                        │
      │  index.html          ─ job.is_active  → disabled       │
      │                      ─ job.is_busy    → aria-busy      │
      │  partials/status.html─ job.is_active  → hx-trigger     │
      │                      ─ job.is_busy    → prose branch   │
      │  partials/history.html─ job.state | state_label        │
      └───────────────────────────────────────────────────────┘


   ┌─────────────────────────────────────────────────────────┐
   │ scanner/base.py   (separate, must NOT import vocabulary)│
   │   SourceKind (StrEnum): FLATBED FEEDER FEEDER_DUPLEX    │
   │                          AUTO UNKNOWN                    │
   │   classify_source(str) -> SourceKind   ◄─ ONE rule       │
   └──────────┬──────────────────────────┬───────────────────┘
              │                          │
              ▼                          ▼
   ┌────────────────────┐     ┌────────────────────────┐
   │ sane_backend.py    │     │ auto_profiles.py       │
   │ _is_adf_source     │     │ source_to_slug         │
   │  :158-160          │     │  :47-58                │
   │ → "does this use   │     │ → "what do I NAME the  │
   │    the feeder?"    │     │    profile?"           │
   │  (multi_scan path) │     │  (different question — │
   │  ** D-11 change **  │     │   see Pitfall 4)       │
   └────────────────────┘     └────────────────────────┘
```

A reader tracing the primary use case: a browser submits → `routes.py` creates a `Job` (state `PENDING`) → `worker._process_job` sets `SCANNING` → `run_pipeline` emits `PipelineEvent`s → `_status_cb` projects each to a `JobState` via `PipelineEvent.job_state` and applies only the **active** ones → the 1 s HTMX poll re-renders `status.html`, which asks the `Job` object `is_active` / `is_busy` and asks the filter for the label → `run_pipeline` returns a `ScanResult` → the worker sets `DONE`.

### Recommended Project Structure

```
src/saneless/
├── vocabulary.py        # NEW — the phase's primary artifact (leaf, no app imports)
├── job.py               # re-exports JobState/ErrorCategory; Job gains is_active/is_busy
├── pipeline.py          # PipelineEvent.job_state, ScanResult; -> dict deleted
├── paperless.py         # UploadResult; `return "fallback"` deleted
├── worker.py            # _status_cb collapses; _categorize_error moves out
├── cli.py               # _event_labels deleted
├── web/
│   ├── app.py           # _STATE_LABELS deleted; filter(s) re-backed
│   └── templates/
│       ├── index.html                # literal state lists → job.is_active / job.is_busy
│       └── partials/
│           ├── status.html           # literal list + prose chain → shared vocabulary
│           └── history.html          # job.state | state_label
└── scanner/
    ├── base.py          # SourceKind + classify_source()
    ├── sane_backend.py  # _is_adf_source delegates
    └── ../auto_profiles.py  # source_to_slug builds on classify_source
```

---

### Pattern 1: The Exhaustiveness Pattern (THE load-bearing pattern of this phase)

**What:** A total function over an enum, written so that adding a member and forgetting a case is caught by *both* checkers at edit time — and so that ruff does not reject it.

**When to use:** `PipelineEvent.job_state`, `state_label()`, `progress_label()`, `error_message()`. Per D-08, every lookup that must be total.

**The measured pattern matrix.** All rows executed on 2026-09-10 against this repo with `ty 0.0.80` and `pyrefly 1.2.0`, using a 3-member probe `StrEnum` with one member deliberately unhandled.

| Construct | `ty` catches missing member? | `pyrefly` catches missing member? | Usable? |
|-----------|:---:|:---:|:---:|
| `match` with bare member patterns (`case S.A:`), `assert_never(s)` after the match | ✅ `Literal[S.C]` | ✅ `Literal[S.C]` | **YES** |
| `match` with bare member patterns, `case _: assert_never(s)` as last arm | ✅ `Literal[S.C]` | ✅ `Literal[S.C]` | **YES** |
| `match` with or-patterns (`case S.A \| S.B:`) | ✅ | ✅ | **YES** |
| `if s is S.A: ... ` chain, `assert_never(s)` after | ✅ | ✅ | **YES** |
| `if s == S.A: ...` chain, `assert_never(s)` after | ✅ | ✅ | **YES** |
| `match` with **raw string patterns** (`case "A":`) | ✅ `Literal[S.C]` | ❌ reports the whole `S` — **does not narrow at all** | **NO** |
| `match` with **`.value` patterns** (`case S.A.value:`) | ✅ | ❌ reports the whole `S` | **NO** |
| `match` with a **guarded arm** (`case S.A if cond:`) | ❌ silently accepts | ✅ reports `Literal[S.A]` | **NO** |
| `match` with `case _: raise ValueError(...)` | ❌ no diagnostic | ❌ no diagnostic | **NO — anti-pattern** |
| `dict[S, str]` literal missing a member | ❌ no diagnostic | ❌ no diagnostic | **NO** (confirms D-08) |

`[VERIFIED: executed — probe module type-checked with both tools in this repository]`

**The ruff constraint.** A `match` over the 7-member `JobState` written with one `return` per arm produces:

```
PLR0911 Too many return statements (7 > 6)
  --> src/saneless/_probe.py:21:5
```

`[VERIFIED: executed — uv run ruff check]`

`PLR0911`'s default `max-returns` is 6. `JobState` has 7 members. Raising the limit in `pyproject.toml` or adding `# noqa` both violate the project rule. **The structural fix is assign-then-single-return.**

**The recommended form:**

```python
def state_label(state: JobState) -> str:
    """
    Return the short human label for a job state.

    Args:
        state: The job state to label.

    Returns:
        The user-facing label, e.g. "Waiting for flip".

    Raises:
        AssertionError: If the value is not a JobState member.

    """
    match state:
        case JobState.PENDING:
            label = "Pending"
        case JobState.SCANNING:
            label = "Scanning"
        case JobState.AWAITING_FLIP:
            label = "Waiting for flip"
        case JobState.ASSEMBLING:
            label = "Assembling"
        case JobState.UPLOADING:
            label = "Uploading"
        case JobState.DONE:
            label = "Complete"
        case JobState.ERROR:
            label = "Failed"
        case _:
            assert_never(state)
    return label
```

Verified against all four gates:
- `ruff check` → clean (one return statement)
- `ty check` → clean when total; `error[type-assertion-failure] ... Inferred type of argument is Literal[JobState.ERROR]` when a member is dropped
- `pyrefly check` → clean when total; `ERROR Argument Literal[JobState.ERROR] is not assignable to parameter arg with type Never [bad-argument-type]` when a member is dropped
- runtime → returns the label; raises `AssertionError: Expected code to be unreachable, but got: 'ZZZ'` for an unknown value

`[VERIFIED: executed — all four gates, both the complete and the deliberately-incomplete variant]`

**Functions with ≤ 6 arms** (`error_message()` over 5 `ErrorCategory` members, `PipelineEvent.job_state` over 6 members) may use the multi-return form without tripping `PLR0911`. Using the single-return form uniformly is still recommended for consistency and for future-proofing — `PipelineEvent` gains a 7th member in Phase 25.

**Runtime note for D-10:** `typing.assert_never` raises **`AssertionError`**, not `ValueError`. It is a real `raise` statement inside `typing.py`, not an `assert`, so `python -O` does **not** remove it. The D-10 replacement test must therefore be `pytest.raises(AssertionError)`. `[VERIFIED: executed — inspect.getsource(typing.assert_never)]`

**Runtime note on `StrEnum` matching:** because a `StrEnum` member *is* a `str`, `case JobState.DONE:` matches the raw string `"DONE"` at runtime as well as the enum member. This means the existing template call `{{ job.state.value | humanize_state }}` would keep working even after the function is retyped to take `JobState` — silently, and untyped. Change the template to pass the enum anyway (see Jinja2 Integration). `[VERIFIED: executed]`

---

### Pattern 2: `PipelineEvent.job_state` and the worker collapse

**What:** D-01's total projection from transient event vocabulary onto persisted vocabulary.

```python
class PipelineEvent(StrEnum):
    """Events emitted by the scan pipeline to report progress."""

    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    SCANNING_REVERSE = "SCANNING_REVERSE"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"

    @property
    def job_state(self) -> JobState | None:
        """
        The persisted job state this event implies, or None.

        None means the event changes no persisted state; today that is
        exactly SCANNING_REVERSE, which has no JobState twin until the
        manual-duplex work adds one.
        """
        match self:
            case PipelineEvent.SCANNING:
                state = JobState.SCANNING
            case PipelineEvent.AWAITING_FLIP:
                state = JobState.AWAITING_FLIP
            case PipelineEvent.SCANNING_REVERSE:
                state = None
            case PipelineEvent.ASSEMBLING:
                state = JobState.ASSEMBLING
            case PipelineEvent.UPLOADING:
                state = JobState.UPLOADING
            case PipelineEvent.DONE:
                state = JobState.DONE
            case _:
                assert_never(self)
        return state
```

**⚠️ The trap.** The current `_status_cb` (`worker.py:222-233`) handles four events and **silently ignores `SCANNING` and `DONE`**. But `run_pipeline` emits both: `notify(PipelineEvent.SCANNING)` fires at the top of the scan step, and `notify(PipelineEvent.DONE)` fires **inside** the `with tempfile.TemporaryDirectory(...)` block, before the directory is torn down and before `run_pipeline` returns. The worker sets `DONE` only *after* `run_pipeline` returns (`worker.py:252`).

A naive rewrite — "apply `event.job_state` whenever it is not `None`" — would mark the job `DONE` from inside the pipeline. Consequences, all user-observable because `status.html` polls once per second:
- The status area shows the "✓ Done" panel and fires the history-refresh `hx-get` before temp-dir cleanup completes.
- If anything raises during cleanup or return, the worker's `except` then writes `ERROR` — a visible DONE→ERROR flicker.
- The `_transition_event` would be set earlier than it is today.

**The fix that preserves behaviour without a hand-written ignore list:** apply only *active* states. `ACTIVE_STATES` is derived vocabulary, so this is not a new literal list — and it naturally excludes `DONE` (terminal) while including `SCANNING` (idempotent: the worker already set it at `worker.py:203`, and `update_state` is a plain UPDATE).

```python
def _status_cb(event: PipelineEvent, _jid: str = job.id) -> None:
    logger.info("Pipeline event: %s", event.value)
    state = event.job_state
    if state is None or state not in ACTIVE_STATES:
        # SCANNING_REVERSE carries no persisted state; DONE is written by
        # the worker after run_pipeline returns, not from inside it.
        self._transition_event.set()
        return
    if state in BUSY_STATES:
        self._job_store.update_state(_jid, state)
        self._transition_event.set()
    else:
        # AWAITING_FLIP: the UI must not see "busy" while we wait for a human.
        self._transition_event.clear()
        self._job_store.update_state(_jid, state)
```

**Ordering is load-bearing** (CONTEXT.md § "Ordering the refactor must respect"): clear-**before** the AWAITING_FLIP update; set-**after** the ASSEMBLING/UPLOADING updates. The branch above preserves that exactly. `BUSY_STATES == ACTIVE_STATES - {AWAITING_FLIP}` is precisely the "set after" set, which is a second independent confirmation of D-04.

One deliberate, benign delta: `PipelineEvent.DONE` currently produces no `_transition_event.set()`; under the code above it does. It is already set at that moment (`worker.py:204` or a prior busy event), and `threading.Event.set()` on an already-set event is a no-op. Same for `PipelineEvent.SCANNING`. If the planner prefers zero delta, add `return` without the `set()` for the terminal case — but note `SCANNING_REVERSE` **must** keep its `set()` (`worker.py:232-233`).

---

### Pattern 3: `Job.is_active` / `Job.is_busy` — the template's contract

```python
@dataclass
class Job:
    ...

    @property
    def is_active(self) -> bool:
        """Whether this job is still in flight (not DONE or ERROR)."""
        return self.state in ACTIVE_STATES

    @property
    def is_busy(self) -> bool:
        """Whether the machine is working (active, but not waiting for a human)."""
        return self.state in BUSY_STATES
```

A `@property` on a `@dataclass` is fine — it is not a field, so it does not appear in `__init__` or `__eq__`. Jinja resolves `job.is_active` by attribute access and calls the descriptor. `[CITED: jinja.palletsprojects.com/en/stable/api]`

This keeps the frozensets out of the template namespace entirely and gives Phase 30 a natural extension point.

---

### Pattern 4: Jinja2 integration

**Registration.** The existing mechanism is correct and should be extended, not replaced:

```python
app.state.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
app.state.templates.env.filters["state_label"] = state_label
app.state.templates.env.filters["progress_label"] = progress_label
```

Jinja's docs are explicit that `env.filters`, `env.tests`, and `env.globals` "can be safely modified as long as no templates have been loaded yet" — `create_app` registers before the first request, so this is correct. `[CITED: jinja.palletsprojects.com/en/stable/api]`

**Filter vs global vs test vs property — the recommendation:**

| Need | Mechanism | Why |
|------|-----------|-----|
| "What is this state called?" | **Filter** (`{{ job.state \| state_label }}`) | Already the established idiom at `app.py:102`; reads naturally in the pipe position; keeps the function typed and unit-testable. |
| "Is this job still running?" | **Property on `Job`** (`{% if job.is_active %}`) | The template already holds the `Job`. A global `ACTIVE_STATES` would force `{% if job.state in ACTIVE_STATES %}`, putting set membership in the template — the exact stringly-typed logic this phase is deleting. |
| "Is the machine working (vs waiting for a human)?" | **Property on `Job`** (`{% if job.is_busy %}`) | Same reasoning; and it happens to be exactly the four `aria-busy` prose branches. |
| Exposing the enum itself | **Avoid** | `env.globals["JobState"] = JobState` would let templates write `job.state == JobState.DONE`, which is fine but no better than the properties, and it widens the template's surface for no gain. Use it only for `DONE`/`ERROR`, where the branches are structurally different (see below). |

**Do not use a Jinja `test`** (`{% if job.state is active %}`). It works, but nothing else in this codebase registers a test, and the property form reads better.

**The `status.html` rewrite.** The prose chain (`:9-19`) is four `aria-busy` branches plus three structurally different branches. Only the four collapse:

```jinja
{% if job.is_busy %}
  <p aria-busy="true">{{ job.state | progress_label }}</p>
{% elif job.state == JobState.AWAITING_FLIP %}
  {% include "partials/flip.html" %}
{% elif job.state == JobState.DONE %}
  ... unchanged markup ...
{% elif job.state == JobState.ERROR %}
  ... unchanged markup ...
{% endif %}
```

`job.is_busy` is exactly `{PENDING, SCANNING, ASSEMBLING, UPLOADING}` — the four prose branches, in the order they occur. Line 1's `{% set active_states = [...] %}` and line 3's membership test become `{% if job and job.is_active %}`.

Whether the remaining three comparisons use `JobState` as a Jinja global or keep `job.state.value == "DONE"` is the planner's call; the global is cleaner and there is now exactly one place that would need updating when Phase 23 adds `FALLBACK`.

**`index.html:58-59` rewrite:**

```jinja
<button type="submit" id="scan-btn"
        {% if job and job.is_active %}disabled{% if job.is_busy %} aria-busy="true"{% endif %}{% endif %}>
    {% if job and job.state == JobState.AWAITING_FLIP %}Waiting for flip&#8230;
    {% elif job and job.is_busy %}Scanning&#8230;
    {% else %}Scan{% endif %}
</button>
```

Note the HTML entities `&#8230;` (ellipsis) and `&#10003;` (check) must be preserved verbatim — `tests/test_web.py` and `tests/test_browser.py` assert against rendered text.

**`history.html:8` rewrite:** `{{ job.state.value | humanize_state }}` → `{{ job.state | state_label }}`. Passing `.value` still works at runtime (StrEnum equality) but hands a `str` to a `JobState`-typed function; pass the enum.

**What breaks if a filter raises inside a render — measured:**

```
render raised: AssertionError -> Expected code to be unreachable, but got: 'UNKNOWN'
HTTP status via TestClient(raise_server_exceptions=False): 500
TestClient default re-raises: AssertionError
```
`[VERIFIED: executed — jinja2 3.1.6 + Starlette Jinja2Templates + fastapi.testclient]`

The exception propagates straight out of `TemplateResponse` and Starlette's `ServerErrorMiddleware` turns it into a bare **HTTP 500**. Because `status.html` is fetched by `hx-get ... hx-trigger="every 1s"` and htmx 2 does **not** swap non-2xx bodies by default (a fact this project already recorded in the ROADMAP's Phase 26 note), the failure mode of a raising label filter is: **the UI freezes silently on its last good render while the poll keeps firing a 500 every second**. There is no error visible to the user.

This is why D-10's safety argument matters and must be re-verified by the planner rather than assumed. It holds: `Job.state` is constructed exclusively through `JobState(row[3])` at `job.py:187` and `job.py:262`, which raises `ValueError` inside `get_job`/`list_recent` for any unknown value — so a bad value cannot reach the template. `[VERIFIED: read job.py]` A note in the test's docstring recording *why* raising is safe would be worth its two lines.

Also note the two `TestClient` behaviours above: `tests/test_web.py`'s client uses the default (`raise_server_exceptions=True`), so a render failure surfaces as the raw exception in the test, not as a 500 — useful when debugging.

---

### Pattern 5: Typed results

```python
# vocabulary.py
class ScanOutcome(StrEnum):
    """How a completed scan was delivered."""

    SUCCESS = "SUCCESS"
    FALLBACK = "FALLBACK"
    # FAILED is Phase 23's, added with the code path that produces it (D-07).


# paperless.py
@dataclass
class UploadResult:
    """Result of an upload_document call."""

    delivered_to_api: bool
    task_uuid: str | None = None
    consume_dir_path: Path | None = None


# pipeline.py
@dataclass
class ScanResult:
    """Result of a full scan pipeline run."""

    outcome: ScanOutcome
    pages_scanned: int
    pages_removed: int
    pages_uploaded: int
    warning: str | None = None
    task_result: dict[str, object] | None = None   # poll_task's dict, untyped until Ph23
```

**Where each `ScanResult` field comes from inside `run_pipeline` today** (so the planner does not have to re-derive it):

| Field | Source |
|-------|--------|
| `pages_scanned` | `len(images)` after the scan step (`pipeline.py:~394`) |
| `pages_removed` | `len(images) - len(filtered)` in the empty-page branch; `0` when detection is disabled |
| `pages_uploaded` | `len(filtered)` on the normal path; on the duplex-mismatch path it is `len(fronts) + len(backs)` across two uploads |
| `warning` | `None` normally; `_handle_duplex_mismatch`'s return value on the mismatch path (`pipeline.py:~386`) |
| `outcome` | `ScanOutcome.FALLBACK` when `UploadResult.delivered_to_api is False`, else `ScanOutcome.SUCCESS`. The duplex-mismatch early return is `SUCCESS` with a warning — matching today's `{"status": "DONE", "warning": ...}` |

**`UploadResult` discriminator — a warning.** Whatever field the pipeline branches on at `pipeline.py:432`, prefer a plain `bool` (`delivered_to_api`) or an explicit `is None` check over an `is`-identity comparison against an enum member. Reason: the test stubs are `MagicMock`s and hand-rolled fakes; an `is` comparison against an enum silently takes the wrong branch when the stub returns something else, whereas an attribute access on the wrong type raises loudly. See the measured blast radius below — the loud failure is what you want.

**`poll_task` is untouched in this phase** (deferred to Phase 23). Its `-> dict[str, object]` stays, so `ScanResult` has to carry that dict somewhere or discard it. Since **neither caller reads the return value today** (`worker.py:250-256` and `cli.py:132-137` both discard it), the planner may legitimately drop `task_result` entirely and let Phase 23 add whatever `poll_task`'s typed replacement returns. That is the cleaner option; carrying an untyped dict through a typed result is the smell this phase exists to remove.

---

### Anti-Patterns to Avoid

- **`case _: raise ValueError(...)` as the fall-through.** Measured: **neither** checker emits a diagnostic. The wildcard absorbs the missing member and destroys the entire enforcement mechanism. Use `case _: assert_never(x)` or put `assert_never(x)` after the match. `[VERIFIED: executed]`
- **`# noqa: PLR0911` or raising `pylint.max-returns`.** Forbidden by CLAUDE.md. Use the single-return form.
- **Raw string patterns or `.value` patterns in a `match` over a `StrEnum`.** `pyrefly` does not narrow them; the `assert_never` fires as an error even when the match is total. `[VERIFIED: executed]`
- **Guarded case arms in an exhaustive match.** `pyrefly` (correctly) refuses to count a guarded arm as consuming the member; `ty` (incorrectly) does. Restructure to put the condition inside the arm body. `[VERIFIED: executed]`
- **Typing the label map as `dict[JobState, str]` for enforcement.** Confirms D-08; M-05's suggestion is measurably wrong here.
- **Applying every `PipelineEvent.job_state` in `_status_cb`.** Sets `DONE` from inside `run_pipeline`. See Pattern 2.
- **Making `source_to_slug` a thin wrapper over `classify_source`.** They answer different questions. See Pitfall 4.
- **Iterating a hand-written list of state names in the new completeness tests.** D-09: parametrise over `list(JobState)` or the test cannot catch a new member.
- **Adding `case _: pass` anywhere in the new total functions.** Same failure mode as the `raise` wildcard.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Compile-time enum exhaustiveness | A custom `@total_map` decorator, a runtime `__init_subclass__` completeness check, or a test that reflects over module globals | `typing.assert_never` in a `match` | It is the only thing both `ty` and `pyrefly` understand, it costs nothing at runtime, and it fails at **edit time** rather than at test time. `[VERIFIED: executed]` |
| Deriving "busy" from "active" | A fourth hand-written literal list | `frozenset` difference: `BUSY_STATES = ACTIVE_STATES - {JobState.AWAITING_FLIP}` | D-04. A derived set cannot drift from its parent. |
| Partition invariant (every member in exactly one of two sets) | A module-level `assert` at import, or a custom validator | The D-09 parametrised test with `^` (XOR) | An import-time assert with `python -O` semantics is fragile; a parametrised test names the offending member in the failure output. |
| Passing state classification to templates | `{% set active_states = [...] %}` or a Jinja global holding the frozenset | `@property` on `Job` | The template asks the object a question instead of re-implementing the rule. |
| Enum-to-string for SQLite | A custom serialiser | `StrEnum` + the existing `JobState(row[3])` round-trip | Already works; do not touch it. Phase 22 owns persistence. |
| Detecting a feeder source | A second substring rule "just for this call site" | `classify_source()` | This is literally the C-06 defect. |
| Result value objects | A dict with documented keys, or a `TypedDict` | `@dataclass` | `TypedDict` gives no attribute access and no runtime identity; the codebase already standardises on `@dataclass` for all five existing value types. |

**Key insight:** In this phase the "custom solution" is almost always *a second copy of a rule that already exists*. The value of the phase is not that the new code is cleverer — it is that there is exactly one of everything, and that the type checker fails a build when someone adds an eighth `JobState` and forgets its label. Every design choice should be evaluated against "does a missing member fail `ty` and `pyrefly` at edit time, or does it fail silently?"

---

## SANE Source Strings (CTR-04)

Harvested on 2026-09-10 from the SANE man pages **installed on this machine** and from the live `test` backend, not from memory.

### The live `test` backend (the exact case C-06 reproduced against)

```
$ scanimage -d test:0 --help
    --source Flatbed|Automatic Document Feeder [Flatbed]
        If Automatic Document Feeder is selected, the feeder will be 'empty'
        after 10 scans.
```
`[VERIFIED: executed — scanimage on this machine]`

This is the ten-page feeder from C-06's experiment. `_is_adf_source("Automatic Document Feeder")` is **False** today.

### Inventory from installed backend man pages

| Backend | Source values | Notes |
|---------|--------------|-------|
| `sane-test` | `Flatbed`, `Automatic Document Feeder` | The C-06 reproduction case |
| `sane-canon_dr` | `Flatbed`, `ADF Front`, `ADF Back`, `ADF Duplex` | Canon DR series — very common for a paperless bridge |
| `sane-epjitsu` | `Flatbed`, `ADF Front`, `ADF Back`, `ADF Duplex` | Fujitsu USB (ScanSnap S300/S1300) |
| `sane-fujitsu` | `Flatbed`, `ADF Front`, `ADF Back`, `ADF Duplex`, **`Card Duplex`** | `Card Duplex` contains "duplex" but **not** "adf" |
| `sane-kodak` | `Flatbed`, `ADF Front`, `ADF Back`, `ADF Duplex` | |
| `sane-sharp` | `Automatic Document Feeder`, **`Transparency Adapter`** | TA is single-page, not a feeder |
| `sane-bh` | `Automatic Document Feeder`, **`Manual Feed Tray`** | Bell+Howell; "Manual Feed Tray" has "feed" but not "feeder" |
| `sane-epson` / `epson2` / `epsonds` | `Flatbed` + installed-option-dependent feeder names; `--adf-mode simplex\|duplex` is a *separate* option | The feeder name is commonly "Automatic Document Feeder" |
| `sane-hp5590` (web) | `Flatbed`, `ADF`, `ADF Duplex`, `TMA Slides`, `TMA Negatives` | `[CITED: sane-project.org/man/sane-hp5590.5.html]` |
| `sane-umax` (web) | `Automatic Document Feeder` | `[CITED: manpages.debian.org scanadf(1)]` |
| `sane-pixma` (Canon) | Feeder exposed as "Automatic Document Feeder" | `[CITED: mankier.com/5/sane-pixma]` |
| Brother | `Automatic Document Feeder(left aligned)`, `Automatic Document Feeder(centrally aligned)` | `[CITED: .planning/reviews/2026-09-09-code-review.md § C-06]` |
| SANE Standard 2.0 draft §4.5.7 | Well-known values: **`Flatbed`**, **`Transparancy Adapter`** *(sic — spelled that way in the standard)*, **`Automatic Document Feeder`** | `[CITED: sane-project.gitlab.io/standard/draft-2/api.html]` |

`[VERIFIED: executed — man -k '^sane-' harvest on this machine]` for the first eight rows.

### The rule order that works

```python
class SourceKind(StrEnum):
    """What kind of scan source a SANE source name denotes."""

    FLATBED = "FLATBED"
    FEEDER = "FEEDER"
    FEEDER_DUPLEX = "FEEDER_DUPLEX"
    AUTO = "AUTO"
    UNKNOWN = "UNKNOWN"


_FEEDER_TOKENS = ("adf", "document feeder", "feeder")


def classify_source(source: str) -> SourceKind:
    """Classify a SANE source name. The only such rule in the codebase."""
    lower = source.strip().lower()
    if lower == "auto":                       # EXACT — see Pitfall 3
        return SourceKind.AUTO
    if "duplex" in lower:                     # before the feeder tokens
        return SourceKind.FEEDER_DUPLEX
    if any(token in lower for token in _FEEDER_TOKENS):
        return SourceKind.FEEDER
    if "flatbed" in lower:
        return SourceKind.FLATBED
    return SourceKind.UNKNOWN
```

Verify this table in the parametrised test (success criterion 2 names the first six explicitly):

| Input | Expected | Today's `_is_adf_source` | Routing change? |
|-------|----------|:---:|:---:|
| `"Flatbed"` | `FLATBED` | False | no |
| `"Automatic Document Feeder"` | `FEEDER` | **False** | **YES — this is D-11 / C-06** |
| `"Automatic Document Feeder(left aligned)"` | `FEEDER` | **False** | **YES — D-11** |
| `"Document Feeder"` | `FEEDER` | **False** | **YES — D-11** |
| `"ADF"` | `FEEDER` | True | no |
| `"ADF Front"` | `FEEDER` | True | no |
| `"ADF Back"` | `FEEDER` | True | no |
| `"ADF Duplex"` | `FEEDER_DUPLEX` | True | no |
| `"Adf-duplex"` | `FEEDER_DUPLEX` | True | no |
| `"Auto"` / `"auto"` | `AUTO` | False | no (the `== "Auto"` override at `sane_backend.py:497` still governs) |
| `"Transparency Adapter"` | `UNKNOWN` | False | no (D-11: UNKNOWN keeps single-page) |
| `"TMA Slides"` / `"TMA Negatives"` | `UNKNOWN` | False | no |
| `"Manual Feed Tray"` | `UNKNOWN` | False | no ("feed" ≠ "feeder") |
| `"Card Duplex"` | `FEEDER_DUPLEX` | **False** | **YES — see Ambiguity A** |
| `"Manual Duplex"` | `FEEDER_DUPLEX` | **False** | **YES — see Ambiguity B** |
| `"ADF Manual Duplex"` | `FEEDER_DUPLEX` | True | no |

### Three genuine ambiguities the planner must decide on

**Ambiguity A — `"Card Duplex"` (Fujitsu).** Contains "duplex", contains no feeder token. Under the rule above it becomes `FEEDER_DUPLEX` and starts taking the `multi_scan()` path. This is almost certainly correct behaviour (a card-feed path *is* a sheet path), but it is a second routing change beyond the one D-11 names. **Recommendation:** accept it and list it in the parametrised test so the change is visible and deliberate; the alternative (requiring a feeder token *and* "duplex") would mis-route real Fujitsu hardware.

**Ambiguity B — `"Manual Duplex"`, the project's own pseudo-source.** `docs/how-to/set-up-adf-duplex.md:60` documents `source = "Manual Duplex"` — no "adf". Today `_is_adf_source` returns False, so each manual-duplex pass takes `dev.start()`/`dev.snap()` and yields **one page per pass**. Under the new rule it becomes `FEEDER_DUPLEX` and each pass uses `multi_scan()`.

However the exposure is narrow: `sane_backend.py:472-485` classifies `effective_source`, not `settings.source`. On any device that exposes a `source` option, `"Manual Duplex"` is not in `available_sources`, so it either falls back to `"Auto"` (routed by `auto_source_mode`, unchanged) or raises `ScanError`. The new behaviour only reaches a device with **no** `source` option at all. `[VERIFIED: read sane_backend.py:458-505]`

Phase 25 (DPLX-01) deletes the `source`-overloading entirely, so this is transient either way. **Recommendation:** accept, note it in the test table, and do not build a special case for it — a special case would be work Phase 25 immediately throws away, exactly the reasoning CONTEXT.md applies to `_is_manual_duplex`.

**Ambiguity C — `"ADF Back"` and the FEEDER/slug split.** `"ADF Back"` is unambiguously a feeder for *routing* purposes (`multi_scan()` is right). But it must **not** slug to `adf-simplex`, or it collides with `"ADF Front"` — the N-09 defect, pinned by `tests/test_auto_profiles.py:57-61`. This is not an ambiguity in the classifier; it is the reason `source_to_slug` cannot be a thin wrapper. See Pitfall 4.

### Delegating the two existing rules

**`sane_backend._is_adf_source` (`:158-160`)** — the backend only needs the boolean "use `multi_scan()`". Give `SourceKind` a total property so the question is asked once:

```python
    @property
    def uses_feeder(self) -> bool:
        """Whether this kind of source feeds a stack of sheets."""
        match self:
            case SourceKind.FEEDER | SourceKind.FEEDER_DUPLEX:
                feeds = True
            case SourceKind.FLATBED | SourceKind.AUTO | SourceKind.UNKNOWN:
                feeds = False
            case _:
                assert_never(self)
        return feeds
```

Then `use_adf = classify_source(effective_source).uses_feeder`, and the `if effective_source == "Auto":` override at `:497-504` **stays exactly as it is** (Phase 24 owns it). Because `SourceKind.AUTO.uses_feeder` is `False` and the override then sets `use_adf` from `auto_source_mode`, the `"Auto"` path is byte-for-byte unchanged.

**`auto_profiles.source_to_slug` (`:47-58`)** — refactor to *build on* the classifier while keeping the slug distinctions:

```python
def source_to_slug(source: str) -> str:
    """Convert a SANE source name to a profile slug."""
    lower = source.lower()
    match classify_source(source):
        case SourceKind.AUTO:
            slug = "auto-scan"
        case SourceKind.FLATBED:
            slug = "flatbed-scan"
        case SourceKind.FEEDER_DUPLEX:
            slug = "adf-duplex"
        case SourceKind.FEEDER:
            # "ADF Back" is a distinct source from "ADF Front" and must not
            # collapse onto the same profile name.
            slug = _slugify(lower) if "back" in lower else "adf-simplex"
        case SourceKind.UNKNOWN:
            slug = _slugify(lower)
        case _:
            assert_never(...)
    return slug
```

**Checked against all nine existing slug tests** (`tests/test_auto_profiles.py:25-61`): `Flatbed`→`flatbed-scan` ✓, `ADF`→`adf-simplex` ✓, `Automatic Document Feeder`→`adf-simplex` ✓, `ADF Duplex`→`adf-duplex` ✓, `Adf-duplex`→`adf-duplex` ✓, `ADF Front`→`adf-simplex` ✓, `Auto`→`auto-scan` ✓, `auto`→`auto-scan` ✓, `ADF Back`→`adf-back` (not simplex/duplex) ✓. **All nine stay green with zero test edits.**

---

## Duplication Inventory (CTR-01) — the complete call-site list

Every site that must read from `vocabulary.py` when the phase is done. Line numbers verified 2026-09-10.

### State labels — four vocabularies, two kinds

| # | Site | Kind | Becomes |
|---|------|------|---------|
| 1 | `web/app.py:35-42` `_STATE_LABELS` (keyed by raw `str`, silent fallthrough at `:48`) | short labels | deleted; filter re-backed by `state_label()` |
| 2 | `cli.py:111-115` `_event_labels` (keyed by `PipelineEvent`) | progress prose | deleted; `status_callback` reads `progress_label()` via `event.job_state`, with one explicit `SCANNING_REVERSE` case |
| 3 | `web/templates/partials/status.html:9-19` | **same prose as #2**, re-spelled as an `if/elif` over `job.state.value` literals | `{{ job.state \| progress_label }}` inside a single `job.is_busy` branch |
| 4 | `web/templates/index.html:59` | button text, a third spelling | `job.is_busy` / `job.state == JobState.AWAITING_FLIP` |

### Active-state lists — three lists, and they are not the same list

| # | Site | Members | Becomes |
|---|------|---------|---------|
| 5 | `partials/status.html:1` | 5, includes `AWAITING_FLIP` | `job.is_active` |
| 6 | `index.html:58` | 5, includes `AWAITING_FLIP` | `job.is_active` |
| 7 | `index.html:59` | 4, **excludes** `AWAITING_FLIP` | `job.is_busy` (this is `BUSY_STATES`, D-04) |

### Event → state mapping

| # | Site | Becomes |
|---|------|---------|
| 8 | `worker.py:222-233` `if/elif` over four events | `PipelineEvent.job_state` + the `ACTIVE_STATES` guard (Pattern 2) |

### Source classification

| # | Site | Rule | Becomes |
|---|------|------|---------|
| 9 | `sane_backend.py:158-160` | `"adf" in source.lower()` | `classify_source(...).uses_feeder` |
| 10 | `auto_profiles.py:47-58` | also recognises `"document feeder"`, `"feeder"` | builds on `classify_source()` |

### Confirmed NOT duplication sites

- **`static/app.js` does not hard-code state strings.** M-05 says the JavaScript "implies it again"; it does not — the file is purely HTMX-lifecycle driven. One fewer site. `[VERIFIED: CONTEXT.md § Existing Code Insights, re-confirmed by grep]`
- **`web/routes.py` never references `JobState`.** It only passes `Job` objects into the template context. `[VERIFIED: grep — 0 matches]`
- **`cli.py:238` and `cli.py:262`** print `j.state.value` raw, in `saneless jobs`. `:238` is inside `--json` output and **must stay raw** (a machine contract). `:262` is the human table. Success criterion 1 says "the worker, web templates, and CLI all read them from the same module" — the CLI's *progress prose* does, via site #2. Changing `:262` to a label would alter CLI output that a user sees today, which the discretion constraint forbids, and Phase 23's criterion 2 ("`saneless jobs` renders `FALLBACK` distinctly") is already satisfied by raw values. **Recommendation: leave both alone and say so in the plan** so the verifier does not read it as an omission.

---

## Sentinel Inventory (CTR-03) — the `"fallback"` string, end to end

| Site | What it is |
|------|-----------|
| `src/saneless/paperless.py:87-88` | Docstring documenting `"fallback"` as a return value |
| `src/saneless/paperless.py:155` | `return "fallback"` after retries are exhausted and the PDF is copied |
| `src/saneless/pipeline.py:432` | `if task_uuid != "fallback":` — chooses polling vs. synthesising a dict |
| `src/saneless/pipeline.py:438` | `result = {"status": "FALLBACK", "path": ...}` — the synthesised dict |
| `tests/test_paperless.py:231` | `assert result == "fallback"` |
| `tests/test_paperless.py:425` | `assert result == "fallback"` |
| `tests/test_paperless.py:452` | `assert result == "fallback"` |
| `docs/explanation/consume-directory-fallback.md:66` | **"The job status shows FALLBACK."** — false today (D-13) |

Plus the five `upload_document` **stub sites** in `tests/` that must move in lockstep (below).

**Verification commands** (D-13 — a naive `grep -rn fallback docs/` will fail forever and does not test the criterion):

```bash
grep -rn '"fallback"' src/ tests/          # must return nothing
grep -rn 'FALLBACK' docs/ README.md        # must return nothing claiming a job status
grep -rn 'FALLBACK' src/                   # only ScanOutcome.FALLBACK should remain
```

`grep -rn FALLBACK docs/ README.md` currently returns exactly one hit: `docs/explanation/consume-directory-fallback.md:66`. `[VERIFIED: executed]` The same file already states the *true* consequence at `:62` ("only the PDF file is saved. Title, tags, and correspondent metadata ... are lost"), so the `:66` rewrite has a factual anchor to lean on.

---

## Common Pitfalls

### Pitfall 1: `PLR0911` kills the obvious exhaustiveness form

**What goes wrong:** `state_label()` and `progress_label()` written as `match` with one `return` per arm produce `PLR0911 Too many return statements (7 > 6)`. CI goes red on a change that passes both type checkers and all tests.
**Why it happens:** `JobState` has 7 members; ruff's `max-returns` default is 6; `PLR` is in this project's `select` list.
**How to avoid:** assign-in-arm + one trailing `return` (Pattern 1). Do **not** add `# noqa` or raise `max-returns` — CLAUDE.md forbids both.
**Warning signs:** the function is a pure lookup with more than six branches. `PipelineEvent.job_state` (6) and `error_message()` (5) are under the limit today, but `PipelineEvent` gains a 7th member in Phase 25 — use the single-return form uniformly.
`[VERIFIED: executed]`

### Pitfall 2: `ty` and `pyrefly` disagree about which patterns narrow

**What goes wrong:** a `match` written with raw string patterns or `.value` patterns passes `ty check` cleanly and then fails `pyrefly check` with `Argument JobState is not assignable to parameter arg with type Never` — the `assert_never` reports the *whole enum type*, not a missing member, which reads like a bug in the code rather than a pattern-syntax problem. Conversely a guarded arm passes `pyrefly`... no: it passes `ty` and fails `pyrefly`.
**Why it happens:** a `StrEnum` member is also a `str`. `ty` treats a string literal pattern as matching the corresponding enum member and narrows; `pyrefly` does not narrow at all. Guards are the mirror image — `pyrefly` correctly refuses to count a conditionally-taken arm as consuming the member, `ty` incorrectly does.
**How to avoid:** bare member patterns only. No `.value`. No string literals. No `if` guards in the case clause.
**Warning signs:** `pyrefly` reports the bare enum type (`JobState`) rather than a specific `Literal[JobState.X]` — that means "narrowing didn't happen", not "you missed a member".
`[VERIFIED: executed against ty 0.0.80 and pyrefly 1.2.0]`

### Pitfall 3: `"Automatic Document Feeder"` contains the substring `"auto"`

**What goes wrong:** a `classify_source` that checks `if "auto" in lower: return SourceKind.AUTO` before the feeder tokens classifies the single most important C-06 case as `AUTO`. `AUTO` falls to the single-page branch (D-11 keeps `UNKNOWN`/`AUTO` routing unchanged), so the ten-page stack yields one page — **the exact defect this requirement exists to fix**, now hidden behind a "single classifier" that looks correct.
**Why it happens:** `"automatic document feeder".find("auto") == 0`.
**How to avoid:** exact match — `if lower == "auto"`. `auto_profiles.source_to_slug:48` already does it this way; copy that, not the substring habit from `_is_adf_source`.
**Warning signs:** the parametrised classifier test asserts `classify_source("Automatic Document Feeder") is SourceKind.FEEDER` and it returns `AUTO`. Make sure that row is in the table.
`[VERIFIED: string analysis + the harvested source inventory]`

### Pitfall 4: making `source_to_slug` a thin wrapper over `classify_source`

**What goes wrong:** `"ADF Back"` classifies as `FEEDER` (correct, for routing), so a thin wrapper slugs it `adf-simplex` — the same slug as `"ADF Front"`. `tests/test_auto_profiles.py:57-61` goes red, and the N-09 collision defect is reintroduced under the banner of de-duplication.
**Why it happens:** "which scan path do I take?" and "what do I name this profile?" are different questions with different equivalence classes. Routing wants `{Front, Back}` to be the same; naming wants them distinct.
**How to avoid:** the layered form above — `classify_source` decides the kind, `source_to_slug` adds the naming distinctions on top. CTR-04's "only classification rule" is satisfied because the *classification* happens once; the slug is a separate, downstream naming decision.
**Warning signs:** `test_adf_back_fallback` fails, or `generate_profiles` on a Canon DR emits fewer profiles than the device has sources.

### Pitfall 5: applying `PipelineEvent.DONE.job_state` in `_status_cb`

Fully described in Pattern 2. **Warning signs:** the browser test sees the "✓ Done" panel before the pipeline returns; `tests/test_worker.py::TestWorkerEnumDispatch` still passes (it only asserts `ASSEMBLING in states_seen`), so this will **not** be caught by the existing suite. A verifier reading only the test output would miss it. If the planner wants coverage, assert the *order* of `states_seen` in that test.

### Pitfall 6: a raising label filter freezes the UI silently

**What goes wrong:** D-10 makes `humanize_state`/`state_label` raise on an unrecognised value. If one ever reaches a template, Jinja propagates the `AssertionError`, Starlette returns a bare 500, and htmx 2 does not swap non-2xx bodies — so the status area stops updating with no message while the 1 s poll hammers the server with 500s.
**Why it happens:** htmx's default `responseHandling` swaps only 2xx; polling continues until the element is removed or a 286 is returned.
**How to avoid:** D-10's safety argument, which holds: `Job.state` is only ever built via `JobState(row[3])` (`job.py:187`, `:262`), which raises inside the store for any unknown value. Record that reasoning in the test docstring so a future reader does not "helpfully" restore a fallback. Also pass the **enum**, not `.value`, from every template so the typed contract is honest.
**Warning signs:** a browser test where the status area never leaves its first rendered state.
`[VERIFIED: executed — the 500 and the propagation were reproduced]`

### Pitfall 7: forgetting that `run_pipeline` emits `SCANNING` too

Minor, but worth naming: `worker.py:203` sets `SCANNING` *before* calling `run_pipeline`, and the pipeline then emits `PipelineEvent.SCANNING`. Under the recommended `_status_cb` this becomes a second, idempotent `update_state(SCANNING)` — a redundant SQLite `UPDATE` + `commit` per job. Harmless today, but Phase 22 puts an `RLock` on every store method, so it is one more lock acquisition. If the planner wants zero redundancy, skip the write when the state is unchanged; do not special-case `SCANNING` by name.

### Pitfall 8: `filterwarnings = ["error"]` turns any new import warning into a red suite

`vocabulary.py` uses only `enum`, `dataclasses`, and `typing`, so there is no exposure here — but the constraint is live (Phase 20) and worth remembering if the planner reaches for anything else.

---

## Code Examples

### Module skeleton for `vocabulary.py`

```python
"""
The words the system uses about itself.

This module is a leaf: it must not import from job.py, pipeline.py, or
anything under web/. Every consumer imports from here, never the reverse.
"""

from __future__ import annotations

from enum import StrEnum
from typing import assert_never

__all__ = [
    "ACTIVE_STATES",
    "BUSY_STATES",
    "TERMINAL_STATES",
    "ErrorCategory",
    "JobState",
    "ScanOutcome",
    "classify_error",
    "error_message",
    "progress_label",
    "state_label",
]


class JobState(StrEnum):
    """States in the scan job lifecycle."""

    PENDING = "PENDING"
    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"
    ERROR = "ERROR"


ACTIVE_STATES: frozenset[JobState] = frozenset({
    JobState.PENDING,
    JobState.SCANNING,
    JobState.AWAITING_FLIP,
    JobState.ASSEMBLING,
    JobState.UPLOADING,
})
TERMINAL_STATES: frozenset[JobState] = frozenset({JobState.DONE, JobState.ERROR})
# Derived, never hand-written: "the machine is working" excludes
# "we are waiting for the human".
BUSY_STATES: frozenset[JobState] = ACTIVE_STATES - {JobState.AWAITING_FLIP}
```

Note `__all__` is alphabetically sorted — ruff's `RUF022` will otherwise fix it for you; `job.py:19` follows the same convention.

`classify_error` moves verbatim from `worker.py:130-149` (it never touches `self`), losing the `self` parameter. `[VERIFIED: read worker.py]`

### The D-09 completeness tests

```python
@pytest.mark.parametrize("state", list(JobState))
def test_every_state_has_a_label(state: JobState) -> None:
    """Every JobState member has a non-empty, non-raw label."""
    label = state_label(state)
    assert label
    assert label != state.value        # not merely the raw member name


@pytest.mark.parametrize("state", list(JobState))
def test_every_state_is_classified_exactly_once(state: JobState) -> None:
    """ACTIVE_STATES and TERMINAL_STATES partition JobState exactly."""
    assert (state in ACTIVE_STATES) ^ (state in TERMINAL_STATES)


@pytest.mark.parametrize("category", list(ErrorCategory))
def test_every_category_has_a_message(category: ErrorCategory) -> None:
    """Every ErrorCategory member has a user-facing message."""
    assert error_message(category)


@pytest.mark.parametrize("event", list(PipelineEvent))
def test_every_event_maps_to_a_state_or_explicitly_to_none(
    event: PipelineEvent,
) -> None:
    """PipelineEvent.job_state is total: it returns without raising."""
    result = event.job_state
    assert result is None or isinstance(result, JobState)
```

Parametrising over `list(JobState)` — never a hand-written list — is what makes a new member fail the suite automatically (D-09).

### The D-10 replacement for `tests/test_web.py:372-378`

```python
def test_state_label_unit() -> None:
    """state_label converts JobState members to human-readable labels."""
    assert state_label(JobState.DONE) == "Complete"
    assert state_label(JobState.ERROR) == "Failed"
    assert state_label(JobState.SCANNING) == "Scanning"
    assert state_label(JobState.AWAITING_FLIP) == "Waiting for flip"


def test_state_label_rejects_unknown_values() -> None:
    """
    An unrecognised value raises instead of silently echoing itself.

    This is safe at runtime because Job.state is always a JobState:
    job.py builds every Job through JobState(row[3]), which already
    rejects anything the enum does not name.
    """
    with pytest.raises(AssertionError):
        state_label("UNKNOWN")  # type: ignore-free: the call is deliberately wrong
```

`assert_never` raises `AssertionError`, not `ValueError`. `[VERIFIED: executed]`

⚠️ The comment above is illustrative only — **do not write an actual `# type: ignore`**. Passing a raw `str` where `JobState` is expected will be flagged by both checkers. The clean way to write this test without a suppression is to construct the bad value so the checkers cannot see its type, e.g. `bad: JobState = cast("JobState", "UNKNOWN")` — `typing.cast` is not a suppression comment and is idiomatic for "I am deliberately violating this contract to test the guard". The planner should confirm `cast` passes both checkers here before committing (it is expected to; `cast` is unchecked by design).

### The D-11 behaviour-change test (a test that could not have passed before)

```python
def test_automatic_document_feeder_scans_the_whole_stack(...) -> None:
    """A feeder named 'Automatic Document Feeder' uses multi_scan, not snap."""
    mock_dev._multi_scan_pages = [img1, img2, img3]
    settings = ScanSettings(
        source="Automatic Document Feeder", resolution=300, mode="color"
    )
    pages = list(backend.scan_pages("test:0", settings))
    assert len(pages) == 3          # was 1 before this phase
```

Model it on the existing `tests/test_scanner.py:503-540` ADF tests, and remember to add `"Automatic Document Feeder"` to the fake's `available_sources` constraint list (see `_FakeSaneDevice` / the `raw_options` constraint at `tests/test_scanner.py:115`), or `sane_backend.py:472-485` will reject the source before routing is reached.

---

## Regression Risk (research question 6) — a concrete, measured list

### A. `upload_document -> UploadResult` — **18 tests, MEASURED**

Method: temporarily changed `tests/conftest.py:136` to return a non-`str` and `src/saneless/pipeline.py:432` to read an attribute off the result, then ran the suite. Both files restored; tree verified clean.

**Result: `18 failed, 314 passed, 8 deselected in 26.93s`**

| File | Failing tests |
|------|--------------|
| `tests/test_cli.py` | `TestScanCommand::test_scan_happy_path` |
| `tests/test_pipeline.py` | `TestRunPipeline::test_run_pipeline_happy_path`, `::test_run_pipeline_temp_cleanup`, `::test_run_pipeline_calls_poll`, `::test_run_pipeline_status_callback` |
| | `TestPipelineThumbnail::test_thumbnail_callback_called_flatbed`, `::test_no_thumbnail_callback_ok` |
| | `TestPipelineEmptyPageFilter::test_empty_pages_filtered`, `::test_custom_thresholds_from_profile` |
| | `TestManualDuplex::test_manual_duplex_happy_path`, `::test_duplex_match_still_interleaves_normally`, `::test_manual_duplex_empty_page_after_interleave`, `::test_manual_duplex_thumbnail_from_first_page`, `::test_manual_duplex_flip_event_wait` |
| | `TestExifStripped::test_exif_stripped_before_pdf` |
| | `TestEmptyPageDetectionToggle::test_empty_page_filter_skipped_when_disabled` |
| | `TestFlatbedStillWorks::test_flatbed_single_page_with_thumbnail` |
| | `TestPipelineEventEnum::test_pipeline_emits_enum_events` |

`[VERIFIED: executed]`

**There are five distinct `upload_document` stub sites, not one.** Every one must move in the same commit:

| Site | What it is |
|------|-----------|
| `tests/conftest.py:136` | `mock_paperless` fixture — `upload_document.return_value = "mock-task-uuid"`. Shared by `test_pipeline.py` (52 refs) and `test_worker.py` (48 refs). |
| `tests/test_pipeline.py:153` | `test_run_pipeline_calls_poll` overrides it with `"task-uuid-123"` |
| `tests/test_cli.py:142` | `MockPaperlessClient.upload_document(...) -> str` returning `"mock-task-uuid"` — a hand-written class, not a MagicMock |
| `tests/test_cli.py:253` | `FailPaperless.upload_document(...) -> None` — raises; only its annotation needs a look |
| `tests/test_paperless.py:231, :425, :452` | Three `assert result == "fallback"` assertions on the real client |

Note that `tests/test_web.py` also has a fixture named `mock_paperless` (`:103`) but it is unrelated — it patches `get_tags`/`get_correspondents`, not `upload_document`.

**This is the phase's `20-01` moment.** In Phase 20 a dependency and its config had to land in one commit or every test run broke. Here the producer signature, its consumer, and all five stubs must land in one commit or 21 tests break.

### B. `run_pipeline -> ScanResult` — 4 assertions read the dict today

| Site | Assertion | Becomes |
|------|-----------|---------|
| `tests/test_pipeline.py:51` | `assert result is not None` | `assert result.outcome is ScanOutcome.SUCCESS` |
| `tests/test_pipeline.py:496` | `assert result["status"] == "DONE"` | `assert result.outcome is ScanOutcome.SUCCESS` |
| `tests/test_pipeline.py:497` | `assert "Page count mismatch: 3 fronts, 2 backs" in result["warning"]` | `... in result.warning` |
| `tests/test_pipeline.py:531` | `assert "warning" not in result` | `assert result.warning is None` |

Plus `tests/test_worker.py`'s ~11 `fake_pipeline` stubs annotated `-> None` — these are fine at runtime (the worker discards the return value) but the planner should sweep them for accuracy.

### C. `humanize_state` → `state_label` — 3 test sites, one deliberate

| Site | Effect |
|------|--------|
| `tests/test_web.py:36` | `from saneless.web.app import create_app, humanize_state` — breaks if the name changes |
| `tests/test_web.py:372-377` | four label assertions — survive unchanged if the labels are preserved (they must be) |
| `tests/test_web.py:378` | `assert humanize_state("UNKNOWN") == "UNKNOWN"` — **deliberately replaced (D-10)** |
| `tests/test_web.py:311` | `assert "Complete" in response.text` (history render) — survives |

`src/saneless/web/app.py:28` `__all__ = ["create_app", "humanize_state"]` must be updated if the name changes.

### D. Template rewrites — the tests that render them

| Site | Assertion |
|------|-----------|
| `tests/test_web.py:131`, `:300` | `'id="scan-btn"' in response.text` |
| `tests/test_web.py:290-300` | `"disabled" in response.text` for a `SCANNING` job |
| `tests/test_web.py:303-311` | `"Complete" in response.text` in the history partial |
| `tests/test_web.py:313+` | `test_status_no_inline_scripts` — DONE/ERROR branches must keep their markup |
| `tests/test_browser.py` (8 tests, `-m browser`) | Deselected from the standard run. **Run `uv run pytest -m browser` after the template edits** — CLAUDE.md forbids treating these as manual-only. |

### E. `_categorize_error` move — **zero test breakage**

`grep -rn "_categorize_error" tests/` returns **no matches**. `[VERIFIED: executed]` The CONTEXT discretion item ("whether `classify_error` keeps a thin `ScanWorker._categorize_error` delegate for test compatibility") therefore resolves cleanly: **no delegate is needed.** `tests/test_worker.py:574-600` tests categories end-to-end through the worker (running the pipeline and reading `job.error_category`), which is unaffected.

### F. Worker `_status_cb` collapse

| Site | Effect |
|------|--------|
| `tests/test_worker.py:257` | `request.status_callback(PipelineEvent.AWAITING_FLIP)` — the clear-before ordering must survive |
| `tests/test_worker.py:513`, `:559` | `ASSEMBLING`/`UPLOADING` + `wait_transition` — the set-after ordering must survive |
| `tests/test_worker.py:715-756` | `test_wait_transition_returns_true` / `_timeout` |
| `tests/test_worker.py:968-1010` | `TestWorkerEnumDispatch` — asserts `JobState.ASSEMBLING in states_seen`; **will NOT catch Pitfall 5** |

### G. Classifier delegation — **zero existing test breakage expected**

- No existing scanner test uses `"Automatic Document Feeder"` as a *scan source* — the D-11 change cannot break an existing scanner test, which is why it needs a new one. `[VERIFIED: grep of tests/test_scanner.py]`
- All nine `source_to_slug` tests (`tests/test_auto_profiles.py:25-61`) stay green under the layered refactor above. `[VERIFIED: traced by hand against each assertion]`
- `tests/test_scanner.py:115` `raw_options` constraint list is `["Flatbed", "ADF", "ADF Duplex"]` — the new test needs a fake whose constraint list includes the long feeder name.

---

## Ordering and Atomicity (research question 1)

Each numbered item is a candidate commit. **The suite must be green at every one.**

| # | Commit | Contents | Why it is safe alone |
|---|--------|----------|---------------------|
| 1 | **Vocabulary lands** | New `vocabulary.py` (`JobState`, `ErrorCategory`, `ScanOutcome`, the three frozensets, `state_label`, `progress_label`, `error_message`, `classify_error`); `job.py` re-exports + `Job.is_active`/`is_busy`; the D-09 parametrised tests | Pure addition. `job.py`'s `__all__` and every `from saneless.job import JobState` keep working (CTR-01, criterion 4). Nothing else is touched. |
| 2 | **Web rewiring** | `app.py`: delete `_STATE_LABELS`, re-back the filter(s), register `progress_label`; `status.html`, `index.html`, `history.html`; `tests/test_web.py:378` (D-10) | **Atomic:** the filter change and `test_web.py:378` must land together. Template edits ride along because they change what is passed to the filter. |
| 3 | **Event mapping + CLI + worker** | `PipelineEvent.job_state`; `cli.py` `_event_labels` deleted; `worker._status_cb` collapsed with the `ACTIVE_STATES` guard | **Atomic:** `_event_labels` deletion and the `status_callback` rewrite are the same edit. `tests/test_cli.py:205-207` and the worker transition tests must stay green with no edits. |
| 4 | **ErrorCategory move** | `classify_error` moved off `ScanWorker`; `worker.py` import updated; the `ErrorCategory` × `error_message` parametrised test (if not already in #1) | Zero test references to `_categorize_error`. Optionally wire `error_message()` into the worker's failure log line (D-12 permits it, provided no test asserts that log text — `tests/test_worker.py` asserts on `job.error`, not the log). |
| 5 | **Classifier lands** | `SourceKind` + `classify_source()` + `uses_feeder` in `scanner/base.py`; the parametrised classifier test | Pure addition; nothing calls it yet. |
| 6 | **Classifier wired — ⚠️ the D-11 behaviour change** | `sane_backend._is_adf_source` → `classify_source(...).uses_feeder`; `auto_profiles.source_to_slug` layered on the classifier; the new "Automatic Document Feeder yields N pages" test | **Atomic** (both delegations plus the new test). This is the phase's one intentional behaviour change — the plan must say so, and the verifier must not read it as drift. |
| 7 | **`UploadResult`** | `paperless.py:87-88` docstring, `:155` return; `pipeline.py:432` branch; **all five test stub sites** | **The tightest atomicity constraint in the phase — 18 measured failures otherwise.** |
| 8 | **`ScanResult` / `ScanOutcome`** | `run_pipeline`'s `-> dict` deleted, both return sites (`:386`, `:443`), the synthesised `{"status": "FALLBACK"}` dict at `:438` deleted; `tests/test_pipeline.py:51, 496, 497, 531` | Needs #7's `UploadResult` to derive `ScanOutcome`. |
| 9 | **Docs** | `docs/explanation/consume-directory-fallback.md:66` rewritten (D-13) | No code impact. Could be folded into #8. |

**Rules the ordering must respect:**
- `vocabulary.py` + `job.py` re-exports land before anything that imports them (CONTEXT discretion).
- `vocabulary.py` imports nothing from `job.py`, `pipeline.py`, or `web/` (D-02).
- `scanner/base.py` must not import `vocabulary.py` (D-03).
- Commits 5→6 and 7→8 have hard producer/consumer dependencies. Commits 2, 3, 4 are mutually independent and can be reordered or parallelised.
- The `worker.py:222-233` clear-before / set-after ordering is preserved exactly in commit 3 (CONTEXT § "Ordering the refactor must respect").

**Which moves must be atomic — the short answer to the planner's question:**
1. **Yes**, deleting `_STATE_LABELS` must land with the `test_web.py:378` edit (the filter's contract changes). The *template* edits are technically separable — `StrEnum` equality means the old `job.state.value` call still resolves — but should ride along so the typed contract is honest.
2. **Yes, hardest**, `upload_document`'s signature must land with all five test stub sites — 18 measured failures otherwise.
3. **Yes**, `run_pipeline`'s signature must land with the four dict assertions.
4. **Yes**, the two classifier delegations must land together (criterion 2: "it is the only classification rule").
5. **No**, `classify_error`'s move is free (zero test references).

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `Enum` + manual `.value` for string persistence | `enum.StrEnum` | Python 3.11 | Already the codebase's idiom; nothing to change |
| `assert isinstance(x, Never)` / `raise NotImplementedError` fall-throughs | `typing.assert_never` | Python 3.11 (`typing_extensions` before) | The exhaustiveness idiom both `ty` and `pyrefly` understand |
| `if/elif` chains over string constants | `match` with bare enum member patterns | Python 3.10 (`match`), narrowing matured since | The only form that narrows under both checkers here |
| mypy/pyright | `ty` + `pyrefly` | Project decision (CLAUDE.md), versions bumped in Phase 20 Plan 05 | The two tools have **different** narrowing behaviour; write to the intersection |
| PEP 758 bracketless `except A, B:` | valid Python 3.14+ | 3.14 | Not needed in this phase, but do not flag it in review if it appears |

**Deprecated / outdated in the review text (do not restore):**
- **M-05's `dict[JobState, str]` enforcement claim** — measurably false against this project's `ty` and `pyrefly` (D-08, reproduced).
- **M-05's claim that `static/app.js` duplicates the state list** — it does not.
- **M-05's `hx-swap-oob` Scan-button suggestion** — Phase 26's.
- **C-03 fix items 1, 3, 5** (`poll_task` raising, `JobState.FALLBACK`, worker-level outcome tests) — Phase 23's.
- **C-06's "safer default" for unrecognised sources** — Phase 24's (D-11).

---

## Runtime State Inventory

This phase is a rename/refactor, so this section is mandatory. The canonical question: *after every file in the repo is updated, what runtime systems still have the old string cached, stored, or registered?*

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| **Stored data** | **The job SQLite database persists `JobState` as `TEXT`** and reads it back through `JobState(row[3])` (`job.py:187`, `:262`). **No `JobState` member is added, removed, or renamed in this phase** (D-06), so every persisted value keeps resolving. `error_category` is persisted the same way (`job.py:188`) and `ErrorCategory`'s members are unchanged — only its defining module moves, which SQLite cannot see. `"fallback"` was **never** persisted: it was a transient return value from `upload_document`, consumed by `pipeline.py:432` and discarded. Verified: the string appears in no `INSERT`/`UPDATE`. | **None.** No data migration. This is the direct payoff of D-06/D-07 — deferring the new members to Phase 23 keeps this phase free of any schema or data concern, which is also why Phase 22 (the migration ladder) can safely follow. |
| **Live service config** | **None.** saneless has no external service holding configuration. Paperless-ngx receives only documents and metadata — it never sees a `JobState`, an `ErrorCategory`, or the `"fallback"` sentinel. Verified: `grep -rn "task_uuid\|upload_document" docs/ README.md` returns nothing, and `paperless.py`'s request bodies carry only `title`/`created`/`correspondent`/`tags`. | **None — verified by reading every `self._client.post`/`get` call in `paperless.py`.** |
| **OS-registered state** | **None.** saneless registers nothing with the OS. The web server is started by `saneless serve`; there is no systemd unit, launchd plist, pm2 process, or Task Scheduler entry in the repo. Verified: no `.service`, `.plist`, or `ecosystem.config.*` anywhere in the tree. | **None — verified by tree inspection.** |
| **Secrets / env vars** | **None affected.** The only env vars are the `SANELESS_*` pydantic-settings prefix (stripped per test by `conftest.py:74`'s autouse `clean_env` fixture) and `SANE_CONFIG_DIR`. No env var name contains `JobState`, `ErrorCategory`, or `fallback`. `settings.paperless.consume_dir` is a **path** setting whose name and value are unchanged — only the sentinel that signalled "we used it" changes shape. | **None — verified by grep of `config.py` field names.** |
| **Build artifacts** | **None.** No package rename, no `pyproject.toml` `name`/entry-point change, no compiled extension, no Docker image tag change. `src/saneless/vocabulary.py` is a new module inside an existing package, picked up by the existing editable install with no reinstall. `docs/` is built by mkdocs from source on demand. | **None.** No `uv sync` or reinstall needed. |

**Additional non-obvious surface specific to this refactor:** the `"fallback"` sentinel and the state strings are also embedded in **Jinja2 templates**, which are read from disk at render time and are *not* Python imports — `ty` and `pyrefly` cannot see them. A template that still says `job.state.value in ["PENDING", ...]` after `vocabulary.py` lands will keep working silently and will keep drifting. **Template edits are therefore the one part of this refactor that no type checker guards**; the browser suite (`-m browser`) and `tests/test_web.py`'s render assertions are the only mechanical evidence. Plan for both.

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 + pytest-timeout 2.4.0 |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (`--strict-config`, `--strict-markers`, `xfail_strict`, `filterwarnings = ["error"]`, `timeout = 60`, `timeout_method = "signal"`) |
| Quick run command | `uv run pytest -q -m "not browser" tests/test_<module>.py` |
| Full suite command | `uv run pytest -m "not browser"` |
| Browser suite | `uv run pytest -m browser` (8 tests, deselected by default) |
| **Measured baseline (2026-09-10)** | **`332 passed, 8 deselected in 26.83s`** on a clean tree |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CTR-01 | Every `JobState` member has a non-raw label | unit (parametrised over `list(JobState)`) | `uv run pytest -q tests/test_vocabulary.py::test_every_state_has_a_label` | ❌ Wave 0 |
| CTR-01 | Every member is in exactly one of ACTIVE/TERMINAL | unit (parametrised, XOR) | `uv run pytest -q tests/test_vocabulary.py::test_every_state_is_classified_exactly_once` | ❌ Wave 0 |
| CTR-01 | `BUSY_STATES` is derived, not hand-written | unit | `uv run pytest -q tests/test_vocabulary.py::test_busy_states_is_active_minus_awaiting_flip` | ❌ Wave 0 |
| CTR-01 | Existing `from saneless.job import JobState, ErrorCategory` still resolves | unit | `uv run pytest -q tests/test_job.py` (existing; imports at `:14`) | ✅ |
| CTR-01 | Worker projects every `PipelineEvent` to a state or explicitly to `None` | unit (parametrised over `list(PipelineEvent)`) | `uv run pytest -q tests/test_pipeline.py::TestPipelineEventEnum` | ✅ (extend) |
| CTR-01 | Worker still transitions on ASSEMBLING/UPLOADING/AWAITING_FLIP with correct `_transition_event` ordering | integration | `uv run pytest -q tests/test_worker.py -k "transition or EnumDispatch or flip"` | ✅ |
| CTR-01 | Templates render labels from the shared vocabulary | integration (TestClient render) | `uv run pytest -q tests/test_web.py -k "humanize or label or history or scan_button"` | ✅ (edit `:378`) |
| CTR-01 | Rendered UI is still correct in a browser | e2e | `uv run pytest -m browser` | ✅ |
| CTR-01 | CLI progress prose is byte-identical | integration | `uv run pytest -q tests/test_cli.py::TestScanCommand::test_scan_status_output` | ✅ |
| CTR-01 | Unrecognised state raises (D-10) | unit | `uv run pytest -q tests/test_vocabulary.py::test_state_label_rejects_unknown_values` | ❌ Wave 0 |
| CTR-02 | `run_pipeline` returns a typed `ScanResult` with an outcome enum and page counts | unit | `uv run pytest -q tests/test_pipeline.py::TestRunPipeline` | ✅ (edit `:51`) |
| CTR-02 | Duplex mismatch returns SUCCESS + warning on the typed result | unit | `uv run pytest -q tests/test_pipeline.py::TestManualDuplex` | ✅ (edit `:496-497`, `:531`) |
| CTR-03 | `upload_document` returns a typed `UploadResult` for both the API and consume-dir paths | unit | `uv run pytest -q tests/test_paperless.py -k "fallback or consume_dir"` | ✅ (edit `:231`, `:425`, `:452`) |
| CTR-03 | The sentinel is absent from `src/` and `tests/` | grep gate | `! grep -rn '"fallback"' src/ tests/` | ❌ Wave 0 (add to plan verification, not to the suite) |
| CTR-03 | No doc claims a `FALLBACK` job status | grep gate | `! grep -rn 'FALLBACK' docs/ README.md` | ❌ Wave 0 |
| CTR-04 | `classify_source` returns the right `SourceKind` for every harvested real-world name | unit (parametrised over the table above) | `uv run pytest -q tests/test_scanner.py::TestClassifySource` | ❌ Wave 0 |
| CTR-04 | **A feeder named "Automatic Document Feeder" yields N pages, not 1** (D-11) | unit | `uv run pytest -q tests/test_scanner.py -k automatic_document_feeder` | ❌ Wave 0 — **this test could not have passed before this phase** |
| CTR-04 | `source_to_slug` still distinguishes ADF Front from ADF Back | unit | `uv run pytest -q tests/test_auto_profiles.py::TestSourceToSlug` | ✅ (must stay green unedited) |
| CTR-05 | Every `ErrorCategory` member has a user message | unit (parametrised over `list(ErrorCategory)`) | `uv run pytest -q tests/test_vocabulary.py::test_every_category_has_a_message` | ❌ Wave 0 |
| CTR-05 | `classify_error` maps each exception type correctly | unit | `uv run pytest -q tests/test_worker.py -k ErrorCategory` | ✅ |
| **all** | Exhaustiveness is actually enforced | type-check gate | `uv run ty check && uv run pyrefly check` | ✅ (Phase 20 CI) |
| **all** | `PLR0911` and the rest of the lint gate | lint gate | `uv run ruff check . && uv run ruff format --check .` | ✅ (Phase 20 CI) |
| **all** | Nothing else regressed | full suite | `uv run pytest -m "not browser"` — must return **332+ passed** | ✅ |

### Sampling Rate

- **Per task commit:** `uv run ruff check . && uv run ty check && uv run pyrefly check && uv run pytest -q -m "not browser" <the touched test module>` — the type-check trio is non-negotiable here because it *is* the phase's enforcement mechanism, and a missing enum arm is invisible to pytest.
- **Per wave merge:** `uv run pytest -m "not browser"` (~27 s) — must be ≥ 332 passed, 0 failed.
- **After any template edit:** additionally `uv run pytest -m browser`. Templates are the one surface no type checker guards.
- **Phase gate:** all five CONTRIBUTING checks green, plus the two grep gates, before `/gsd-verify-work`.

### Wave 0 Gaps

- [ ] `tests/test_vocabulary.py` — new module for the D-09 parametrised completeness tests (`JobState` × label, `JobState` × classification XOR, `ErrorCategory` × message, D-10's raise test, `BUSY_STATES` derivation)
- [ ] `tests/test_scanner.py::TestClassifySource` — new parametrised class covering the harvested source-string table, including the three ambiguities
- [ ] `tests/test_scanner.py` — new "Automatic Document Feeder yields N pages" test; requires a `_FakeSaneDevice` whose `raw_options` source constraint includes the long name (model on `:115` and `:503-540`)
- [ ] `tests/test_pipeline.py::TestPipelineEventEnum` — extend with the `list(PipelineEvent)` × `job_state` totality test
- [ ] Two grep gates in the plan's verification steps (not in the pytest suite): `! grep -rn '"fallback"' src/ tests/` and `! grep -rn 'FALLBACK' docs/ README.md`
- [ ] Framework install: **none needed** — pytest 9.0.2, pytest-timeout 2.4.0, and the `browser` marker are all already configured

---

## Security Domain

This phase changes no authentication, authorisation, session, network, or storage behaviour. It moves enum definitions between modules and replaces a magic string with a dataclass. `security_enforcement` is not set to `false` in `.planning/config.json`, so the section is included for completeness.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | The project has no login by design (REQUIREMENTS.md § Out of Scope: "Multi-user authentication ... Trusted-LAN decision stands") |
| V3 Session Management | no | No sessions. The APPL-09 owner token is Phase 30 and is explicitly *not* an auth mechanism |
| V4 Access Control | no | No change to any route's reachability |
| V5 Input Validation | **marginally** | `classify_source()` accepts an arbitrary `str` from the SANE device and from the config file. It performs only `.strip().lower()` and substring tests, returns a closed enum, and never interpolates the input into a shell, a path, a SQL statement, or HTML. `SourceKind.UNKNOWN` is the safe default. No new validation library needed |
| V6 Cryptography | no | None used or touched |
| V7 Error Handling & Logging | **yes, mildly** | `error_message()` must not leak raw exception text — but D-12 keeps it *out* of the UI entirely this phase, and the status partial keeps rendering `job.error` verbatim exactly as today. Net change: zero |
| V12 Files & Resources | no | No path handling changes. `UploadResult.consume_dir_path` carries a path the client already computed at `paperless.py:151-154` |
| V14 Configuration | no | No config schema change |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation | Status in this phase |
|---------|--------|---------------------|---------------------|
| Server-side template injection | Tampering | Never build templates from user input; use autoescape | Unchanged — all templates are static files; only filter *arguments* change, and Jinja2Templates autoescapes `.html` |
| XSS via unescaped label strings | Tampering | Jinja2 autoescape | The new labels are developer-authored constants, not user input. Do **not** wrap any label in `\| safe` |
| Information disclosure via error text | Info disclosure | Generic user messages, detail in logs | This is precisely U-05 / APPL-04 — **Phase 30's**, deliberately deferred (D-12). No regression here because the current behaviour is preserved byte-for-byte |
| Enum injection from the database | Tampering | `JobState(row[3])` rejects unknown values at the store boundary | Already in place (`job.py:187`, `:262`) and unchanged; it is also what makes D-10 safe |
| Denial of service via a raising template filter | DoS | Ensure filters cannot raise on reachable input | Analysed above (Pitfall 6). A 500 per second on a 1 s poll is a mild self-DoS; the mitigation is the store-boundary guard, which already exists |

**Net assessment:** no new attack surface, no new dependency, no new network or filesystem interaction. The one item worth a line in the plan is "do not `\| safe` any label".

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Brother spells its feeder `"Automatic Document Feeder(left aligned)"` / `"(centrally aligned)"` | SANE Source Strings | Low. Taken from the project's own C-06 finding, not verified against hardware here. Either spelling classifies as `FEEDER` under the recommended rule because both contain "document feeder" |
| A2 | `"Card Duplex"` (Fujitsu) should classify as `FEEDER_DUPLEX` | Ambiguity A | Medium. It is a real Fujitsu source string (verified from the installed man page) but no one here has the hardware. Classifying it `FEEDER_DUPLEX` starts routing it to `multi_scan()`. `multi_scan()` on a single-sheet path yields one page, so the downside is bounded |
| A3 | `epson2` / `epsonds` expose their feeder as `"Automatic Document Feeder"` | SANE Source Strings | Low. The installed man pages confirm `Flatbed` and a separate `--adf-mode simplex\|duplex`, but the exact feeder string is option-dependent and was not enumerable without hardware. Any name containing "feeder" classifies correctly |
| A4 | htmx 2 does not swap non-2xx bodies, so a 500 from the status poll freezes the UI silently | Pitfall 6, Jinja2 Integration | Low. Recorded as an established fact in `.planning/ROADMAP.md` § Phase 26; the 500 itself was reproduced here, the htmx half was not re-tested in a browser |
| A5 | `typing.cast` is the suppression-free way to write the D-10 negative test | Code Examples | Low. `cast` is unchecked by design in both tools, but the planner should confirm it passes `ty` and `pyrefly` in situ before committing rather than trusting this |
| A6 | Redundant `update_state(SCANNING)` from the pipeline's `SCANNING` event is harmless | Pitfall 7 | Low. `update_state` is an idempotent UPDATE with `error=None`; the job is freshly created so `error` is already `None`. Becomes one extra lock acquisition after Phase 22 |
| A7 | The `saneless jobs` CLI table should keep printing raw state values | Duplication Inventory | Medium. This is an interpretation of success criterion 1 against the "strings users see today must not change" constraint. If the verifier reads criterion 1 as requiring the `jobs` table to use `state_label()`, the plan is one line short. **Worth confirming with the user or flagging explicitly in the plan.** |

---

## Open Questions

1. **Does `saneless jobs` (`cli.py:262`) need to render `state_label()`?**
   - What we know: it prints `j.state.value` raw today. Criterion 1 says the CLI must "read them from the same module". The CLI's *progress prose* will. The discretion constraint says user-visible strings must not change, and `cli.py:238`'s JSON output is a machine contract that must stay raw.
   - What's unclear: whether "the CLI" in criterion 1 means the scan progress output (satisfied) or also the `jobs` table (would be a visible change).
   - **Recommendation:** leave `:238` and `:262` raw and state that decision, with its reason, explicitly in the plan so the verifier reads it as a decision rather than an omission. Phase 23's criterion 2 ("`saneless jobs` renders `FALLBACK` distinctly") is already satisfied by raw values.

2. **Should `ScanResult` carry `poll_task`'s untyped dict?**
   - What we know: neither caller reads `run_pipeline`'s return value today (`worker.py:250-256`, `cli.py:132-137` both discard it). `poll_task` keeps its `-> dict[str, object]` until Phase 23.
   - What's unclear: whether Phase 23 will want the raw task dict threaded through or will replace it wholesale.
   - **Recommendation:** drop it. Carrying an untyped dict inside a typed result is the exact smell N-38 names. Phase 23 adds whatever `poll_task`'s typed replacement returns.

3. **Should `PipelineEvent.DONE` still fire `_transition_event.set()` under the new `_status_cb`?**
   - What we know: it does not today (no branch matches). Under the recommended guard it would, and `Event.set()` on an already-set event is a no-op.
   - What's unclear: whether any future consumer of `wait_transition` would care. `routes.py:297` is the only caller and it uses a 2 s timeout after a flip-continue.
   - **Recommendation:** accept the no-op set for simplicity, but note it in the commit message so it is not mistaken for an accident. If the planner prefers zero delta, `return` without the set for terminal states — but `SCANNING_REVERSE` must keep its set (`worker.py:232-233`).

4. **`frozen=True` on the two new result dataclasses?**
   - What we know: no existing dataclass in this codebase is frozen. Results are conceptually immutable snapshots.
   - **Recommendation:** planner's call. A mild preference for `frozen=True` on `ScanResult`/`UploadResult` only, since nothing mutates them and Phase 23 will pass them across a thread boundary.

5. **Does the "Automatic Document Feeder" test need the real SANE `test` backend, or is a fake enough?**
   - What we know: the real `test` backend is installed on this machine (`scanimage -d test:0 --help` confirms `Flatbed|Automatic Document Feeder`), but **SCNR-08 explicitly assigns the opt-in real-backend integration test to Phase 24.**
   - **Recommendation:** use a fake in Phase 21, modelled on `tests/test_scanner.py:503-540`. Do not build the `SANE_CONFIG_DIR` harness here — that is Phase 24's, and building it now would half-satisfy SCNR-08 in the wrong phase, exactly the discipline problem D-06 guards against.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python | everything | ✓ | 3.14.2 | — |
| uv | all commands | ✓ | (project-managed) | — |
| ruff | lint gate | ✓ | 0.15.7 | — |
| ty | type gate | ✓ | 0.0.80 | — |
| pyrefly | type gate | ✓ | 1.2.0 | — |
| pytest | test gate | ✓ | 9.0.2 | — |
| pytest-timeout | hang guard | ✓ | 2.4.0 | — |
| Jinja2 | template filters | ✓ | 3.1.6 | — |
| FastAPI / Starlette | render tests | ✓ | 0.135.1 / 0.52.1 | — |
| **SANE `scanimage` + `test` backend** | *(nice-to-have)* verifying real source strings | ✓ | installed at `/usr/bin/scanimage` | Fake device (what this phase should use anyway — see Open Question 5) |
| **SANE backend man pages** | source-string harvesting | ✓ | installed | Web man pages |
| Chromium / Playwright | `-m browser` suite | ⚠️ unverified | — | The 8 browser tests are deselected by default. If Chromium is missing, `uv run pytest -m browser` errors on collection — **the planner should verify this before writing a task that depends on it** |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** none confirmed. The only unverified item is the Playwright/Chromium install; CLAUDE.md forbids marking browser checks manual-only, so if Chromium is absent the planner needs an install task, not a waiver.

---

## Sources

### Primary (HIGH confidence) — executed in this repository on 2026-09-10

- **Type-checker pattern matrix** — probe module written to `src/saneless/_probe.py`, checked with `uv run ty check`, `uv run pyrefly check`, `uv run ruff check`, then deleted; tree verified clean via `git status --porcelain` (0 lines). Ten pattern variants, each in complete and deliberately-incomplete form.
- **`PLR0911` finding** — `uv run ruff check` on a 7-arm / 7-return `match`: `PLR0911 Too many return statements (7 > 6)`.
- **`upload_document` blast radius** — `tests/conftest.py` and `src/saneless/pipeline.py` temporarily modified, `uv run pytest -q -m "not browser"` run twice, both files restored from backups, tree verified clean. Result: `18 failed, 314 passed, 8 deselected`.
- **Baseline** — `uv run pytest -q -m "not browser"` → `332 passed, 8 deselected in 26.83s`.
- **`assert_never` runtime semantics** — `inspect.getsource(typing.assert_never)`; raises `AssertionError`, not an `assert` statement.
- **`StrEnum` match-at-runtime** — a member pattern matches the equivalent raw `str`.
- **Jinja2 filter-raise behaviour** — `jinja2.Environment` direct render, plus `Jinja2Templates` + `fastapi.testclient.TestClient` with and without `raise_server_exceptions`: `AssertionError` propagates; HTTP 500.
- **SANE `test` backend** — `scanimage -d test:0 --help` → `--source Flatbed|Automatic Document Feeder [Flatbed]`.
- **SANE source-string harvest** — `man -k '^sane-'` across all installed backend man pages, quoted-value extraction (`sane-test`, `sane-canon_dr`, `sane-epjitsu`, `sane-fujitsu`, `sane-kodak`, `sane-sharp`, `sane-bh`, `sane-epson*`).
- **Tool versions** — `importlib.metadata.version` for all nine.
- **Codebase reads** — `job.py`, `pipeline.py`, `paperless.py`, `worker.py`, `cli.py`, `web/app.py`, `web/routes.py`, all three templates, `scanner/base.py`, `scanner/sane_backend.py`, `auto_profiles.py`, `tests/conftest.py`, `tests/test_pipeline.py`, `tests/test_worker.py`, `tests/test_web.py`, `tests/test_cli.py`, `tests/test_paperless.py`, `tests/test_auto_profiles.py`, `tests/test_scanner.py`, `pyproject.toml`, `CONTRIBUTING.md`.

### Primary (HIGH confidence) — project artefacts

- `.planning/phases/21-vocabulary-and-contracts/21-CONTEXT.md` — D-01..D-13, the duplication inventory, the sentinel inventory, the ordering constraint
- `.planning/REQUIREMENTS.md` § "Contracts and Vocabulary" — CTR-01..CTR-05 verbatim
- `.planning/ROADMAP.md` § Phases 21, 22, 23, 24, 25, 26 — boundaries and the htmx `responseHandling` note
- `.planning/reviews/2026-09-09-code-review.md` § C-06 — the confirmed real-world feeder names and the ten-page/one-page experiment
- `CLAUDE.md`, `CONTRIBUTING.md` — the five gates and the no-suppression rule
- `.planning/config.json` — `nyquist_validation: true`, `tdd_mode: true`, `code_review_depth: deep`

### Secondary (MEDIUM confidence) — official documentation

- Jinja2 API — filter/test/global registration and the "no templates loaded yet" caveat: https://jinja.palletsprojects.com/en/stable/api (via Context7 `/websites/jinja_palletsprojects_en_stable`)
- SANE Standard 2.0 draft §4.5.7 "Scan Source Options" — well-known values `Flatbed`, `Transparancy Adapter`, `Automatic Document Feeder`: https://sane-project.gitlab.io/standard/draft-2/api.html
- `sane-hp5590(5)` — `Flatbed`, `ADF`, `ADF Duplex`, `TMA Slides`, `TMA Negatives`: http://www.sane-project.org/man/sane-hp5590.5.html
- `sane-canon_dr(5)` — `Flatbed|ADF Front|ADF Back|ADF Duplex`: https://manpages.ubuntu.com/manpages/jammy/man5/sane-canon_dr.5.html
- `scanadf(1)` — `sane-umax` requires `--source="Automatic Document Feeder"`: https://manpages.debian.org/testing/sane/scanadf.1.en.html
- `sane-pixma(5)` — Canon feeder naming: https://www.mankier.com/5/sane-pixma
- `sane-epjitsu(5)` — `Flatbed`, `ADF Front`, `ADF Back`, `ADF Duplex`: https://linux.die.net/man/5/sane-epjitsu

### Tertiary (LOW confidence)

- GitLab issue sane-project/backends#411 (ScanSnap iX500 `--source ADF Front|ADF Back|ADF Duplex`) — corroborates the Fujitsu naming already confirmed from the installed man page; used only as a cross-check, not as a primary source.

---

## Metadata

**Confidence breakdown:**

| Area | Level | Reason |
|------|-------|--------|
| Exhaustiveness pattern (ty/pyrefly/ruff) | **HIGH** | Ten pattern variants executed against this repo's exact tool versions, in both complete and incomplete form. The `PLR0911` blocker and the `ty`/`pyrefly` disagreement are reproduced, not inferred. |
| Regression / atomicity analysis | **HIGH** | The 18-test blast radius was measured by running the suite, not estimated. Every other affected test was located by grep and read. |
| Duplication & sentinel inventories | **HIGH** | Every line number re-verified against the current tree; two of CONTEXT.md's own claims independently re-confirmed (`app.js` has no state strings; `routes.py` has no `JobState`). |
| Jinja2 integration | **HIGH** | Filter-raise behaviour reproduced end to end through `Jinja2Templates` + `TestClient`; registration semantics from official docs via Context7. |
| Typed-result design | **HIGH** | Rationale grounded in the codebase's own five existing dataclasses and in the concrete extension path STOR-03 defines. |
| SANE source strings | **MEDIUM-HIGH** | Nine backends harvested from man pages installed on this machine plus the live `test` backend. Vendor-specific feeder spellings for Brother and Epson are cited from the project's own review and from web man pages, not verified against hardware — see A1, A2, A3. |
| Ordering / commit sequence | **MEDIUM-HIGH** | Each dependency edge is grounded in a measured or read fact; the exact commit granularity remains the planner's discretionary call per CONTEXT.md. |
| Security domain | **HIGH** | Assessed as no-change with a specific, checkable rationale per ASVS category. |

**Research date:** 2026-09-10
**Valid until:** 2026-10-10 (30 days). The one fast-moving input is `ty` (0.0.80, pre-1.0) — its narrowing behaviour could change between releases. If `ty` or `pyrefly` is bumped before this phase executes, **re-run the pattern matrix probe** before trusting the table.
