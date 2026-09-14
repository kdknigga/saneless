# Phase 25: Manual Duplex - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-09-13
**Phase:** 25-manual-duplex
**Areas discussed:** Duplex field + legacy translation, FlipCoordinator contract + timeout, Pass-B visibility, Abort semantics + Phase 28 boundary, Additional gray areas (round 2)

---

## Duplex field + legacy translation

### Q1 — What does `source` become for a legacy `"Manual Duplex"` profile?

| Option | Description | Selected |
|--------|-------------|----------|
| Resolve from the device | Translate `duplex` at load; backend picks the device's own feeder source at scan time via `classify_source` | ✓ |
| Hardcode `"ADF"` | The review's literal suggestion; one place, but fails on "Automatic Document Feeder" | |
| Keep source, fail loudly | Most honest, but conflicts with success criterion 1's "still loads and scans" | |
| You decide | | |

**User's choice:** Resolve from the device
**Notes:** Chosen knowing it splits the translation across two moments (load sets `duplex`, scan resolves `source`). Rationale: Phase 24 established that real feeders are named "Automatic Document Feeder", so a hardcoded `"ADF"` fails on exactly the consumer hardware manual duplex exists for.

### Q2 — What does `ScanSettings` carry to trigger feeder resolution?

| Option | Description | Selected |
|--------|-------------|----------|
| `DuplexMode` enum in `scanner/base.py` | Typed, total, satisfies Phase 21 D-03; costs double-spelling | |
| Empty-string sentinel | No new field; revives the stringly-typed protocol Phase 21's N-38 removed | |
| `prefer_feeder` boolean | Minimal, no duplex vocabulary in `scanner/`; cannot express "hardware" later | |
| You decide | | ✓ |

**User's choice:** You decide
**Notes:** Recorded as Claude's discretion with the D-03 constraint (scanner package must not import job vocabulary) and the single-conversion-point requirement attached.

### Q3 — Where does the legacy translation live, and can the warning name the profile?

| Option | Description | Selected |
|--------|-------------|----------|
| `ProfileConfig` before-validator | Covers every construction path; cannot name the profile | |
| `Settings` profiles validator | Can name the profile; misses directly-constructed `ProfileConfig` | |
| Both seams, split roles | Before-validator translates; profiles validator emits the named warning | ✓ |
| You decide | | |

**User's choice:** Both seams, split roles
**Notes:** Accepted the cost of spreading the deprecation concern across two validators in two classes, in exchange for full coverage (~219 duplex references in the suite construct `ProfileConfig` directly) plus named diagnostics.

### Q4 — What replaces the `isinstance` dispatch?

| Option | Description | Selected |
|--------|-------------|----------|
| Total `match` with `assert_never` | House pattern; enforced by both `ty` and `pyrefly` per Phase 21 D-08 | ✓ |
| Raise `DuplexMismatchError` | N-07's literal wording; models a successful recovery as a failure | |
| One result record, optional mismatch | A union in disguise; neither checker forces the branch | |
| You decide | | |

**User's choice:** Total `match` with `assert_never`
**Notes:** Noted during discussion that half of N-07 is already done — the `(fronts, backs)` tuple became `_DuplexMismatch` in an earlier phase — so a comment must record that this dispatch satisfies the finding.

### Q5 — Does the duplex-mismatch branch's empty-page bypass get fixed?

| Option | Description | Selected |
|--------|-------------|----------|
| Leave the bypass, document it | A blank back is evidence about the mismatch; `pages_removed=0` becomes literally true | ✓ |
| Filter safely, never raise | Honours the toggle everywhere; needs a non-raising variant and hits EXC-03's territory | |
| Filter as-is, accept the raise | Smallest change; an all-blank backs pass would destroy the fronts | |
| You decide | | |

**User's choice:** Leave the bypass, document it
**Notes:** Surfaced by Claude during the area close rather than from any requirement. `_drop_empty_pages` raises `ScanError` when every page is empty, which is what made the naive fix dangerous.

---

## FlipCoordinator contract + timeout

### Q1 — What is the coordinator's contract?

| Option | Description | Selected |
|--------|-------------|----------|
| ABC returning a `FlipOutcome` enum | Matches the ScannerBackend/WR-08 precedent; one atomic answer | ✓ |
| ABC returning `bool` | Smallest surface; reintroduces the wake-then-ask-why two-step M-02 blames | |
| `Protocol` returning `FlipOutcome` | Lightest coupling; cuts against the house rule established by grep | |
| You decide | | |

**User's choice:** ABC returning a `FlipOutcome` enum
**Notes:** The ABC-vs-Protocol rule was derived from the tree during discussion: `Protocol` describes shapes the project does not own (`SaneDevice`, `_SettingsFactory`); `ABC` defines seams it implements (`ScannerBackend`).

### Q2 — Where does the flip timeout live, and what is its default?

| Option | Description | Selected |
|--------|-------------|----------|
| `OutputConfig` key, 600s default | M-07's literal prescription; the section where every timeout lives | ✓ |
| Module constant + default arg | Consistent with `_DEFAULT_PAGE_TIMEOUT_SECONDS`; not operator-tunable | |
| `ProfileConfig` field | Most precise scoping; drags CFG-07 merge semantics into this phase | |
| You decide | | |

**User's choice:** `OutputConfig` key, 600s default
**Notes:** Chosen knowing it diverges from the codebase's own post-Phase-24 precedent for scan-side timeouts. Deciding argument: this is the only timeout that waits on a human rather than a machine.

### Q3 — Where does the "no coordinator" refusal live?

| Option | Description | Selected |
|--------|-------------|----------|
| Top of `run_pipeline`, before `_resolve_device` | Only placement that precedes SANE contact | |
| In `_scan_manual_duplex` at point of use | A trap — pass A is already scanned by then | |
| In both entry points | The duplication shape C-02 blames | |
| You decide | | ✓ |

**User's choice:** You decide
**Notes:** Recorded as discretion with the hard constraint that the refusal must precede any SANE contact, and an explicit warning that the point-of-use placement burns a feeder pass.

### Q4 — What does the CLI prompt use off a TTY?

| Option | Description | Selected |
|--------|-------------|----------|
| `confirm()`, refuse up front on non-TTY | Safe, and says why before paper is fed | ✓ |
| `confirm()` only, let `Abort` handle it | Less code; the failure arrives after pass A | |
| `pause()` to match the roadmap wording | Disqualified — documented no-op off a terminal, reintroducing C-02 | |
| You decide | | |

**User's choice:** `confirm()`, and refuse up front on non-TTY
**Notes:** Verified against click's documentation during discussion. This choice requires amending roadmap success criterion 2, whose "press Enter" wording describes the unsafe API.

---

## Pass-B visibility

### Q1 — A ninth `JobState`, or reuse of `SCANNING`?

| Option | Description | Selected |
|--------|-------------|----------|
| New `JobState.SCANNING_REVERSE` | Named by DPLX-06; lets the operator tell fronts from backs | ✓ |
| Map to `JobState.SCANNING` | M-02's first suggestion; zero churn, loses the distinction | |
| New state named `SCANNING_BACKS` | M-02's wording; splits vocabulary across two spellings | |
| You decide | | |

**User's choice:** New `JobState.SCANNING_REVERSE`
**Notes:** Confirmed during discussion that the jobs table has no `CHECK` constraint, so no migration is required.

### Q2 — What do the forced label arms say?

| Option | Description | Selected |
|--------|-------------|----------|
| "Scanning backs" / "Scanning reverse sides..." | Progress string byte-identical to `cli.py:131`'s existing output | ✓ |
| "Scanning reverse" / "Scanning reverse sides..." | Matches the enum name; reads as clipped jargon in the table | |
| "Scanning back sides" / "Scanning the back sides..." | Plainest English; changes shipped CLI output | |
| You decide | | |

**User's choice:** "Scanning backs" / "Scanning reverse sides..."

### Q3 — Does the whole `_transition_event` protocol go?

| Option | Description | Selected |
|--------|-------------|----------|
| Delete the entire protocol | `routes.py:297` is the only reader; the rest becomes write-only state | ✓ |
| Delete only the public method | Smallest diff; leaves an event with zero readers | |
| Replace with a state-polling helper | Pulls Phase 26's ROBU-03 work forward | |
| You decide | | |

**User's choice:** Delete the entire protocol
**Notes:** `tests/test_worker.py:758`'s own docstring documents the stale-signal flaw, so the existing tests pin the defect rather than guard against it.

---

## Abort semantics + Phase 28 boundary

### Q1 — Does this phase introduce cancelled vocabulary?

| Option | Description | Selected |
|--------|-------------|----------|
| Message only, leave vocabulary to Phase 28 | Phase 25 makes abort work; Phase 28 makes it classified | ✓ |
| Add `ScanCancelledError` now | Splits EXC-04 across two phases | |
| Full `CANCELLED` JobState now | A tenth member in the phase that adds the ninth; takes EXC-04's decision | |
| You decide | | |

**User's choice:** Message only — leave vocabulary to Phase 28

### Q2 — What happens to an Abort clicked in the stale-frame window?

| Option | Description | Selected |
|--------|-------------|----------|
| Coordinator answers once; later aborts ignored | Race-free by construction; mid-pass abort is Phase 29's | ✓ |
| Continue returns a non-flip partial unconditionally | Closes the window; asserts a state the store has not recorded | |
| Keep a short bounded wait | `wait_transition` renamed; DPLX-06 forbids it | |
| You decide | | |

**User's choice:** Coordinator answers once; later aborts ignored
**Notes:** Established during discussion that the Continue/Abort controls disappear on their own once `SCANNING_REVERSE` is a distinct state, because `status.html` binds the flip partial to `AWAITING_FLIP` only.

### Q3 — Fix the stale `current_job_id` lookup?

| Option | Description | Selected |
|--------|-------------|----------|
| Share one lookup helper across all three routes | M-02's prescription; ~3 lines, both routes already being rewritten | ✓ |
| Fix `abort_flip` only | Leaves `continue_flip` with the same latent bug | |
| Leave it to Phase 26 | No Phase 26 requirement covers it | |
| You decide | | |

**User's choice:** Share one lookup helper across all three routes

---

## Additional gray areas (round 2)

### Q1 — Does the `duplex = "hardware"` member ship?

| Option | Description | Selected |
|--------|-------------|----------|
| Ship all three, document hardware as declarative | Keeps DPLX-01 intact; APPL-05 is the eventual reader | ✓ |
| Ship `none \| manual` only | Every member gets a reader; amends DPLX-01 | |
| Ship all three and make hardware assert | Gives it a reader; sets a cross-field-validation precedent | |
| You decide | | |

**User's choice:** Ship all three, document hardware as declarative
**Notes:** Raised because a grep showed `FEEDER_DUPLEX` appears only inside its own enum definition — nothing branches on it, so `"hardware"` would have no reader.

### Q2 — Manual duplex on a device with no feeder source?

| Option | Description | Selected |
|--------|-------------|----------|
| Raise before pass A, naming the device's sources | Reuses the existing message shape; wastes nothing | |
| Fall back to `Auto` as `_resolve_source` does today | Reproduces C-01's exact failure | |
| Refuse up front via `get_capabilities` | Earliest error; costs the extra round trip Area 1 declined | |
| You decide | | ✓ |

**User's choice:** You decide
**Notes:** Recorded as discretion with the hard constraint that the `Auto` substitution must not be reachable for manual duplex.

### Q3 — Does `auto-profiles` emit a `duplex` key?

| Option | Description | Selected |
|--------|-------------|----------|
| Emit "hardware" for duplex feeders only | Follows Phase 16's non-default-only precedent | ✓ |
| Never emit the key | Smallest diff; a duplex feeder's profile would claim `none` | |
| Always emit explicitly, including `none` | Most self-documenting; contradicts Phase 16 | |

**User's choice:** Emit "hardware" for duplex feeders only

### Q4 — Does the how-to keep documenting the legacy form?

| Option | Description | Selected |
|--------|-------------|----------|
| Teach `duplex` only, with one deprecation note | Recommended; gives the warning a place to point | |
| Remove the legacy form entirely | Cleanest teaching surface; nothing invites new use | ✓ |
| Present both as supported alternatives | Teaches the form C-01 exists to retire | |

**User's choice:** Remove the legacy form entirely
**Notes:** **Diverges from the recommendation.** Accepted consequence, recorded as a binding constraint in CONTEXT.md D-18: since the deprecation warning still fires but no page mentions the key, the warning text becomes the only migration instruction and must name the replacement inline.

---

## Claude's Discretion

- What `ScanSettings` carries to trigger feeder resolution (Area 1 Q2) — constrained by Phase 21 D-03 and a single-conversion-point requirement.
- Where the "manual duplex without a coordinator" refusal fires (Area 2 Q3) — must precede any SANE contact; not inside `_scan_manual_duplex`.
- What happens when a manual-duplex profile meets a device with no feeder source (Round 2 Q2) — the `Auto` fallback must not be reachable.
- Where `FlipOutcome` lives (`vocabulary.py` is the leaf every consumer imports).
- Which doc sentences get rewritten beyond those named in CONTEXT.md.

## Deferred Ideas

No out-of-scope features were proposed during discussion. Boundary clarifications — work this phase deliberately does not do — are recorded in CONTEXT.md's `<deferred>` section: cancelled vocabulary (Phase 28), mid-pass abort (Phase 29), the `wait_for_state` helper (Phase 26), owner-only flip prompt and page counts (Phase 30), `extra="forbid"` coverage and `--force` merge semantics (Phase 27), and route conversion plus 429 backpressure (Phase 26).

## Criteria amendments raised

- **Roadmap Phase 25, success criterion 2** — its `prompts "Flip the stack and press Enter" on stdin` wording describes `click.pause`, which is a documented no-op off a terminal and would reintroduce C-02. The criterion should describe a prompt that blocks until the operator answers.
