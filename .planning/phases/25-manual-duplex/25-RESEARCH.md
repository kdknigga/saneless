# Phase 25: Manual Duplex - Research

**Researched:** 2026-09-13
**Domain:** Python 3.14 application internals — pydantic-settings validation, Click CLI interactivity, `threading` coordination, SANE source resolution, StrEnum vocabulary evolution
**Confidence:** HIGH (every load-bearing claim below was executed against this tree or read from vendor documentation; the exceptions are tagged `[ASSUMED]` and listed in the Assumptions Log)

---

## Summary

This phase is **not a research problem — it is a sequencing problem.** There is no external
library to choose, no new dependency to install, and no unfamiliar API. Every mechanism the
eighteen locked decisions require already exists in this tree or in a pinned dependency, and I
verified each one by execution rather than recall. The genuinely hard questions are (a) which
existing tests break and in what order, (b) three concrete constraints the CONTEXT does not name
that will stop a plan dead if the planner does not design around them, and (c) how to observe a
ten-minute human timeout in a suite with a sixty-second per-test SIGALRM.

**Three measured findings change the plan shape, and all three are new:**

1. **`_scan_manual_duplex` sits exactly on ruff's `PLR0913` limit (5 args; 6 errors — executed).**
   D-09 requires the flip timeout to reach it, and adding a parameter is a lint failure the
   project forbids suppressing. The fix has an in-tree precedent and costs nothing: drop `notify`
   and derive it from `request`, exactly as `_handle_duplex_mismatch` already does.
2. **`CliRunner` reports `sys.stdin.isatty() is False` (executed).** D-11 requires the CLI to
   refuse manual duplex off a terminal *and* requires a `CliRunner(input="y\n")` test proving the
   prompt appears. Written naively, those two requirements are **mutually untestable** — the
   refusal fires in the very test that is supposed to reach the prompt. A one-line indirection
   seam in `cli.py` resolves it; without it, one of DPLX-04's two halves cannot be tested.
3. **`tests/test_web_state_rendering.py` needs no edits at all.** Its six `list(JobState)`
   parametrisations build their expectations from `state_label` / `progress_label` / `ACTIVE_STATES`,
   so `SCANNING_REVERSE` flows through automatically — and `test_status_area_prose` then *proves
   D-16 for free*, because it already asserts `("flip-prompt" in text) is (state is AWAITING_FLIP)`.
   The CONTEXT's sizing ("4 parametrised `list(JobState)` sites") undercounts the sites (there are
   10) but overstates the work (only 4 hand-written lists actually need touching).

The ripple is also smaller than feared in a second place: `PipelineEvent` gains **no** member, so
`tests/test_pipeline.py:987`'s `len(PipelineEvent) == 6` pin does **not** break. What breaks is
`test_scanning_reverse_has_no_job_state`, which asserts the very defect DPLX-06 removes.

**Primary recommendation:** Sequence this as seven waves in strict dependency order — config →
vocabulary → coordinator → scanner → CLI → auto-profiles → docs — and land each wave's production
change together with the tests that pin it. No git hook in this repo runs pytest, so "every commit
leaves the suite green" is a discipline the plan must enforce by task boundaries, not a gate that
will catch a mistake.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

All eighteen decisions in `25-CONTEXT.md` § `<decisions>` are **locked for planning**. Reproduced
in summary; the CONTEXT file is authoritative for their full reasoning.

- **D-01:** `ProfileConfig` gains `duplex: Literal["none", "hardware", "manual"] = "none"`, and
  `source` becomes a pure SANE value. `source` is passed to the device verbatim and is never
  inspected to decide a scanning strategy anywhere in the codebase. `pipeline._is_manual_duplex`
  is deleted; `worker.py:194`'s copy of the rule becomes `profile.duplex == "manual"` — or
  disappears entirely, since D-09 moves the coordinator decision to one place.
- **D-02:** A legacy `source = "Manual Duplex"` translates to `duplex = "manual"` at config load,
  and the device source is resolved at scan time from the device's own source list. The backend
  picks the first source classifying `FEEDER` or `FEEDER_DUPLEX` via Phase 24's `classify_source`.
  **Do not hardcode `"ADF"`.** The backend is the right place because `scan_pages` already holds
  `raw_options`.
- **D-03:** The translation lives in `ProfileConfig` (a `model_validator(mode="before")`, so every
  construction path is covered); the named warning lives in `Settings` (the existing
  `field_validator("profiles")`, which can name the profile). Validators running after source
  merging is pydantic-settings' own contract — **do not spend a test proving it.**
- **D-04:** The deprecation warning is a `logger.warning`, not `warnings.warn`, and not pydantic's
  `Field(deprecated=...)`. `filterwarnings = ["error"]` is live and there is zero
  `warnings.warn` precedent.
- **D-05:** All three `duplex` values ship, and `"hardware"` is declarative. **Nothing reads
  `"hardware"`, and the code must say so in a comment.** Phase 30's APPL-05 is its eventual reader.
- **D-06:** `auto-profiles` emits `duplex = "hardware"` for `FEEDER_DUPLEX` sources and omits the
  key everywhere else (Phase 16's "write only when non-default" precedent). `auto-profiles` can
  never emit `"manual"`.
- **D-07:** The `isinstance` dispatch at `pipeline.py:871` becomes a total `match` with
  `assert_never`. **Add a comment recording that this dispatch satisfies N-07.**
- **D-08:** The duplex-mismatch branch keeps bypassing `_drop_empty_pages`, and this is documented
  as deliberate — in code **and** in the ADF how-to. `_drop_empty_pages` raises when every page is
  empty, so filtering an all-blank backs pass would destroy the fronts.
- **D-09:** `FlipCoordinator` is an **ABC** (not `typing.Protocol`) with one abstract method
  returning a total enum: `wait_for_flip(timeout: float) -> FlipOutcome`, where `FlipOutcome` is
  `CONTINUED | ABORTED | TIMED_OUT`, consumed with `match` + `assert_never`. It replaces **both**
  `PipelineRequest.flip_event` and `abort_event`. One atomic answer is the point.
- **D-10:** `OutputConfig.flip_timeout_seconds: int = 600`. Deliberately diverges from the
  module-constant precedent because this is the only timeout that waits on a **human**.
- **D-11:** The CLI uses `click.confirm`, plus an up-front refusal when stdin is not a TTY, exiting
  **2**. **`click.pause` is disqualified, not merely dispreferred** — it is a documented no-op off
  a terminal.
- **D-12:** `JobState.SCANNING_REVERSE` is added as the ninth member. **Verified: no migration is
  needed** — `state TEXT NOT NULL` with no `CHECK` constraint. **A planner must not invent a
  migration task.** `BUSY_STATES` is derived, so busy-ness follows automatically.
- **D-13:** `state_label` returns `"Scanning backs"`; `progress_label` returns
  `"Scanning reverse sides..."` — byte-identical to what `cli.py:131` already prints.
- **D-14:** The **entire** `_transition_event` protocol is deleted: `wait_transition`, the
  attribute, all five `set()` calls, both `clear()` calls, the call at `routes.py:297`, and the
  tests at `tests/test_worker.py:729`, `:758`, `:805` plus the usage at `:1400`. **Do not build a
  `wait_for_state` polling helper here** — ROBU-03 (Phase 26) owns it.
- **D-15:** An aborted flip raises `ScanError` with a clear message; the cancelled vocabulary
  (`ScanCancelledError`, a `CANCELLED` state, not logging at ERROR) is **Phase 28's**.
- **D-16:** The coordinator answers once, and its answer is final. A late Abort is dropped; the
  route returns current status. The control disappears on its own because `status.html` renders
  `partials/flip.html` only in the `AWAITING_FLIP` branch.
- **D-17:** All three status routes share one "current job, else most recent" lookup, extracted
  from `current_job_status` (`routes.py:186-192`).
- **D-18:** The legacy `source = "Manual Duplex"` form is removed from the documentation entirely.
  **Derived constraint, binding on D-04's message:** the warning text is the only migration
  instruction an operator will ever receive, so it **must state the replacement inline**.

### Claude's Discretion

> Genuinely open to the planner. Make the call and record the reasoning in the plan.

- **What `ScanSettings` carries to trigger feeder resolution** (D-02). Options weighed: a
  `DuplexMode` enum defined in `scanner/base.py` alongside `SourceKind`; an empty-string sentinel;
  a `prefer_feeder` boolean. Constraints: Phase 21's D-03 forbids the scanner package importing job
  vocabulary, so any enum lives in `scanner/`, not `vocabulary.py`; and if `duplex` ends up spelled
  both as a `ProfileConfig` Literal and a scanner-side enum there must be exactly one conversion
  point.
- **Where the "manual duplex without a coordinator" refusal fires** (DPLX-04). Hard constraint: it
  must precede any SANE contact, and `_resolve_device` calls `get_devices()` when `scanner.device`
  is empty. **Placing it inside `_scan_manual_duplex` is a trap** — that function scans pass A
  first, so refusing there burns a full feeder pass before failing.
- **What happens when a `duplex = "manual"` profile meets a device with no feeder source.** Hard
  constraint: the `"Auto"` substitution inside `_resolve_source` **must not be reachable for manual
  duplex** — that path is precisely C-01's mechanism. Raising before pass A in the existing "Device
  does not support ... Available: [...]" message shape is the expected shape.
- **Where `FlipOutcome` lives** — `vocabulary.py` is the leaf module every consumer already imports
  from, and it may not import `pipeline`.
- **Which doc sentences get rewritten**, beyond those named in `<canonical_refs>`.

### Deferred Ideas (OUT OF SCOPE)

- **`ScanCancelledError`, a `CANCELLED` job state, and not logging an abort at ERROR level** —
  Phase 28 (EXC-04, N-08). D-15 stops at a clear message deliberately.
- **Aborting during pass B rather than at the prompt** — Phase 29 (HARD-02). Requires lazy page
  consumption. D-16 removes the misleading control instead.
- **A `wait_for_state` polling helper for worker tests** — Phase 26 (ROBU-03).
- **Owner-only flip prompt, Abort confirmation, and front/back page counts during pass B** —
  Phase 30 (APPL-09, APPL-03). D-05's declarative `"hardware"` value is groundwork for APPL-05.
- **`extra="forbid"` covering the new `duplex` and `flip_timeout_seconds` keys, and `--force` merge
  semantics for `duplex`** — Phase 27 (CFG-01, CFG-07).
- **Converting the flip routes to plain `def` handlers and adding 429 backpressure** — Phase 26
  (ROBU-02, ROBU-05), even though D-17 edits the same three routes.
- **Filtering blank pages on the duplex-mismatch path** — declined outright by D-08, not deferred.
- **Hardcoding `"ADF"` as the translated device source** — declined outright by D-02.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| **DPLX-01** | `ProfileConfig` has a `duplex` field (`none`, `hardware`, `manual`); `source` is passed to SANE verbatim and is never overloaded to mean a scanning strategy [C-01, doc row 2] | Finding 1 (pydantic field + measured before-validator behaviour); Finding 9 (the `grep` gate that proves "never overloaded"); Don't Hand-Roll row 1 |
| **DPLX-02** | A legacy config with `source = "Manual Duplex"` still loads, is translated to `duplex = "manual"` at config load, and logs a deprecation warning [C-01] | Finding 1 (executed: translation fires through direct construction, nested dict, and pydantic-settings merge); Finding 2 (the two-validator split and the `caplog` precedent); Pitfall 3 (the overwrite-vs-absent decision) |
| **DPLX-03** | The manual-duplex decision is read in exactly one place; the duplicated detection rule in the worker and the `isinstance` dispatch on the two-outcome result are gone [N-07] | Finding 9 (grep gate + the one surviving home for the substring rule); Pattern 3 (`match`/`assert_never` dispatch, measured in Phase 21 D-08) |
| **DPLX-04** | A `FlipCoordinator` protocol with a timeout is the only way the pipeline waits for a flip; the CLI provides a stdin prompt, the web provides the HTMX Continue button, and running manual duplex without a coordinator is refused up front [C-02, M-07, doc row 1] | Finding 3 (**PLR0913 blocker + its fix**); Finding 4 (**CliRunner isatty blocker + its fix**); Finding 5 (coordinator module placement); Pattern 1 (the single-answer coordinator shape); Pitfall 1 (~10 existing tests break on the refusal guard) |
| **DPLX-05** | A flip wait that exceeds the timeout fails the job with a clear message and releases the scanner [M-07] | Finding 6 (`Event.wait(0)` measured at 4 µs — the zero-cost timeout seam, with the `_TIMEOUT_BUDGET = 0` precedent); Validation Architecture C3; Open Question 1 (the CLI cannot honour the timeout) |
| **DPLX-06** | During pass B the job is in a visible `SCANNING_REVERSE` state, Abort at the flip prompt cancels the job, and `wait_transition` is deleted [M-02, doc row 22] | Finding 7 (the **real** JobState ripple — 4 edits, not 10); Finding 8 (`PipelineEvent.job_state` narrows to non-Optional, collapsing two `state is None` branches); Finding 11 (D-14 vs ROBU-03 is not a conflict — the helper already exists); Finding 12 (`index` is a **fourth** copy of D-17's lookup) |
| **DPLX-07** | Auto-profiles always writes a `default` profile, for flatbed-only, feeder-only, and mixed devices, proven by a write-then-load round-trip test [C-08] | Finding 10 (**confirmed already satisfied**; exactly one of three round-trips exists at `tests/test_auto_profiles.py:548`); Code Example 6 |
</phase_requirements>

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Declaring the duplex strategy (`duplex` field) | Config (`config.py`) | — | It is operator intent, expressed in TOML, validated by pydantic. D-01. |
| Translating the legacy `source` marker | Config (`ProfileConfig` before-validator) | — | D-03: must cover TOML, env var and direct construction, so it belongs on the model, not on a loader. |
| Naming the profile in the deprecation warning | Config (`Settings` field validator) | — | `ProfileConfig` does not know its own key. D-03 splits the roles for exactly this reason. |
| Choosing the scanning strategy (one pass vs two) | Pipeline (`run_pipeline`) | — | Orchestration decides *what sequence of acquisitions to run*. DPLX-03: exactly one reader. |
| Resolving a device source name | Scanner backend (`sane_backend._resolve_source`) | — | It is the only tier holding `raw_options`; D-02 measured that resolving elsewhere costs a whole extra `sane.open`/`close`. |
| Refusing manual duplex on a feeder-less device | Scanner backend | Pipeline (surfaces the `ScanError`) | The device's source list is the evidence, and only the backend has it. |
| Defining the flip contract (`FlipCoordinator`) | Pipeline (`pipeline.py`) | — | The ABC sits beside `PipelineRequest`, the record that carries it. Both CLI and worker already import `pipeline`. |
| The flip vocabulary (`FlipOutcome`) | Vocabulary (`vocabulary.py`) | — | A closed set of words the system uses about itself, consumed by `match` + `assert_never` — the `ScanOutcome` precedent exactly. Leaf module; imports nothing. |
| Producing a flip answer — interactive | CLI (`cli.py`) | — | `run_pipeline` is synchronous and the coordinator runs on the calling thread, which is what makes prompting from inside it correct. |
| Producing a flip answer — web | Worker (`worker.py`) | Web routes (trigger only) | The worker owns the thread that is waiting; the routes only signal it. |
| Holding the timeout **value** | Config (`OutputConfig`) | — | D-10. It sits with `paperless_task_timeout` and `paperless_cache_ttl_seconds`. |
| **Enforcing** the timeout | Pipeline (`_scan_manual_duplex`) | — | D-09 fixes the signature as `wait_for_flip(timeout)`, so the caller passes it in. |
| Pass-B visibility (the ninth state) | Vocabulary (`JobState`) | Worker (persists), Web templates (render) | One enum, one label map — Phase 21's whole point. Templates own no vocabulary of their own. |
| "Current job, else most recent" lookup | Web routes | — | D-17. Note it is duplicated in **four** places today, not three (Finding 12). |
| Emitting `duplex` into generated TOML | auto-profiles (`auto_profiles.py`) | — | D-06, following the `auto_source_mode` write-when-non-default precedent. |
| Teaching the new form | Docs | — | Phase rule: correct what you changed. D-18 makes this load-bearing, not cosmetic. |

**Two tier-assignment traps this map exists to prevent:**

1. **Feeder resolution does not belong in the pipeline.** It reads as an orchestration concern
   ("pick the right input"), but the pipeline has no capabilities in scope — `run_pipeline` never
   calls `get_capabilities`, and `_resolve_device` only calls `get_devices()` when
   `settings.scanner.device` is empty (verified). Putting it there costs a full extra device
   open/close before every pass A.
2. **The coordinator refusal does not belong in the scanner tier or in `_scan_manual_duplex`.** It
   is a *request validity* question, answerable with zero device contact, and the CONTEXT already
   flags `_scan_manual_duplex` as a trap (pass A runs first). It belongs at the top of
   `run_pipeline`, before `_resolve_device`.

---

## Standard Stack

**This phase installs nothing.** Every mechanism is already a pinned dependency or a standard-library
module. Versions below were read from the live environment with `uv run python -c ...`.

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| pydantic | 2.12.5 | `model_validator(mode="before")` for the legacy translation; `Literal` for the `duplex` field | Already the config layer. `mode="before"` is the documented hook for reshaping raw input before instantiation. [VERIFIED: `uv run python -c "import pydantic; print(pydantic.VERSION)"`] [CITED: pydantic docs, concepts/validators.md via Context7] |
| pydantic-settings | 2.13.1 | Merges TOML + env + init into one dict, then validates | D-03 depends on this ordering; the CONTEXT correctly classifies it as the library's own contract and forbids a test for it |
| click | 8.3.1 | `click.confirm` for the flip prompt; `CliRunner` for testing it | C-02's prescribed API. `confirm()` raises `Abort` on interrupt. [VERIFIED: executed, see Finding 4] [CITED: click.palletsprojects.com/en/stable/api] |
| `threading` (stdlib) | 3.14 | `Event` + `Lock` inside the web `FlipCoordinator` | Already the mechanism; D-09 restructures it rather than replacing it |
| `enum.StrEnum` (stdlib) | 3.14 | `FlipOutcome`, `JobState.SCANNING_REVERSE` | The project's uniform vocabulary type |
| `typing.assert_never` (stdlib) | 3.14 | Totality on both new enums | Phase 21 D-08 **measured** that only `match` + `assert_never` is enforced by both `ty` and `pyrefly` |

### Supporting (test-side, already installed)

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest | 9.0.2 | Test runner; `importlib` import mode | `from tests.fake_sane import ...` — the bare form raises `ModuleNotFoundError` |
| pytest-timeout | 2.4.0 | `timeout = 60`, `timeout_method = "signal"` | The hang guard. SIGALRM is delivered to the **main** thread, so a worker-thread hang reports the wrong traceback — keep per-test waits far below it |
| `caplog` (pytest builtin) | — | Asserting D-04's deprecation warning | Precedent: `tests/test_auto_profiles.py:617`, `caplog.at_level(logging.WARNING, logger="saneless.auto_profiles")` |
| `unittest.mock.MagicMock(spec=ScannerBackend)` | stdlib | Pass-count control via `side_effect` | Precedent: `tests/test_outcomes_e2e.py:_build_scanner` |
| `tests/fake_sane.FakeSaneDev` | in-tree | Driving the **real** `SaneBackend` against a truthful fake | The only seam that can prove C-01 is actually fixed |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `logger.warning` (D-04) | `warnings.warn(..., DeprecationWarning)` | **Locked against.** `filterwarnings = ["error"]` is live and `src/`+`tests/` contain zero `warnings.warn` occurrences, so every legacy-config test would become an error |
| `logger.warning` (D-04) | `Field(deprecated=...)` | **Locked against.** Warns on field *access*, not at load — wrong trigger for DPLX-02, and under the live filter every access becomes a test error |
| `click.confirm` (D-11) | `click.pause` | **Disqualified, and I re-verified it by execution:** off a terminal `pause()` printed nothing and returned immediately (Finding 4). It is the only API matching "press Enter" literally, and using it would reintroduce C-02 invisibly |
| ABC (D-09) | `typing.Protocol` | **Locked against**, and the rule is observable: `Protocol` describes shapes this project does not own (`SaneDevice`, `_SettingsFactory`); `ABC` defines seams it implements (`ScannerBackend`, whose CLI stubs Phase 24's WR-08 moved to subclassing) |
| `match` + `assert_never` | `dict[FlipOutcome, ...]` | A dict missing a member draws no diagnostic from either checker; the same enum in a `match` is caught by both, at edit time (Phase 21 D-08, measured) |

**Installation:** none. `uv sync` already provides everything.

---

## Package Legitimacy Audit

**This phase installs no external packages.** There is nothing to slopcheck.

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| *(none)* | — | — | — | — | — | N/A — no new dependency |

**Packages removed due to slopcheck `[SLOP]` verdict:** none — the gate was not applicable.
**Packages flagged as suspicious `[SUS]`:** none.

Every library named in Standard Stack is already present in `pyproject.toml` and resolved in
`uv.lock`. Versions were confirmed by importing them from the project's own environment, not by a
registry query, which is the stronger check for an already-installed dependency.

---

## Architecture Patterns

### System Architecture Diagram

Data flow for one manual-duplex job, after this phase. Arrows are data/control flow; boxes are
conceptual components, not files.

```
   TOML / env / direct construction
                │
                ▼
   ┌────────────────────────────┐
   │ ProfileConfig              │   before-validator: legacy `source` marker ⇒ duplex="manual"
   │  .source (pure SANE value) │   (source itself is NOT rewritten — D-01)
   │  .duplex  none|hw|manual   │
   └──────────────┬─────────────┘
                  │
                  ▼
   ┌────────────────────────────┐
   │ Settings.profiles validator│ ──▶ logger.warning naming the profile + the replacement (D-04/D-18)
   └──────────────┬─────────────┘
                  │  settings
    ┌─────────────┴──────────────────────────────────────────────┐
    │                                                            │
 CLI entry                                                   Web entry
 `saneless scan`                                             POST /api/scan
    │                                                            │
    │ duplex=="manual" and not interactive ⇒ EXIT 2 (D-11)        ▼
    │                                                       ScanWorker._process_job
    │ builds ClickFlipCoordinator                                 │ builds WorkerFlipCoordinator
    └──────────────┬─────────────────────────────────────────────┘
                   │  PipelineRequest(.flip_coordinator)
                   ▼
    ╔══════════════════════════════════════════════════════╗
    ║ run_pipeline                                         ║
    ║                                                      ║
    ║  ① profile lookup                                    ║
    ║  ② ── GUARD ─────────────────────────────────────    ║
    ║      duplex=="manual" and coordinator is None        ║
    ║          ⇒ raise ConfigError   ◀── BEFORE any SANE   ║
    ║  ③ _resolve_device  (may call get_devices())         ║
    ║  ④ ScanSettings(source=…, resolve_feeder_source=…)   ║
    ║  ⑤ strategy: profile.duplex == "manual" ?            ║
    ╚═══════╤══════════════════════════════════╤═══════════╝
            │ no                               │ yes
            ▼                                  ▼
     _scan_simplex                      _scan_manual_duplex
            │                                  │
            │                    ┌─────────────┴──────────────┐
            │                    │ PASS A: scanner.scan_pages │──┐
            │                    └─────────────┬──────────────┘  │
            │                     notify(AWAITING_FLIP)          │
            │                                  │                 │
            │                    coordinator.wait_for_flip(t) ◀───┼── Continue / Abort
            │                                  │                 │   (web: HTMX POST)
            │                     ┌────────────┴────────────┐    │   (CLI: click.confirm)
            │                     │ match FlipOutcome:      │    │
            │                     │  CONTINUED → carry on   │    │
            │                     │  ABORTED   → ScanError  │    │
            │                     │  TIMED_OUT → ScanError  │    │
            │                     │  _ → assert_never       │    │
            │                     └────────────┬────────────┘    │
            │                     notify(SCANNING_REVERSE) ──────┼──▶ JobState.SCANNING_REVERSE
            │                                  │                 │    (busy ⇒ flip prompt gone)
            │                    ┌─────────────┴──────────────┐  │
            │                    │ PASS B: scanner.scan_pages │◀─┘  device re-opened & re-closed
            │                    └─────────────┬──────────────┘     (scanner already released
            │                                  │                     between passes — verified)
            │                     ┌────────────┴────────────┐
            │                     │ counts equal?           │
            │                     │  yes → ScanBatch        │
            │                     │  no  → _DuplexMismatch  │
            │                     └────────────┬────────────┘
            │                                  │
            └──────────────┬───────────────────┘
                           ▼
            match result:  ScanBatch() | _DuplexMismatch() | _ → assert_never   (D-07)
                           │                       │
                           │                       └─▶ two partial PDFs, NO blank filtering (D-08)
                           ▼
              _drop_empty_pages → assemble_pdf → _preserving[ upload → poll ] → ScanResult
```

Two boundaries the diagram makes visible and the planner must preserve:

- **The guard at ② is upstream of every SANE call**, including `get_devices()` at ③. That ordering
  is the whole content of the second discretion item.
- **The device is opened and closed inside each `scan_pages` call**, so "releases the scanner"
  (criterion 3) is already true of the handle. The thing a timeout releases is the single worker
  thread, which is M-07's actual complaint.

### Recommended Project Structure

No new files. Changes land in existing modules:

```
src/saneless/
├── config.py           # + duplex field, + before-validator, + legacy predicate,
│                       #   + deprecation field_validator, + flip_timeout_seconds
├── vocabulary.py       # + JobState.SCANNING_REVERSE, + ACTIVE_STATES member,
│                       #   + two label arms, + FlipOutcome
├── pipeline.py         # + FlipCoordinator ABC, ~ PipelineRequest, ~ _scan_manual_duplex,
│                       #   ~ run_pipeline guard + dispatch, ~ PipelineEvent.job_state,
│                       #   - _is_manual_duplex
├── worker.py           # + WorkerFlipCoordinator, - _transition_event (entirely),
│                       #   - the duplicated rule, ~ _status_cb
├── cli.py              # + _stdin_is_interactive seam, + ClickFlipCoordinator,
│                       #   + the exit-2 refusal, ~ status_callback
├── auto_profiles.py    # ~ generate_profiles (duplex=hardware), ~ write_profiles_to_config
├── scanner/
│   ├── base.py         # ~ ScanSettings (the feeder-resolution signal)
│   └── sane_backend.py # ~ _resolve_source (feeder selection + no-feeder refusal)
└── web/
    ├── routes.py       # + shared job lookup, ~ continue_flip, ~ abort_flip, ~ index
    └── templates/      # status.html needs NO change (Finding 7)
```

### Pattern 1: The single-answer coordinator

**What:** One `Event`, one outcome slot, one `Lock`. The outcome is written *before* the event is
set, and only if nothing has been written yet.

**When to use:** This is D-09's "one atomic answer is the point" and D-16's "the coordinator answers
once, and its answer is final", implemented. It replaces today's wake-then-ask-why two-step at
`worker.py:104-109`, which is where M-02's dead Abort lives.

```python
# Source: derived from worker.py:97-109 (the defect) + D-09/D-16 (the contract).
# Shape only -- the planner owns docstrings, which ruff's D rules require.
class WorkerFlipCoordinator(FlipCoordinator):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._outcome: FlipOutcome | None = None

    def _resolve(self, outcome: FlipOutcome) -> bool:
        """Claim the single answer. Returns False if it was already claimed."""
        with self._lock:
            if self._outcome is not None:
                return False
            self._outcome = outcome
        self._event.set()
        return True

    def signal_continue(self) -> None:
        self._resolve(FlipOutcome.CONTINUED)

    def signal_abort(self) -> None:
        self._resolve(FlipOutcome.ABORTED)

    def wait_for_flip(self, timeout: float) -> FlipOutcome:
        if not self._event.wait(timeout):
            # Claim TIMED_OUT under the same lock, then re-read: a Continue
            # that won the race is honoured rather than overwritten.
            self._resolve(FlipOutcome.TIMED_OUT)
        with self._lock:
            answer = self._outcome
        assert answer is not None  # noqa-free: _resolve always leaves one set
        return answer
```

> **Note on the trailing `assert`:** `assert` is banned outside tests only by `S101`, which
> `[tool.ruff.lint.per-file-ignores]` relaxes for `tests/**` — meaning it is **active in `src/`**.
> Do not write a bare `assert` here. Restructure so `_resolve` returns the claimed outcome and
> `wait_for_flip` reads it directly; the type then narrows without an assertion. This is a real
> lint trap, not a style note.

**Consumption** is the `job_state_for` precedent verbatim:

```python
# Source: vocabulary.py:261-268 (job_state_for) -- the established shape.
match coordinator.wait_for_flip(timeout):
    case FlipOutcome.CONTINUED:
        pass
    case FlipOutcome.ABORTED:
        raise ScanError("Manual duplex scan cancelled at the flip prompt")
    case FlipOutcome.TIMED_OUT:
        raise ScanError(
            f"Timed out after {timeout:g}s waiting for the stack to be flipped"
        )
    case _:
        assert_never(outcome)
```

### Pattern 2: Feeder resolution inside `_resolve_source`

**What:** When the caller asked for feeder resolution, the function *selects* from the device's
list instead of *validating* a requested name. The two paths are disjoint, which is what makes the
`Auto` substitution structurally unreachable for manual duplex rather than merely guarded.

**When to use:** D-02, plus the third discretion item.

```python
# Source: sane_backend.py:792-847 (_resolve_source) + scanner/base.py:55-113 (classify_source).
def _resolve_source(
    raw_options: list[tuple],
    requested: str,
    *,
    resolve_feeder: bool = False,
) -> tuple[str, bool]:
    reported = _constraint(raw_options, "source")
    has_source_option = reported.present
    available_sources = [str(s) for s in reported.values or []]

    if resolve_feeder:
        # The requested name still wins WHEN THE DEVICE HAS IT and it feeds --
        # so a documented `source = "ADF"` is honoured verbatim (D-01) and the
        # device's list is consulted only as a fallback (D-02).
        if requested in available_sources and classify_source(requested).uses_feeder:
            return requested, has_source_option
        feeders = [s for s in available_sources if classify_source(s).uses_feeder]
        if not feeders:
            msg = (
                "Manual duplex needs a document feeder, but the device reports "
                f"no feeder source. Available: {available_sources}"
            )
            raise ScanError(msg)
        return feeders[0], has_source_option

    # ... existing validate-or-substitute-Auto path, unchanged ...
```

**Why the requested name is still preferred:** it reconciles D-01 ("`source` is passed to the
device verbatim"), D-02 ("resolved at scan time from the device's own source list") and D-18 (the
how-to teaches `source = "ADF"` + `duplex = "manual"`). Under the alternative — ignore `source`
entirely whenever `duplex == "manual"` — the `source` value D-18 tells users to write becomes
decorative, and a user who deliberately selects one of two feeders gets the other one silently.
**Flagged for the planner:** this is a judgement call at the seam between two locked decisions, and
the plan should record it explicitly. See Open Question 2.

### Pattern 3: Total dispatch (D-07), the house rule

```python
# Source: pipeline.py:871 (the isinstance defect) -> the shape Phase 21 D-08 measured.
# N-07's other half: the tuple already became _DuplexMismatch in an earlier phase.
# Say so in a comment, or a later reader comparing code to the review reads it as unaddressed.
match duplex_result:
    case ScanBatch():
        batch = duplex_result
    case _DuplexMismatch():
        ...  # recovery; returns early
    case _:
        assert_never(duplex_result)
```

### Anti-Patterns to Avoid

- **Threading the coordinator as a new parameter into `_scan_manual_duplex`.** It has exactly five
  parameters and ruff's `PLR0913` limit is five (Finding 3, executed). Derive `notify` from
  `request` instead — `_handle_duplex_mismatch` already does exactly this and says why in a comment.
- **Adding a parameter to `_handle_duplex_mismatch`.** Also at the ceiling; the CONTEXT says so and
  I confirmed it (5 parameters).
- **A bare `assert` anywhere in `src/`.** `S101` is active outside `tests/`.
- **Writing a `SCANNING_REVERSE` branch into `status.html`.** The busy branch already handles it
  once the state joins `ACTIVE_STATES`, and adding a branch would *break* D-16 by giving pass B a
  presentation of its own instead of letting the flip prompt disappear (Finding 7).
- **Inventing a schema migration for the ninth state.** D-12 verified there is no `CHECK`
  constraint; I re-read `job.py:285-296` and confirm `state TEXT NOT NULL`.
- **Deleting the legacy substring rule outright.** D-01 deletes `pipeline._is_manual_duplex`, but
  DPLX-02 still needs the rule to *detect* a legacy config. It moves to `config.py` and is never
  consulted for strategy. See Finding 9.
- **Building a new `wait_for_state` helper.** One already exists at `tests/conftest.py:194` with a
  fixture wrapper. D-14's prohibition is against *building* one; *using* the existing one is the
  intended path (Finding 11).

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Translating a legacy config key on every construction path | A `load_settings` post-processing pass | `model_validator(mode="before")` on `ProfileConfig` | D-03. Executed: it fires for direct construction, nested dicts through `BaseSettings`, and instances. A loader-level pass misses the ~219 direct `ProfileConfig(source=...)` call sites in the test suite |
| Deciding whether a source name feeds paper | A new substring test | `classify_source()` / `SourceKind.uses_feeder` | Phase 24 D-01 made it the **only** rule, with a settled `UNKNOWN` policy and four load-bearing branch-order rules documented in its docstring |
| Mapping an outcome enum to behaviour | `dict[FlipOutcome, …]` | `match` + `assert_never` | Phase 21 D-08 **measured** that a dict missing a member draws no diagnostic from `ty` or `pyrefly`; the match is caught by both at edit time |
| Waiting for a worker-thread state change in a test | A `time.sleep(0.5)` or a new polling helper | `wait_for_state` fixture (`tests/conftest.py:194`) | Already exists, already used by `test_outcomes_e2e.py`, already raises a message naming the job and the observed state instead of a SIGALRM traceback |
| Driving two passes with different page counts | A bespoke stub | `MagicMock(spec=ScannerBackend)` + `scan_pages.side_effect=[batch, batch]` | The established seam; `tests/test_outcomes_e2e.py:_build_scanner` documents why a second call on a simplex case *should* raise `StopIteration` |
| Proving the real backend drives a real feeder name | A new fake | `tests/fake_sane.FakeSaneDev` + `report_sources([...])` | Its default list is `["Flatbed", "Automatic Document Feeder", "ADF Duplex"]` — **no plain "ADF"** — which is precisely the shape that falsifies C-01's hardcoded-`"ADF"` suggestion |
| Making a timeout observable without waiting | `monkeypatch` on `Event.wait` or a sleep | `flip_timeout_seconds = 0` | Measured at 4 µs. `_TIMEOUT_BUDGET = 0` in `test_outcomes_e2e.py` is the exact precedent, with a comment explaining why `0` and not `0.05` (the field is `int`; pydantic rejects a fractional float) |
| Asserting a `logger.warning` | Capturing stderr, or a handler of your own | `caplog.at_level(logging.WARNING, logger="saneless.config")` | Precedent at `tests/test_auto_profiles.py:617` and `:728` |

**Key insight:** every piece of machinery this phase needs was built by an earlier phase in this
same milestone, and each was built with a docstring explaining *why it is the only one*. The
failure mode for this phase is not "reached for the wrong library" — it is "quietly grew a second
rule beside an existing single rule", which is literally the defect (N-07) DPLX-03 exists to
remove. Any new helper that answers a question one of the assets above already answers should be
treated as a planning error.

---

## Common Pitfalls

### Pitfall 1: The coordinator refusal breaks ~10 existing pipeline tests at once

**What goes wrong:** The guard at the top of `run_pipeline` raises `ConfigError` for a
manual-duplex profile with no coordinator. Almost every manual-duplex test in
`tests/test_pipeline.py` constructs `PipelineRequest(profile_name=..., title=...)` with no flip
event at all — `test_manual_duplex_happy_path`, `test_duplex_mismatch_saves_partial_pdfs`,
`test_duplex_mismatch_reports_fallback_when_upload_falls_back`,
`test_duplex_mismatch_is_fallback_when_only_one_upload_falls_back`,
`test_duplex_match_still_interleaves_normally`, `test_manual_duplex_empty_page_after_interleave`,
`test_manual_duplex_thumbnail_from_first_page`, and the mismatch cases beyond them. They pass today
only because `pipeline.py:705` reads `if request.flip_event is not None:` and skips the wait.

**Why it happens:** The guard converts an optional collaborator into a required one. That is the
point (C-02: "an optional parameter that silently changes semantics is a trap"), but it means the
guard and the test migration are **one atomic change**.

**How to avoid:** Introduce a trivial always-continue test coordinator in `tests/conftest.py` (a
concrete `FlipCoordinator` subclass returning `FlipOutcome.CONTINUED`) in the *same* task as the
guard, and update every duplex `PipelineRequest` construction in the same commit. Do not split.

**Warning signs:** a task that says "add the refusal guard" without also naming
`tests/test_pipeline.py`.

### Pitfall 2: `_scan_manual_duplex` is one parameter from a lint failure

**What goes wrong:** The obvious implementation adds `timeout: float` to
`_scan_manual_duplex(scanner, device_id, scan_settings, request, notify)`, producing six
parameters. Ruff's `PLR0913` limit is five; `CLAUDE.md` forbids raising the limit and forbids
`# noqa`.

**Why it happens:** The function already absorbed a `notify` parameter that is derivable.

**How to avoid:** Drop `notify` and derive it inside: `notify = request.status_callback or
_noop_callback`. `_handle_duplex_mismatch` does exactly this and its comment says why ("Derived
rather than passed: it is exactly what the caller would hand us"). Then add `timeout: float`. Five
parameters, no suppression.

**Warning signs:** `uv run ruff check .` reporting `PLR0913 Too many arguments in function
definition (6 > 5)`.

### Pitfall 3: The before-validator silently overrides an explicit `duplex`

**What goes wrong:** A config carrying both `source = "Manual Duplex"` and an explicit
`duplex = "none"` — perhaps mid-migration — has its explicit value overwritten by the translation.
The operator's stated intent loses to an inference.

**Why it happens:** The natural one-liner is `data = {**data, "duplex": "manual"}`, which
unconditionally wins. I executed this and confirmed the overwrite.

**How to avoid:** Translate only when `"duplex"` is absent from the input mapping. Explicit
configuration should always beat inference, and the deprecation warning should then fire only for
the profiles that actually relied on the inference.

**Warning signs:** a test asserting `ProfileConfig(source="Manual Duplex", duplex="none").duplex ==
"manual"` — that is the defect written down as a contract.

### Pitfall 4: `PipelineEvent.job_state` becomes non-Optional and two `if state is None` branches go dead

**What goes wrong:** Once `SCANNING_REVERSE` has a `JobState` twin, **every** `PipelineEvent`
member maps, so the property's return type narrows from `JobState | None` to `JobState`. Two
callers still branch on `None`: `cli.py:125-131` and `worker.py:216-220`. Left in place they are
statically unreachable code that ruff's `SIM`/`RET` families and both type checkers may flag — and
if the property's annotation is narrowed while the branches stay, the commit-stage `ty check src`
gate fails.

**Why it happens:** The seam was deliberately typed as optional and documented as such
(`pipeline.py:56-76`); the phase fills it in.

**How to avoid:** Narrow the annotation, delete both branches, and replace the reserving comment
with the answer — all in one commit. D-13 anticipates this exactly: `progress_label` returns
`"Scanning reverse sides..."`, byte-identical to what `cli.py:131` already echoes, so the CLI's
output is unchanged.

**Warning signs:** `cli.py` still containing the words "SCANNING_REVERSE is the only event that
persists no state" after this phase.

### Pitfall 5: `tests/test_vocabulary.py` has hand-written lists a count guard will not catch

**What goes wrong:** Bumping `len(list(JobState)) == 8` to `9` looks like the whole vocabulary
change. It is not. Three further sites in the same file carry **hand-written** rosters that the
parametrised completeness tests do not cover:

- `test_active_states_membership` — an explicit five-member `frozenset`
- `TestStateLabel::test_state_label_strings` — an eight-row `parametrize`
- `TestProgressLabel::test_progress_label_strings` — a five-row `parametrize`

**Why it happens:** Phase 21 D-09's "parametrise over `list(EnumType)`" rule is applied to the
*completeness* tests; the *literal-text* tests are deliberately hand-written because their whole
job is to pin exact strings.

**How to avoid:** Treat the vocabulary change as five edits in `vocabulary.py` and four in
`tests/test_vocabulary.py`, enumerated in Finding 7.

### Pitfall 6: No git hook runs pytest — "green at every commit" is unenforced

**What goes wrong:** A planner assumes the commit gate will catch a broken suite. It will not.
`.pre-commit-config.yaml` runs ruff, `ty check src`, `pyrefly check src` at commit; ruff-full, `ty
check`, `pyrefly check src tests` at pre-merge-commit and pre-push. **Pytest appears nowhere.**

**Why it happens:** The gate is a *type and lint* gate by design — that is what makes a TDD RED
commit possible without suppression (23.1 D-01/D-02).

**How to avoid:** Put the suite command in the task's own verification steps. And note the
corollary: a RED commit is safe from the hooks, but a commit that adds an enum member without its
`match` arms is **not** — that is a `ty check src` failure at commit stage, so enum member and
label arms must land together.

### Pitfall 7: A `MagicMock` scanner cannot prove C-01 is fixed

**What goes wrong:** Every existing manual-duplex test uses `MagicMock(spec=ScannerBackend)`, which
accepts any `ScanSettings` including `source="Manual Duplex"`. C-01's own text says this is why the
defect survived: "Every manual-duplex test in the suite passes because the scanner is a
`MagicMock(spec=ScannerBackend)` that accepts any source string."

**Why it happens:** The mock has no source list to validate against.

**How to avoid:** One test must drive the real `SaneBackend` against `FakeSaneDev`. The seam
already exists at `tests/test_pipeline.py:806-856`
(`test_both_passes_run_through_the_backend_and_interleave`) — but today it calls
`dev.report_sources(["Flatbed", "ADF Manual Duplex"])`, i.e. it makes the pseudo-source real so the
test can pass. **That line must become a realistic list such as
`["Flatbed", "Automatic Document Feeder"]`**, which is the assertion that actually proves C-01 is
fixed and simultaneously proves D-02's "do not hardcode `ADF`".

**Warning signs:** the string `"ADF Manual Duplex"` surviving in any `report_sources` call.

### Pitfall 8: SIGALRM reports the wrong thread

**What goes wrong:** A worker-thread hang runs to `timeout = 60` and pytest-timeout's `signal`
method delivers SIGALRM to the **main** thread, printing a traceback for the test's wait loop
rather than for the stuck worker.

**Why it happens:** Documented in `tests/conftest.py:208-210` and `test_outcomes_e2e.py:600-603`,
both of which use a 2 s budget for exactly this reason.

**How to avoid:** Every flip-related wait in a test uses `wait_for_state(..., timeout=2.0)`, never
the global ceiling. The flip *timeout* itself is made observable with `flip_timeout_seconds = 0`,
not by waiting.

---

## Code Examples

### 1. The legacy translation (D-03) — executed

```python
# EXECUTED this session against pydantic 2.12.5 / pydantic-settings 2.13.1.
# Results reproduced verbatim below the snippet.
class ProfileConfig(BaseModel):
    source: str = "Flatbed"
    duplex: Literal["none", "hardware", "manual"] = "none"

    @model_validator(mode="before")
    @classmethod
    def _translate_legacy_manual_duplex(cls, data: Any) -> Any:
        if isinstance(data, dict) and "duplex" not in data:   # <- Pitfall 3
            source = data.get("source")
            if isinstance(source, str) and _is_legacy_manual_duplex_source(source):
                data = {**data, "duplex": "manual"}
        return data
```

Measured output:

```
P(source="Manual Duplex")              -> source='Manual Duplex' duplex='manual'
P(source="ADF Manual Duplex")          -> source='ADF Manual Duplex' duplex='manual'
P(source="ADF", duplex="manual")       -> source='ADF'          duplex='manual'
P()                                    -> source='Flatbed'      duplex='none'
S(profiles={"legacy": {"source": "Manual Duplex"}, "default": {}})
    -> {'legacy': P(source='Manual Duplex', duplex='manual'),
        'default': P(source='Flatbed',      duplex='none')}
```

The nested case is the one that matters: it is the shape pydantic-settings produces after merging
TOML, env and init into one dict, and it confirms D-03's claim without needing the test D-03
forbids.

### 2. The deprecation warning (D-04 + D-18's binding constraint)

Add a **second** `field_validator("profiles")` rather than extending `validate_default_profile`, so
each validator keeps one job. Both receive `dict[str, ProfileConfig]` — already constructed, already
translated — which is why `source` is still inspectable here (D-01 keeps it verbatim).

```python
# Source: config.py:177-187 (the existing validator this sits beside).
@field_validator("profiles")
@classmethod
def warn_on_legacy_duplex_source(
    cls, v: dict[str, ProfileConfig]
) -> dict[str, ProfileConfig]:
    for name, profile in v.items():
        if profile.duplex == "manual" and _is_legacy_manual_duplex_source(profile.source):
            logger.warning(
                "Profile %r requests manual duplex through the deprecated "
                "source value %r. saneless has read it as duplex = \"manual\" "
                "for this run. Update the profile to set duplex = \"manual\" "
                "and source to a source your scanner actually reports -- run "
                "'saneless devices --capabilities' to list them.",
                name,
                profile.source,
            )
    return v
```

D-18 makes the message content binding: no documentation page will mention the legacy key any more,
so this text is the operator's only migration instruction. It names the profile, names the
replacement key *and* its value, and names the command that produces the replacement `source`.
**It deliberately makes no removal promise** — no decision in this phase authorises one.

`config.py` has no `logger` today; one must be added (`logger = logging.getLogger(__name__)`),
which is why the `caplog` assertion targets `logger="saneless.config"`.

### 3. The refusal guard (DPLX-04, discretion item 2)

```python
# Source: pipeline.py:833-838 -- inserted between the profile lookup and _resolve_device.
profile = settings.profiles[request.profile_name]

# BEFORE _resolve_device, which calls get_devices() when scanner.device is
# empty. C-02: "the pipeline must refuse to run without [the collaborator]".
if profile.duplex == "manual" and request.flip_coordinator is None:
    msg = (
        f"Profile {request.profile_name!r} uses manual duplex, which needs a "
        "flip coordinator to tell saneless when the stack has been turned over. "
        "No coordinator was supplied."
    )
    raise ConfigError(msg)

device_id = _resolve_device(scanner, settings)
```

The observable claim "before the scanner is opened" is tested as
`scanner.get_devices.assert_not_called()` — see Validation Architecture, criterion 2.

### 4. The CLI interactivity seam (Finding 4 — the blocker)

```python
# Source: cli.py. NEW -- there is no isatty, click.confirm, click.prompt or
# input() anywhere in cli.py today; this is the CLI's first interactive read.
def _stdin_is_interactive() -> bool:
    """Whether stdin is a terminal a human can answer a prompt on."""
    return sys.stdin.isatty()
```

The indirection is one line and it is what makes both halves of D-11 testable:

- the **refusal** test calls `runner.invoke(...)` with no patch (measured: `isatty()` is `False`
  under `CliRunner`) and asserts exit code 2;
- the **prompt** test does
  `monkeypatch.setattr("saneless.cli._stdin_is_interactive", lambda: True)` and then
  `runner.invoke(cli, [...], input="y\n")`.

Without the seam these two tests contradict each other and one of them cannot be written.

### 5. `click.confirm` under `CliRunner` — executed

```python
# EXECUTED this session against click 8.3.1.
@click.command()
def probe() -> None:
    click.echo(f"stdin.isatty={sys.stdin.isatty()}")
    click.echo("Scanning...")
    ok = click.confirm("Continue with the back sides?", default=True)
    click.echo("Scanning reverse sides...")
```

| Input | `result.output` | exit | exception |
|-------|-----------------|------|-----------|
| `"y\n"` | `stdin.isatty=False\nScanning...\nContinue with the back sides? [Y/n]: y\nanswered=True\nScanning reverse sides...\n` | 0 | — |
| `"n\n"` | `...Continue with the back sides? [Y/n]: n\nanswered=False\n...` | 0 | — |
| `""` (EOF) | `...Continue with the back sides? [Y/n]:Aborted!\n` | 1 | `SystemExit` |

Three consequences for the plan:

1. **The prompt text is echoed into `result.output`**, between `Scanning...` and
   `Scanning reverse sides...` — C-02's assertion is achievable exactly as written.
2. **EOF raises `click.Abort` → exit 1**, not a `FlipOutcome`. The CLI coordinator must decide
   whether to let that propagate (exit 1, a scan error) or catch it and return
   `FlipOutcome.ABORTED`. Recommendation: **catch it and return `ABORTED`**, so Ctrl-C at the prompt
   and clicking Abort in the web UI produce the same job outcome, which is what D-15 describes.
3. `click.pause` measured as a **complete no-op** off a terminal (`'before pause\nafter pause\n'`)
   — D-11's disqualification independently re-confirmed.

### 6. The DPLX-07 round-trip, generalised (Finding 10)

`tests/test_auto_profiles.py:548` already proves the feeder-only case. DPLX-07 asks for three.
Parametrise the one that exists:

```python
# Source: tests/test_auto_profiles.py:548-567 (the existing feeder-only case).
@pytest.mark.parametrize(
    ("label", "sources", "expected_default_source"),
    [
        ("flatbed-only", ["Flatbed"], "Flatbed"),
        ("feeder-only", ["Automatic Document Feeder", "ADF Duplex"],
         "Automatic Document Feeder"),
        ("mixed", ["Flatbed", "Automatic Document Feeder", "ADF Duplex"], "Flatbed"),
    ],
)
def test_generated_config_round_trips_with_a_default_profile(...) -> None:
    caps = DeviceCapabilities(sources=list(sources), resolutions=[300], modes=["Color"])
    config_file = tmp_path / "config.toml"
    write_profiles_to_config(config_file, generate_profiles(caps))
    settings = load_settings(str(config_file))          # <- the guarantee that matters
    assert settings.profiles["default"].source == expected_default_source
```

### 7. Emitting `duplex` into TOML (D-06)

`write_profiles_to_config` writes `source`, `resolution`, `mode`, then `auto_source_mode` **only
when non-default**, then `auto_generated`. D-06 follows that precedent in one line:

```python
# Source: auto_profiles.py:519-527.
profile_table.add("mode", profile.mode)
if profile.auto_source_mode != "flatbed":
    profile_table.add("auto_source_mode", profile.auto_source_mode)
if profile.duplex != "none":                       # <- D-06, Phase 16 precedent
    profile_table.add("duplex", profile.duplex)
```

And in `generate_profiles`, one expression beside the existing `auto_source_mode=` argument:
`duplex="hardware" if classify_source(source) is SourceKind.FEEDER_DUPLEX else "none"`. Note this
is `classify_source`'s **fourth** consumer — correct per Phase 24 D-01, and it must not be a fresh
substring test.

---

## Runtime State Inventory

> Rename/refactor scope check. This phase renames a config key's *meaning* and adds a persisted
> enum value, so the inventory is not vacuous.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| **Stored data** | The `jobs.state` column will start carrying a ninth value, `"SCANNING_REVERSE"`. Schema is `state TEXT NOT NULL` (`job.py:289`) with **no `CHECK` constraint** — re-verified by reading the DDL. Existing rows are unaffected; no row anywhere stores `duplex`. | **None.** No migration, no `PRAGMA user_version` step. D-12 says so and I confirmed it. **A planner must not invent a migration task.** |
| **Live service config** | User TOML files on real installations may contain `source = "Manual Duplex"`. These live outside git, on the operator's machine, and saneless never rewrites them. | **Code edit only** — the before-validator translates at load. Deliberately **no data migration**: D-18 accepts that the file keeps working untouched, and D-04's warning is the migration instruction. `auto-profiles --force` merge semantics for `duplex` are Phase 27's (CFG-07) and must not be pulled forward. |
| **OS-registered state** | None. saneless registers no scheduled tasks, systemd units or pm2 processes as part of this phase. Verified: no `Task Scheduler`, `launchd`, `systemd` or `pm2` artefact is produced by any code path changed here. | **None.** |
| **Secrets / env vars** | `SANELESS_PROFILES__<NAME>__SOURCE` env overrides exist by virtue of `env_nested_delimiter="__"`. A legacy value supplied that way is translated identically — the before-validator runs after source merging (executed, Code Example 1). No secret is involved: `ProfileConfig` carries no credential, so the deprecation warning cannot leak one. | **None.** A new `SANELESS_OUTPUT__FLIP_TIMEOUT_SECONDS` override appears for free and needs no registration. |
| **Build artifacts / installed packages** | None. No package rename, no `pyproject.toml` name change, no entry-point change. `uv sync` is unaffected because no dependency moves. | **None.** |

**Nothing found in three of five categories, and that is a measurement, not a blank.** The only
runtime state this phase touches is the `jobs.state` column, and it was verified to accept the new
value without schema work.

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `source` string carries both the SANE device input and the saneless strategy | Separate `duplex` field; `source` is a pure device value | This phase (D-01) | Removes C-01. Two layers stop disagreeing about what one field means |
| `pipeline._is_manual_duplex` + `worker.py:194`'s substring copy | One reader: `profile.duplex == "manual"` | This phase (DPLX-03) | Removes N-07's drift risk, which *is* the C-02 failure |
| `flip_event` + `abort_event`, wake-then-ask-why | One `FlipCoordinator`, one atomic `FlipOutcome` | This phase (D-09) | Removes M-02's dead Abort at its root |
| `request.flip_event.wait()` — unbounded | `wait_for_flip(timeout)` with `flip_timeout_seconds = 600` | This phase (D-10, M-07) | A forgotten prompt becomes a reported error instead of a parked worker thread |
| `SCANNING_REVERSE.job_state → None` + `_transition_event` | Ninth `JobState`; the event protocol deleted entirely | This phase (D-12, D-14) | The flip prompt disappears when pass B starts, and `routes.py:297` stops blocking the event loop for 2 s |
| Scanner-side blank-page removal (pre-Phase 24) | Blank removal only in `_drop_empty_pages`, only when the profile enables it | Phase 24 (SCNR-03) | **Duplex page parity is now the backend's contract**, which is why Phase 24 had to land first |
| Three independent source-classification rules | One `classify_source()` with `SourceKind` | Phase 24 (D-01) | D-02's feeder resolution consumes it; it is not rewritten |
| `list()`-wrapped generator from `scan_pages` | Eager `ScanBatch` with `actual_resolution` + `pages_rejected` | Phase 24 (D-12) | **The device is already closed between passes** — criterion 3's "releases the scanner" is about the worker thread, not the handle |

**Deprecated / outdated as of this phase:**

- `source = "Manual Duplex"` — still *works* (DPLX-02) but is removed from all documentation (D-18)
- `ScanWorker.wait_transition` and `_transition_event` — deleted outright (D-14)
- `PipelineRequest.flip_event` / `.abort_event` — replaced by `.flip_coordinator` (D-09)
- `PipelineEvent.job_state` returning `None` — the seam is filled; the type narrows (Finding 8)

---

## Detailed Findings

### Finding 1 — the before-validator covers every construction path `[VERIFIED: executed]`

Executed against this project's own pydantic 2.12.5 / pydantic-settings 2.13.1. Translation fired
for direct construction, for a nested raw dict inside a `BaseSettings` field, and for an
already-constructed instance (because it was translated at *its* construction). Output reproduced
in Code Example 1. This is the mechanism that covers the ~219 `ProfileConfig(source="...")` sites
across the suite without touching them all.

### Finding 2 — two field validators, not one `[VERIFIED: read]`

`Settings.validate_default_profile` (`config.py:177-187`) is a `field_validator("profiles")`
receiving `dict[str, ProfileConfig]`. Pydantic permits multiple field validators on one field, so
D-04's warning belongs in a second, separately-named validator rather than bolted onto the
default-profile check. `config.py` currently defines **no logger** — one must be added, and the
`caplog` assertion targets `logger="saneless.config"`.

### Finding 3 — `PLR0913` blocks the obvious `_scan_manual_duplex` change `[VERIFIED: executed]`

```
PLR0913 Too many arguments in function definition (6 > 5)
```

`_scan_manual_duplex(scanner, device_id, scan_settings, request, notify)` has exactly five.
`_handle_duplex_mismatch` likewise. Fix: derive `notify` from `request` (in-tree precedent with a
comment saying why) and spend the freed slot on `timeout: float`. D-09 locks the signature
`wait_for_flip(timeout: float)`, so the timeout **must** travel to the call site — it cannot be
bound at coordinator construction.

### Finding 4 — `CliRunner` is not a TTY, and that makes D-11 self-contradictory without a seam `[VERIFIED: executed]`

Measured: `sys.stdin.isatty()` and `sys.stdout.isatty()` both return `False` under
`CliRunner.invoke`. A refusal written as `if not sys.stdin.isatty(): sys.exit(2)` therefore fires
in **every** CliRunner test, including the one that is supposed to reach `click.confirm`. The
one-line `_stdin_is_interactive()` indirection (Code Example 4) is the fix and it is the single
highest-risk omission in this phase's plan.

Also measured: `click.confirm` **echoes its prompt into `result.output`** (so C-02's ordering
assertion works verbatim); answering `n` returns `False` cleanly at exit 0; EOF raises `Abort` →
exit 1 with `SystemExit`; and `click.pause` off a terminal is a **total no-op**, independently
re-confirming D-11's disqualification.

### Finding 5 — module placement for the two new types `[CITED: vocabulary.py:1-8 module docstring]`

`vocabulary.py`'s docstring forbids importing from `job.py`, `pipeline.py`, `worker.py`, `cli.py`
or `web/`. `FlipOutcome` imports nothing, so it satisfies the rule and is *exactly* the kind of
thing that module exists for — a closed set of words consumed by `match` + `assert_never`, the
`ScanOutcome` precedent. `FlipCoordinator`, being a runtime seam rather than a word, belongs in
`pipeline.py` beside `PipelineRequest`, which carries it. No new import edge is created: `cli.py`
and `worker.py` already do `from .pipeline import PipelineEvent, PipelineRequest, run_pipeline`.

### Finding 6 — a zero-cost timeout seam `[VERIFIED: executed]`

`threading.Event().wait(timeout=0)` returns `False` in ~4 µs. `OutputConfig.flip_timeout_seconds`
is typed `int`, so `0` is valid and `0.05` is not — pydantic rejects a fractional float in lax int
mode. This is the same trap `test_outcomes_e2e.py:104-118` already documents at length for
`paperless_task_timeout`, and `_TIMEOUT_BUDGET = 0` is the precedent to copy.

### Finding 7 — the JobState ripple is four test edits, not ten `[VERIFIED: read + reasoned]`

Ten sites parametrise over `list(JobState)` (4 in `test_vocabulary.py`, 6 in
`test_web_state_rendering.py`) and one iterates it (`test_job.py:443`). Of these, **only the
hand-written rosters need touching**:

*Must edit (4, all in `tests/test_vocabulary.py`):*
- `:55` `len(list(JobState)) == 8` → `9`
- `test_active_states_membership` — explicit 5-member `frozenset` → 6
- `TestStateLabel::test_state_label_strings` — 8-row `parametrize` → 9 (`"Scanning backs"`)
- `TestProgressLabel::test_progress_label_strings` — 5-row `parametrize` → 6
  (`"Scanning reverse sides..."`)

*Needs no edit and passes automatically:* all six `test_web_state_rendering.py` cases and
`test_job.py:443`, because each builds its expectation from `state_label` / `progress_label` /
`ACTIVE_STATES` / `BUSY_STATES` rather than from a literal list. `cli.py:47`'s `_STATUS_COL_WIDTH`
is likewise derived (`max(len(state_label(s)) for s in JobState)`) and self-updates.

**The bonus:** `test_status_area_prose` already asserts
`("flip-prompt" in text) is (state is JobState.AWAITING_FLIP)` **parametrised over every state**.
The moment `SCANNING_REVERSE` exists and is in `ACTIVE_STATES` (hence `BUSY_STATES`), that test
proves D-16's core guarantee — "Continue and Abort vanish during pass B" — with zero new test code.

*Also must edit, in `tests/test_pipeline.py`:* `test_scanning_reverse_has_no_job_state` (it asserts
the defect), and `test_job_state_projection_mapping` (add the new row). `test_job_state_projection_is_total`
becomes vacuous once the type is non-Optional — tighten it to
`assert isinstance(event.job_state, JobState)`.

*Does **not** break:* `tests/test_pipeline.py:987`'s `len(PipelineEvent) == 6`. `PipelineEvent`
gains no member; only its projection changes. N-11 complained about this pin, and it turns out to
be harmless here.

### Finding 8 — `PipelineEvent.job_state` narrows to non-Optional `[VERIFIED: read]`

After D-12, all six members map to a `JobState`. The property's `JobState | None` becomes
`JobState`, which collapses `cli.py:125-131` and `worker.py:216-220`. D-13's byte-identical
progress string is what makes the CLI collapse invisible to users. See Pitfall 4 for the ordering
constraint (annotation and both branches in one commit, or `ty check src` fails at commit stage).

### Finding 9 — where the substring rule survives `[VERIFIED: read]`

D-01 deletes `pipeline._is_manual_duplex`, but DPLX-02 still needs the *detection* rule to
recognise a legacy config. It moves to `config.py` as a private predicate with exactly two callers
— the before-validator and the deprecation validator — and is never consulted for strategy.

`tests/test_pipeline.py` imports `_is_manual_duplex` at line 29 and exercises it in
`TestIsManualDuplex` (`:237-259`, six cases). Those tests should **move to `tests/test_config.py`
and retarget the new predicate**, not be deleted: the rule still exists and still deserves its
edge cases.

**The DPLX-03 grep gate** (a cheap, exact acceptance check):

```bash
grep -rn '"manual" in\|"duplex" in\|_is_manual_duplex' src/
# Must return ONLY config.py's legacy predicate.
```

### Finding 10 — DPLX-07 is already satisfied; it reduces to two tests `[VERIFIED: read]`

`generate_profiles` (`auto_profiles.py:293-380`) reserves the `"default"` slug up front, backs it
with a flatbed when one exists and otherwise with the device's first reported source, and its
docstring names `Settings.validate_default_profile` as the reason. `tests/test_auto_profiles.py`
already has `test_real_flatbed_still_backs_the_default_profile` (:521),
`test_feeder_only_device_still_gets_a_default_profile` (:531) and — crucially — one genuine
**write-then-load round-trip**, `test_a_feeder_only_config_round_trips_through_load_settings`
(:548). Missing: the flatbed-only and mixed round-trips. Generalise the existing one (Code
Example 6). **The CONTEXT's prediction was exactly right; no emission logic is missing.**

### Finding 11 — D-14 and ROBU-03 are not in conflict `[VERIFIED: read]`

D-14 deletes `wait_transition` and says "do not build a `wait_for_state` polling helper here". A
`wait_for_state` helper **already exists** at `tests/conftest.py:194-244`, with a fixture wrapper at
`:247`, added in Phase 23 and already consumed by `tests/test_outcomes_e2e.py`. The four worker
tests that lose their synchronisation primitive (`:729`, `:758`, `:805`, `:1400`) should simply
*use* it. ROBU-03 owns converting the *other* ~26 sleep-based worker tests. Reading D-14 as
"those four tests must invent their own waiting" would be a misread that costs the phase a working
test suite.

### Finding 12 — D-17's lookup is duplicated **four** times, not three `[VERIFIED: read]`

D-17 names `current_job_status`, `continue_flip` and `abort_flip`. `index` (`routes.py:81-87`)
carries a fourth, byte-equivalent copy of the same "current job, else most recent" logic. Extracting
the helper and leaving `index` behind would leave the duplication D-17 exists to remove. **Flagged
as a finding, not silently adopted** — the planner should either extend D-17's scope to four sites
or record why the fourth is excluded.

### Finding 13 — the fake device's source list falsifies the hardcoded-`"ADF"` approach `[VERIFIED: read]`

`tests/fake_sane.py:109`: `_DEFAULT_SOURCES = ["Flatbed", "Automatic Document Feeder", "ADF Duplex"]`.
There is **no plain `"ADF"`**. A test driving the real backend against the default fake is
therefore an automatic regression test for C-01's declined suggestion: a hardcoded `"ADF"` raises
`ScanError: Device does not support source 'ADF'`, while `classify_source`-driven selection picks
`"Automatic Document Feeder"`. This makes D-02's rationale testable rather than merely argued.

### Finding 14 — the scanner package's import boundary currently holds `[VERIFIED: executed grep]`

`grep -rn "vocabulary\|JobState\|ScanOutcome" src/saneless/scanner/` returns **nothing**.
`scanner/base.py` imports only stdlib; `sane_backend.py` imports `saneless.exceptions`,
`saneless.paper_sizes` and `saneless.scanner.base`. Phase 21 D-03 is intact, and whatever signal
`ScanSettings` carries must keep it that way — which is the constraint behind the first discretion
item.

### Finding 15 — no hook runs pytest `[VERIFIED: read `.pre-commit-config.yaml`]`

Commit stage: ruff (with `--fix`), `ty check src`, `pyrefly check src`. Pre-merge-commit and
pre-push add `ty check`, `pyrefly check src tests`, `ruff check --no-fix .`, `ruff format --check .`.
**Pytest appears in no stage.** A TDD RED commit therefore needs no suppression (23.1 D-01/D-02) —
and "the suite stays green at every commit" must be enforced by task design, because nothing will
catch a violation.

---

## Sequencing — how to keep the suite green

TDD mode is on and a RED commit must land with hooks enabled and no suppression. The binding
mechanical constraints are: (a) `ty check src` runs at **commit** stage, so an enum member without
its `match` arms fails immediately; (b) pytest runs at **no** stage; (c) the refusal guard breaks
~10 tests the instant it lands.

| Wave | Content | Why here | Suite state |
|------|---------|----------|-------------|
| **A — Config** | `duplex` field + before-validator + legacy predicate + second field validator + `flip_timeout_seconds`. Move `TestIsManualDuplex` to `test_config.py`, retargeted. | Nothing reads `duplex` yet; `_is_manual_duplex` still drives the pipeline, so **no behaviour changes**. | Green throughout |
| **B — Vocabulary** | `JobState.SCANNING_REVERSE` + both label arms + `ACTIVE_STATES` + `PipelineEvent.job_state` narrowed + the two `state is None` branches deleted + 4 `test_vocabulary.py` edits + 2 `test_pipeline.py` edits. | Enum member and `match` arms **must** be one commit (`ty check src`). `test_web_state_rendering.py` needs no edits (Finding 7). | Green after the wave's single commit |
| **C — Coordinator** | `FlipOutcome`, `FlipCoordinator` ABC, `PipelineRequest` field swap, `_scan_manual_duplex` rewrite (drop `notify`, add `timeout`), the refusal guard, D-07's `match` dispatch, D-08's comment, delete `_is_manual_duplex` + the worker's duplicated rule. Migrate every duplex `PipelineRequest` in the same commit. | The guard and the ~10 test migrations are **atomic** (Pitfall 1). Depends on B for `SCANNING_REVERSE`. | RED→GREEN within the wave; split into C1 (types+pipeline) and C2 (worker+routes) if the diff is unwieldy |
| **D — Worker & web** | `WorkerFlipCoordinator`, delete `_transition_event` entirely, D-16/D-17 route changes, convert the four worker tests to the existing `wait_for_state`. | Depends on C's coordinator existing. | Green |
| **E — Scanner** | `ScanSettings` signal + `_resolve_source` feeder selection + no-feeder refusal. Retarget `test_pipeline.py:815`'s `report_sources` to a realistic list. | Depends on C for the strategy flag. This is the wave that actually closes C-01. | Green |
| **F — CLI** | `_stdin_is_interactive`, `ClickFlipCoordinator`, exit-2 refusal, `status_callback` cleanup, CLI tests. | Depends on C's ABC and E's working device path. | Green |
| **G — auto-profiles + docs** | D-06 emission, DPLX-07 round-trips, all doc rewrites, UI-SPEC. | Independent of A–F except for the `duplex` field (Wave A). Could run parallel to D–F. | Green |

**Parallelisation note:** Wave G's auto-profiles half depends only on Wave A and can run
concurrently with C–F. Waves B through F are a strict chain.

---

## Documentation Scope (discretion item 5)

Every sentence below was read this session. Rows marked **named** appear in `<canonical_refs>`;
rows marked *new* are ones I found that the CONTEXT does not list.

| File | Lines | Change | Source |
|------|-------|--------|--------|
| `docs/how-to/set-up-adf-duplex.md` | 44-82 | Rewrite § Manual Duplex: drop `source = "Manual Duplex"` (:68), drop the substring rule sentence (:64), replace "press Enter (CLI)" (:60), teach `source = "ADF"` + `duplex = "manual"` | named (D-18, doc rows 1, 2) |
| ″ | 48-52 | The admonition claims *"A source name containing 'duplex' is treated as a feeder source"* — that is now a `classify_source` statement, not a strategy statement, and reads as the old rule | *new* |
| ″ | 77 | "Continue and **Cancel** buttons" — the button is `Abort scan` | *new* (same defect as doc row 24) |
| ″ | 100-105 | Add D-08's note: blank-page removal is deliberately skipped on the mismatch path, because a blank back is evidence | named (D-08) |
| `docs/reference/configuration.md` | 63 | `source` row lists `ADF Manual Duplex` → remove; add a `duplex` row | named |
| ″ | `[output]` table | Add `flip_timeout_seconds` (int, 600) | *new* (D-10 implies it) |
| `docs/how-to/configure-scan-profiles.md` | 56 | `source` row → add a `duplex` row beneath | *new* |
| ″ | 71-77 | § "Source values" — the manual-duplex pointer must name the new two-key form | *new* |
| `docs/reference/web-api.md` | 124-136 | Flip endpoint responses (D-16/D-17) | named (doc row 22) |
| `docs/reference/cli-commands.md` | 29-36 | `scan` exit-code table: code 2 gains the non-TTY refusal | *new* (D-11 implies it) |
| `docs/how-to/cli-scripting.md` | 56-57 | The eight-state list is falsified by a ninth | named |
| ″ | 67-77 | Exit-code table gains D-11's case | named |
| `docs/getting-started/first-web-ui-scan.md` | 42 | "face-up" → flip the stack; "**Cancel**" → `Abort scan` | named (doc row 24) |
| ″ | 35-38 | The stage list (Scanning / Assembling / Uploading / Done) omits the flip and pass B entirely | *new* |
| `docs/explanation/architecture.md` | 28-33 | Pipeline stage 3 | named |
| ″ | 51 | "the worker blocks between pass A and pass B using a `threading.Event`" → `FlipCoordinator` + bounded timeout | named |
| `.planning/UI-SPEC.md` | ~197 | "Status states \| **Nine** presentations" — **re-checked as instructed:** the status-area table at :238-247 lists **eight** rows and omits `AWAITING_FLIP`, so the summary already disagrees with its own table. After this phase there are **ten** presentations | named (the CONTEXT asks for exactly this re-check) |
| ″ | 238-247 | Add a `SCANNING_REVERSE` row **and** the missing `AWAITING_FLIP` row | named + *new* |
| ″ | 269 | Humanised-label list gains `Scanning backs` | named |
| ″ | 300-313 | htmx map: flip-route behaviour under D-16/D-17 | named |
| `docs/PRD.md` | 110, 116 | Lists `ADF Manual Duplex` as a scanner source and describes the two-pass flow | **Open Question 4** — the PRD records what was asked for, so it is arguably a historical record rather than a claim about current behaviour |

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python | everything | ✓ | 3.14 | — |
| uv | all commands | ✓ | (resolves `uv run`) | — |
| pytest | validation | ✓ | 9.0.2 | — |
| pytest-timeout | the hang guard | ✓ | 2.4.0 | — |
| click | D-11's prompt | ✓ | 8.3.1 | — |
| pydantic / pydantic-settings | D-01/D-03 | ✓ | 2.12.5 / 2.13.1 | — |
| ruff | lint + format gates | ✓ | (per `uv.lock`, ≥0.15.5) | — |
| ty | type gate | ✓ | (≥0.0.80) | — |
| pyrefly | type gate | ✓ | (≥1.2.0) | — |
| prek | hook runner | ✓ | (≥0.3.5) | — |
| chromium / playwright | `@pytest.mark.browser` tests | **unverified** | — | `uv run pytest -m "not browser"` for quick loops |
| libsane-dev / real SANE | `@pytest.mark.sane_hardware` | not needed | — | Every claim in this phase is provable against `tests/fake_sane.py` |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** chromium is unverified. `uv run pytest -q` completed
**966 passed in 34.18s** this session with no deselection reported, which suggests browser tests
ran or were collected and skipped; the planner should confirm with
`uv run pytest -m "not browser" -q --collect-only` before pinning a quick-run command. No browser
test in this phase's scope requires a new capability — `TestFlipPromptUI` already exists and only
asserts the idle page.

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 + pytest-timeout 2.4.0 |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_pipeline.py tests/test_worker.py tests/test_config.py -q` |
| Full suite command | `uv run pytest -q` |
| Measured baseline | **966 passed in 34.18s** (executed this session, 2026-09-13) |
| Global timeout | `timeout = 60`, `timeout_method = "signal"` (SIGALRM → **main** thread) |
| Strictness | `filterwarnings = ["error"]`, `xfail_strict`, `--strict-markers`, `--strict-config` |
| Static gates | `uv run ruff check .` · `uv run ruff format --check .` · `uv run ty check` · `uv run pyrefly check src tests` (**paths always named** — 23.1 D-10) |

**No new marker is needed.** `--strict-markers` is live, so if the planner adds one it must be
registered in `pyproject.toml` first.

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| DPLX-01 | `ProfileConfig().duplex == "none"`; the three literals accepted; a fourth rejected by pydantic | unit | `uv run pytest tests/test_config.py -q` | ✅ |
| DPLX-01 | `source` reaches `ScanSettings` verbatim for a non-duplex profile | unit | `uv run pytest tests/test_pipeline.py -q` | ✅ |
| DPLX-01 | **Grep gate:** no strategy read of `source` survives in `src/` | static | `grep -rn '"manual" in\|"duplex" in\|_is_manual_duplex' src/` → only `config.py` | ✅ |
| DPLX-02 | `ProfileConfig(source="Manual Duplex").duplex == "manual"` via TOML, env var and direct construction | unit, parametrised | `uv run pytest tests/test_config.py -q` | ✅ |
| DPLX-02 | An explicit `duplex` is **not** overwritten by the translation (Pitfall 3) | unit | `uv run pytest tests/test_config.py -q` | ✅ |
| DPLX-02 | `load_settings` logs one WARNING **naming the profile** and naming the replacement | unit + `caplog` | `uv run pytest tests/test_config.py -q` | ✅ |
| DPLX-02 | A legacy config **still scans** end to end | e2e | `uv run pytest tests/test_outcomes_e2e.py -q` | ✅ (`_DUPLEX_SOURCE` case already exists) |
| DPLX-03 | `worker.py` no longer inspects `profile.source` | static + unit | grep + `uv run pytest tests/test_worker.py -q` | ✅ |
| DPLX-03 | `_DuplexMismatch` dispatch is a total `match`; a third variant fails the type gate | static | `uv run ty check` and `uv run pyrefly check src tests` | ✅ |
| DPLX-04 | Manual duplex with no coordinator raises `ConfigError` **and never touches SANE** — `scanner.get_devices.assert_not_called()` + `scan_pages.assert_not_called()` | unit | `uv run pytest tests/test_pipeline.py -q` | ✅ |
| DPLX-04 | CLI prompt appears **between** `Scanning...` and `Scanning reverse sides...`; `scan_pages` called twice | unit (`CliRunner(input="y\n")`, `_stdin_is_interactive` patched True) | `uv run pytest tests/test_cli.py -q` | ✅ — needs the Wave-0 seam |
| DPLX-04 | Non-TTY manual duplex exits **2** with a message naming an interactive terminal (no patch — `CliRunner` is genuinely not a TTY) | unit | `uv run pytest tests/test_cli.py -q` | ✅ |
| DPLX-04 | CLI answer `n` ⇒ `FlipOutcome.ABORTED` ⇒ job fails, fronts not uploaded | unit | `uv run pytest tests/test_cli.py -q` | ✅ |
| DPLX-04 | Web Continue ⇒ `CONTINUED`; the **second** signal is dropped (D-16) | unit | `uv run pytest tests/test_worker.py -q` | ✅ |
| DPLX-04 | Two passes run through the **real** `SaneBackend` against a device whose feeder is `"Automatic Document Feeder"` (proves C-01 **and** D-02's no-hardcoded-`ADF`) | integration (fake SANE) | `uv run pytest tests/test_pipeline.py -q` | ✅ (`:806`, needs `report_sources` retargeted) |
| DPLX-05 | `flip_timeout_seconds = 0` ⇒ job ends `ERROR` with a message naming the flip wait — **4 µs, no wall clock** | e2e (`_Case` + `wait_for_state`) | `uv run pytest tests/test_outcomes_e2e.py -q` | ✅ — `_Case` gains a `flip_timeout` field |
| DPLX-05 | After a timed-out job, a **second** submitted job reaches a terminal state (the worker thread was unparked) | e2e | `uv run pytest tests/test_outcomes_e2e.py -q` | ✅ |
| DPLX-06 | Pass B persists `SCANNING_REVERSE` — observed via `wait_for_state`, with a scanner stub whose 2nd `scan_pages` blocks on a test-held `Event` | unit (worker) | `uv run pytest tests/test_worker.py -q` | ✅ — needs the blocking stub |
| DPLX-06 | During `SCANNING_REVERSE` the status partial shows `aria-busy` and **no** `flip-prompt` (D-16) | unit, parametrised over `list(JobState)` | `uv run pytest tests/test_web_state_rendering.py -q` | ✅ **already written — passes automatically** |
| DPLX-06 | Abort at the prompt ⇒ job ends `ERROR` with a message naming the flip prompt | unit | `uv run pytest tests/test_worker.py -q` | ✅ |
| DPLX-06 | **Grep gate:** `wait_transition` / `_transition_event` appear nowhere | static | `grep -rn "wait_transition\|_transition_event" src/ tests/` → empty | ✅ |
| DPLX-06 | Nine `JobState` members, each with a label ≠ its raw value | unit, parametrised | `uv run pytest tests/test_vocabulary.py -q` | ✅ |
| DPLX-07 | Write-then-load round trip yields a `default` profile for **flatbed-only**, **feeder-only** and **mixed** devices | unit, parametrised | `uv run pytest tests/test_auto_profiles.py -q` | ✅ — one of three exists (`:548`) |
| DPLX-07 | `auto-profiles` emits `duplex = "hardware"` only for `FEEDER_DUPLEX`, and omits the key otherwise | unit | `uv run pytest tests/test_auto_profiles.py -q` | ✅ |

### How each roadmap success criterion is sampled

| # | Criterion | Observable | Cost |
|---|-----------|-----------|------|
| 1 | Legacy config loads, scans, translates, warns; `source` never inspected for strategy | `caplog` record + the existing `_DUPLEX_SOURCE` e2e case + a grep gate | ~0 |
| 2 | CLI prompts and completes two passes; no-coordinator start refused before the scanner opens | `result.output` ordering (confirm **echoes** — measured) + `get_devices.assert_not_called()` | ~0 |
| 3 | Flip timeout fails the job clearly and releases the scanner | `flip_timeout_seconds = 0` (**4 µs**, `_TIMEOUT_BUDGET` precedent) + a second job reaching terminal | ~0 |
| 4 | `SCANNING_REVERSE` reported; Abort cancels; `wait_transition` gone | blocking-stub worker test + the **already-written** parametrised rendering tests + a grep gate | < 1 s |
| 5 | auto-profiles always emits `default` | three-way parametrised write→`load_settings` round trip | ~0 |

**The three hard observation problems, and their answers:**

- **A real ten-minute wait would blow the 60 s SIGALRM.** Answer: never wait. `flip_timeout_seconds`
  is an `int` config field, `0` is valid, and `Event.wait(0)` was measured at 4 µs. The identical
  technique is already documented at `test_outcomes_e2e.py:104-118` for `paperless_task_timeout`,
  including why `0` and not `0.05`.
- **`CliRunner` is not a TTY, so the non-TTY refusal and the prompt test collide.** Answer: the
  `_stdin_is_interactive()` seam. The refusal test asserts the *unpatched* behaviour — which is the
  honest one, because `CliRunner` genuinely is not a terminal — and the prompt test patches the
  seam. Both halves of D-11 become testable, and neither test lies.
- **Two passes against a fake SANE device.** Answer: already solved.
  `tests/test_pipeline.py:806-856` drives the real `SaneBackend` over `FakeSaneDev`, reloading the
  feeder from inside the status callback, and asserts `dev.assignments.count("source") == 2` and
  `dev.calls.count("snap") == 6`. It needs two changes: the coordinator replaces its bare
  `threading.Event`, and `report_sources` must carry a **realistic** feeder name.

### Sampling Rate

- **Per task commit:** the module(s) the task touched, e.g.
  `uv run pytest tests/test_pipeline.py -q` — a few seconds against a 34 s full suite.
- **Per wave merge:** `uv run pytest -q` (measured 34.18 s) plus `uv run ruff check .` and
  `uv run ty check src`.
- **Phase gate:** full suite green **plus** all four static gates —
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
  `uv run pyrefly check src tests` (paths named, never bare — 23.1 D-10) — before
  `/gsd-verify-work`.
- **Max feedback latency:** ~35 s.

⚠️ **No git hook runs pytest** (Finding 15). The sampling above is a discipline the plan must
encode in task verification steps; nothing will enforce it automatically.

### Wave 0 Gaps

- [ ] `src/saneless/cli.py` — `_stdin_is_interactive()` seam. **Production code, and it is a test
      enabler**: without it DPLX-04's prompt test cannot be written at all.
- [ ] `tests/conftest.py` — a concrete always-continue `FlipCoordinator` stub. Required by ~10
      migrated pipeline tests in Wave C; must land in the same commit as the refusal guard.
- [ ] `tests/test_worker.py` — a scanner stub whose **second** `scan_pages` blocks on a test-held
      `Event`, so `SCANNING_REVERSE` is observable rather than instantaneous.
- [ ] `tests/test_outcomes_e2e.py` — `_Case` gains a `flip_timeout: int` field and a
      `flip-timeout` case; `_build_settings` passes it through to `OutputConfig`.
- [ ] `src/saneless/config.py` — a module logger (`logging.getLogger(__name__)`); none exists
      today, and D-04's `caplog` assertion needs `logger="saneless.config"`.
- [ ] `tests/test_pipeline.py:815` — retarget `report_sources` from `["Flatbed", "ADF Manual
      Duplex"]` to a realistic list. Without this the integration test still asserts the fiction.

*No new test files and no framework install are needed. Every target module exists.*

---

## Security Domain

`security_enforcement` is absent from `.planning/config.json`, so it is treated as enabled.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | saneless has no user accounts; the trusted-LAN posture is a recorded product decision (`REQUIREMENTS.md:218`) |
| V3 Session Management | no | No sessions. Job identity is a UUID in a URL-free POST body |
| V4 Access Control | **yes, but deferred** | `POST /api/flip/continue` and `/api/flip/abort` are unauthenticated and have no cross-site protection. **N-22/ROBU-10 (Phase 26) owns this.** D-17 rewrites these two handlers, so the planner must resist "while we're here" — and the verifier must not fail the phase for its absence |
| V5 Input Validation | **yes** | `duplex: Literal["none","hardware","manual"]` — pydantic rejects anything else. `flip_timeout_seconds: int` likewise. No hand-rolled parsing |
| V6 Cryptography | no | Nothing in this phase touches secrets. `ProfileConfig` carries no credential, so D-04's warning — which interpolates a profile name and a source string — cannot leak one |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Unbounded wait on a human parks the single worker thread; ten queued jobs then hang the server | **Denial of Service** | `flip_timeout_seconds = 600` enforced in `wait_for_flip` (D-10, M-07). **This phase is itself a DoS mitigation** |
| Unbounded feeder loop re-scanning a platen | Denial of Service | Already mitigated: `_MAX_ADF_PAGES = 500`, **per `scan_pages` call, therefore per pass** (Phase 24 D-04) — say so wherever duplex is written down |
| Log injection via a profile name or source string in the deprecation warning | Tampering (log integrity) | Use `%`-style lazy args (`logger.warning("...%r...", name, source)`), never an f-string. Ruff's `G` rules are enabled and enforce this |
| A cross-site POST aborts a stranger's in-progress scan | Tampering | `Sec-Fetch-Site` rejection — **ROBU-10, Phase 26.** Recorded here as a known, accepted, deferred exposure |
| A config value chosen to make the timeout ineffective (`flip_timeout_seconds = 0`) | Denial of Service (self-inflicted) | Accepted: the config file is operator-owned and already able to break the install in a dozen ways. `0` is deliberately *useful* — it is the test seam |
| Unreadable/oversized page data reaching the PDF assembler | Tampering | Already mitigated upstream: `_validate_page_image` + `_MIN_PAGE_BYTES` (Phase 24) |

**Net security effect of this phase: positive.** DPLX-05 closes a real availability defect (M-07)
that today lets one forgotten browser tab hang the appliance until it is restarted.

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Preferring the profile's `source` when the device reports it *and* it feeds, falling back to device resolution otherwise, is the right reading of D-01 + D-02 together | Pattern 2 | If the intended reading is "ignore `source` entirely when `duplex == "manual"`", the `source = "ADF"` that D-18 teaches becomes decorative and a user who deliberately picks one of two feeders gets the other. **Cheap to flip** — one branch in `_resolve_source`. See Open Question 2 |
| A2 | A device exposing **no** `source` option at all should refuse manual duplex rather than proceed | Pattern 2 | Proceeding would take two platen snapshots on a fixed-flatbed device — C-01's exact failure. Refusing is the conservative branch the CONTEXT's own §Specific-Ideas reading recommends ("be cautious with the operator's paper"), but the CONTEXT does not name this case |
| A3 | The CLI coordinator should catch `click.Abort` (Ctrl-C / EOF) and return `FlipOutcome.ABORTED` rather than letting it exit 1 | Code Example 5 | If it propagates, Ctrl-C at the prompt and web-Abort produce different job outcomes for the same user intent, which fragments what Phase 28's EXC-04 will later have to classify |
| A4 | `ScanSettings` should carry a narrow `resolve_feeder_source: bool`, not a scanner-side `DuplexMode` enum | Open Question 3 | A boolean is against the codebase's strong enum preference; an enum is against D-05's "no member without a reader" (`NONE` and `HARDWARE` would behave identically inside the scanner). Genuinely contested — see Open Question 3 |
| A5 | `docs/PRD.md` is a historical record of what was asked for, not a claim about current behaviour, and is therefore out of this phase's "correct what you changed" scope | Documentation Scope | If the PRD is treated as live documentation, two more sentences (`:110`, `:116`) need rewriting. Low cost either way |
| A6 | D-04's warning should make no removal promise ("will stop working in a future release") | Code Example 2 | A promise no decision backs would either constrain a future phase or become a documented lie. Stated conservatively; the user may prefer the stronger wording |
| A7 | Chromium is available for `@pytest.mark.browser`; the measured `966 passed` run included or cleanly skipped them | Environment Availability | If browser tests were silently skipped, the "full suite" baseline is optimistic. Re-measure with `-m "not browser"` before pinning commands |

---

## Open Questions

1. **The CLI cannot honour `wait_for_flip`'s timeout.**
   - *What we know:* D-09 fixes the signature as `wait_for_flip(timeout: float) -> FlipOutcome`.
     `click.confirm` blocks on stdin with no timeout facility, and implementing one needs `select`
     on stdin or a thread.
   - *What's unclear:* whether DPLX-05's "a flip wait that exceeds the timeout fails the job"
     is meant to bind the CLI at all. M-07's actual complaint is the *web* worker thread being
     parked behind a queue; the CLI has no queue behind it and the operator can press Ctrl-C.
   - *Recommendation:* the CLI coordinator **accepts and documents-away** the timeout, naming the
     parameter `_timeout` (ruff's `ARG002` honours `dummy-variable-rgx`, whose configured pattern
     `^(_+|(_+[a-zA-Z0-9_]*[a-zA-Z0-9]+?))$` matches it; in-tree precedent: `_noop_callback`).
     Criterion 3 is then satisfied through the worker coordinator. **Flag this to the user** — it
     is a narrowing of DPLX-05's plain reading, and the roadmap criterion may want amending
     alongside criterion 2, which the CONTEXT already flags for amendment.

2. **Does `source` still matter when `duplex == "manual"`?** (A1)
   - *What we know:* D-01 says `source` is passed verbatim; D-02 says the device source is resolved
     from the device's list; D-18 teaches writing `source = "ADF"`.
   - *What's unclear:* whether "resolved" means "instead of `source`" or "when `source` will not do".
   - *Recommendation:* prefer `source` when the device reports it and it feeds (Pattern 2). Record
     the reasoning in the plan. One branch either way.

3. **`ScanSettings.resolve_feeder_source: bool` vs a scanner-side `DuplexMode` enum.** (A4)
   - *What we know:* Phase 21 D-03 forbids the scanner package importing job vocabulary (verified
     intact — Finding 14). The codebase strongly prefers total enums. D-05 forbids shipping a value
     with no reader.
   - *What's unclear:* a scanner-side `DuplexMode` would have `NONE` and `HARDWARE` behaving
     *identically* inside the scanner — two members, one behaviour — which is precisely the
     "member with no reader" D-05 argues about at the config layer. A boolean named
     `resolve_feeder_source` says what the backend actually does and creates no second spelling of
     "duplex".
   - *A third option worth naming:* type `ProfileConfig.duplex` as a lowercase-valued `StrEnum`
     defined in `scanner/base.py`, giving **zero** conversion points. `config.py` importing
     `scanner.base` is acyclic (verified). **But D-01 locks the field as a `Literal`**, and
     `ConnectionStatus` is currently the only documented lowercase-value exception. Raised, not
     adopted — a locked decision says `Literal`.
   - *Recommendation:* `resolve_feeder_source: bool`, with the reasoning recorded. It keeps D-01's
     `Literal` as the single spelling of "duplex" in the codebase.

4. **Is `docs/PRD.md` in scope?** (A5) Recommendation: no — but say so in the plan so the verifier
   does not read it as an omission.

5. **Should D-17's extraction cover `index` as well?** (Finding 12) `routes.py:81-87` is a fourth
   copy. Recommendation: yes — leaving it behind preserves the duplication D-17 exists to remove.
   Raised because it widens a locked decision's stated scope.

---

## Sources

### Primary (HIGH confidence)

- **This repository, read directly:** `src/saneless/{config,pipeline,worker,vocabulary,cli,job,auto_profiles}.py`,
  `src/saneless/scanner/{base,sane_backend}.py`, `src/saneless/web/{app,routes}.py`,
  `src/saneless/web/templates/{index.html,partials/status.html,partials/flip.html}`,
  `tests/{conftest,fake_sane,test_pipeline,test_worker,test_vocabulary,test_web,test_web_state_rendering,test_cli,test_config,test_auto_profiles,test_outcomes_e2e,test_browser,test_scanner,test_job}.py`,
  `pyproject.toml`, `.pre-commit-config.yaml`, `docs/**`, `.planning/{REQUIREMENTS,ROADMAP,STATE,UI-SPEC}.md`,
  `.planning/reviews/2026-09-09-code-review.md` §§ C-01, C-02, M-02, M-07, N-07, N-08, 8, 10.
- **Executed this session:**
  - `CliRunner` + `click.confirm` + `click.pause` behaviour probe (Finding 4, Code Example 5)
  - pydantic before-validator translation probe (Finding 1, Code Example 1)
  - `uv run ruff check --select PLR0913` argument-limit probe (Finding 3)
  - `threading.Event().wait(0)` timing probe (Finding 6)
  - `uv run pytest -q` → **966 passed in 34.18s**
  - version probes for click / pydantic / pydantic-settings / pytest
  - `grep -rn "vocabulary\|JobState\|ScanOutcome" src/saneless/scanner/` → empty (Finding 14)
- **Context7 — `/websites/click_palletsprojects_en_stable`:** `click.confirm` signature and `Abort`
  semantics; `CliRunner` input-stream testing; `click.testing.Result` fields.
- **Context7 — `/pydantic/pydantic`:** `model_validator(mode='before')` — receives raw input as
  `Any`, runs before instantiation, and the documented caution against mutating the input in place.

### Secondary (MEDIUM confidence)

- `.planning/phases/{20,21,23,23.1,24}-*/\*-CONTEXT.md` — prior-phase decisions this phase inherits
  (D-03's import boundary, D-08's `match`/`assert_never` measurement, D-09's parametrise-over-enum
  rule, D-04's per-call `_MAX_ADF_PAGES` scope, the commit-gate split). Treated as MEDIUM only
  because they are secondhand records of earlier measurements; the ones this phase depends on
  (import boundary, PLR0913, schema DDL, warning filter) were **re-verified directly** this session.
- `.planning/phases/24-scanner-truthfulness/24-VALIDATION.md` — the shape the orchestrator expects
  from the Validation Architecture section.

### Tertiary (LOW confidence)

- None. Every claim above is either executed, read from this tree, or cited to vendor documentation.
  No WebSearch was used, and no claim rests on training-data recall — which is why the Assumptions
  Log contains judgement calls (A1–A7) rather than unverified facts.

---

## Metadata

**Confidence breakdown:**

- **Standard stack: HIGH** — nothing is installed; every version was read from the running
  environment and every behavioural claim about click and pydantic was executed, not recalled.
- **Architecture: HIGH** — every integration point was read in full. The two responsibility traps
  named in the map (feeder resolution in the pipeline; the refusal inside `_scan_manual_duplex`)
  are backed by code I read (`run_pipeline` never fetches capabilities; `_scan_manual_duplex` scans
  pass A first).
- **Pitfalls: HIGH** — Pitfalls 1, 2, 4, 5 and 7 were each derived from files read in this session,
  and Pitfall 2's limit was executed. Pitfall 6 was read from `.pre-commit-config.yaml`.
- **Validation: HIGH** — the three hard observation problems were each solved with a *measured*
  technique (4 µs timeout, echoed confirm prompt, existing fake-SANE seam) rather than a proposed one.
- **Discretion recommendations: MEDIUM** — the four judgement calls (A1–A4) are reasoned from
  locked decisions that pull in slightly different directions. Each is a one-branch change and each
  is flagged as an Open Question rather than presented as settled.
- **Documentation scope: HIGH** — every listed line was read. Six rows are *new* findings the
  CONTEXT does not list, and the UI-SPEC "Nine presentations" re-check the CONTEXT explicitly asked
  for was performed: the summary already disagrees with its own table, which omits `AWAITING_FLIP`.

**Research date:** 2026-09-13
**Valid until:** 2026-10-13 (30 days). This is an internal-refactor phase against pinned
dependencies; the findings do not decay with the ecosystem. They decay only if the tree changes —
specifically, if `_scan_manual_duplex`'s or `_handle_duplex_mismatch`'s argument counts change, if
a pytest hook is added, or if `tests/fake_sane.py`'s `_DEFAULT_SOURCES` gains a plain `"ADF"`.
