---
phase: 30-appliance-layer
plan: 05
subsystem: config
tags: [auto-profiles, tomlkit, pydantic, sane, scan-profiles, classify-source]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "plan 30-02's ProfileConfig.label / .description fields and their PROFILE_LABEL_MAX_LENGTH / PROFILE_DESCRIPTION_MAX_LENGTH caps"
  - phase: 24-source-classification
    provides: "classify_source and SourceKind -- the one source-classification rule the new text is derived from"
  - phase: 27-profile-ownership
    provides: "_OWNED_KEYS, _generated_values and _merge_profile's set/delete refresh loop (D-01..D-03)"
provides:
  - "_profile_label(source) -- the three D-19 label forms plus an Automatic and a Scanner source fallback"
  - "_profile_description(source) -- one plain sentence per SourceKind"
  - "label and description as the first two _OWNED_KEYS, emitted unconditionally by _generated_values"
  - "generate_profiles sets both fields on every profile it builds, the default included"
  - "how-to documentation of the two keys, the --force overwrite rule, the auto_generated escape hatch and the A-3 backfill"
affects: [30-06, 30-07, appliance-layer-scan-form, profile-dropdown]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "match + assert_never over SourceKind for every new label/message function"
    - "unconditional emission in _generated_values as the way to make Phase 27 D-03's delete branch unreachable for a free-text owned key"
    - "doc-truth test: read the how-to from disk and assert every _OWNED_KEYS entry appears in it, so prose cannot drift from the tuple"

key-files:
  created: []
  modified:
    - src/saneless/auto_profiles.py
    - tests/test_auto_profiles.py
    - docs/how-to/configure-scan-profiles.md

key-decisions:
  - "label and description lead _OWNED_KEYS so the tuple's file key order puts the human name ahead of the SANE source string"
  - "Both keys are emitted unconditionally, unlike auto_source_mode and duplex, which makes Phase 27 D-03's delete branch unreachable for them"
  - "_generated_values derives both from profile.source rather than copying profile.label, so a refresh corrects stored text that no longer matches its source"
  - "The T-30-17 interpolation test asserts on an injected vendor marker, not on the whole source name: 'Automatic' contains the source name 'Auto' by coincidence"

patterns-established:
  - "Pattern: a derivation helper is a pure function of the source name that calls classify_source once and matches over SourceKind with assert_never -- never re-deriving from the string"
  - "Pattern: an owned key holding free text must be emitted unconditionally, and the consequence (the key survives a refresh) is asserted rather than the cause"
  - "Pattern: a verbatim list in prose documentation is kept honest by a test that reads the file and iterates the source-of-truth tuple"

requirements-completed: [APPL-05]

# Metrics
duration: 8min
completed: 2026-09-16
---

# Phase 30 Plan 05: Generated Profile Label and Description Summary

**`saneless auto-profiles` now writes a human profile name and a one-sentence explanation as tool-owned keys, both derived from `classify_source` with no new device probe and no new config key.**

## Performance

- **Duration:** 8 min
- **Started:** 2026-09-16T11:46:34-05:00
- **Completed:** 2026-09-16T11:54:36-05:00
- **Tasks:** 2 (4 commits — TDD RED/GREEN per task)
- **Files modified:** 3

## Accomplishments

- `_profile_label` and `_profile_description` produce the three D-19 forms verbatim (`Feeder, single-sided`, `Feeder, double-sided`, `Glass (flatbed)`) plus a non-empty fallback for `AUTO` and `UNKNOWN`, so no profile is ever offered under a blank name.
- Both are pure functions of the source name that call `classify_source` once and `match` over `SourceKind` with `assert_never` — the mandatory shape `SourceKind.uses_feeder` itself uses. Neither re-derives the classification from the string, which `classify_source`'s docstring forbids.
- Every returned string is a developer-authored constant. The SANE source name is never interpolated, closing T-30-17: a vendor-chosen source string cannot reach the scan page through the config file this writes.
- `label` and `description` are now the first two `_OWNED_KEYS` and are emitted unconditionally by `_generated_values`, so `--force` overwrites them in place exactly like `source`/`mode`/`resolution` and a refresh can never silently prune them.
- `_merge_profile` and `write_profiles_to_config` are unchanged — extending the tuple was the whole change, proven by test rather than asserted in prose.
- The how-to guide documents both keys, the overwrite rule, the `auto_generated` escape hatch and the Amendment A-3 backfill, and a doc-truth test keeps its verbatim owned-key list from drifting from `_OWNED_KEYS`.

## Task Commits

1. **Task 1: `_profile_label` and `_profile_description`, derived not probed**
   - `25de735` (test — RED gate)
   - `0093428` (feat — GREEN gate)
2. **Task 2: label and description as owned keys, and the how-to that explains ownership**
   - `f1e0dd5` (test — RED gate)
   - `6299c71` (feat — GREEN gate)

**Plan metadata:** committed with this SUMMARY.

## Files Created/Modified

- `src/saneless/auto_profiles.py` — added `_profile_label` / `_profile_description` beside `_duplex`; wired `label=` / `description=` into both `ProfileConfig` constructions in `generate_profiles`; put `"label"`/`"description"` at the head of `_OWNED_KEYS` with a comment explaining why they lead; made `_generated_values` emit both unconditionally at the top of the dict; updated `_duplex`'s docstring, which no longer calls APPL-05 its "eventual" reader.
- `tests/test_auto_profiles.py` — `TestProfileLabels` (label/description derivation, bounds, distinctness, non-interpolation) and `TestLabelAndDescriptionAreOwnedKeys` (tuple order, unconditional emission, `--force` overwrite, unflagged profiles untouched, delete branch unreachable, reload round-trip) plus `TestScanProfileHowToDocumentsOwnership` (doc truth). 216 tests in this file, up from 172.
- `docs/how-to/configure-scan-profiles.md` — two new field-table rows, the extended owned-key list, and two paragraphs stating the overwrite rule, the escape hatch and the `--force` backfill.

## Decisions Made

- **`_generated_values` derives both keys from `profile.source` rather than copying `profile.label`.** The plan specified this and it earns its keep: a refresh corrects a profile whose stored text no longer matches the source it carries. Documented in the code comment.
- **Test parametrisation runs over `list(SourceKind)`, not over a local mapping's keys.** A new enum member fails with a `KeyError` in the sample lookup rather than being silently skipped. A companion test asserts the sample-name mapping is itself correct.
- **The T-30-17 non-interpolation assertion uses an injected vendor marker.** See deviation 1 — asserting `source not in _profile_label(source)` is a stricter claim than the property being tested and produces a false positive.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Bug] The RED test for T-30-17 asserted a property stricter than the one it meant to test**

- **Found during:** Task 1 (GREEN gate)
- **Issue:** The test asserted `source not in _profile_label(source)` for every sample source. With the plan-specified label `"Automatic"` for `SourceKind.AUTO`, the source name `"Auto"` is a substring of the returned constant by pure coincidence. The test failed even though the implementation interpolates nothing — a false positive that would have forced a wrong change to locked D-19-adjacent copy.
- **Fix:** Rewrote the test to append a distinctive vendor marker (`ZzVendorZz<script>alert(1)</script>`) to a name of each classified shape and assert the marker — and any `<` — never survives into either return value. Added a companion test proving the marked names still reach four distinct classifier branches rather than all collapsing to `UNKNOWN`, so the interpolation test cannot silently cover one arm while claiming five.
- **Files modified:** `tests/test_auto_profiles.py`
- **Verification:** `uv run pytest tests/test_auto_profiles.py -k label -q` → 30 passed; the marker assertion fails if either function is changed to build a string.
- **Committed in:** `0093428` (Task 1 GREEN commit), explained in the commit body.

**2. [Rule 3 — Blocking] `pathlib.Path` was a `TYPE_CHECKING`-only import**

- **Found during:** Task 2 (RED gate)
- **Issue:** The plan requires the doc-truth test to read the how-to guide from disk rather than hard-code a line number. `tests/test_auto_profiles.py` imported `Path` only under `if TYPE_CHECKING:`, so the test raised `NameError` at runtime.
- **Fix:** Moved `from pathlib import Path` to the runtime imports and removed the now-empty `TYPE_CHECKING` block and its `TYPE_CHECKING` import. Ruff's own `--fix` reordered the import block.
- **Files modified:** `tests/test_auto_profiles.py`
- **Verification:** `uv run ruff check .` and `uv run pytest -m "not browser and not sane_hardware" -q` both clean.
- **Committed in:** `f1e0dd5` (Task 2 RED commit).

**3. [Rule 1 — Bug] The how-to paragraphs were inserted mid-bullet-list**

- **Found during:** Task 2 (GREEN gate)
- **Issue:** The escape-hatch paragraphs landed between the third and fourth bullets of the `--force` list, splitting one Markdown list into two and separating the "a profile without `auto_generated = true` is never changed" bullet from its siblings.
- **Fix:** Moved both paragraphs below the final bullet so the list renders as one block and the paragraphs read as commentary on the whole rule.
- **Files modified:** `docs/how-to/configure-scan-profiles.md`
- **Verification:** Read back the rendered section; the four bullets are contiguous and the two paragraphs follow.
- **Committed in:** `6299c71` (Task 2 GREEN commit).

### Acceptance-criterion deviations (no code impact)

**`grep -c "assert_never" src/saneless/auto_profiles.py` increases by 3, not the 2 the plan predicted.** Two are the new `case _: assert_never(kind)` call sites the criterion is about; the third is the `from typing import ... assert_never ...` line the file needed, since `auto_profiles.py` had no prior use of it. The criterion's intent — exactly two new `assert_never` call sites — is met.

**`grep -n "Feeder, double-sided"` matches once, as required, but only after a wording change.** The first draft of `_duplex`'s updated docstring quoted the label literally, giving two matches. `_duplex`'s docstring now refers to "the double-sided feeder wording the scan page shows" instead, which keeps the label constant defined in exactly one place — the criterion's actual point.

---

**Total deviations:** 3 auto-fixed (2 bugs, 1 blocking) + 2 acceptance-criterion notes.
**Impact on plan:** No scope creep. Two of the three fixes were to this plan's own new tests; the third was a Markdown rendering defect in this plan's own documentation edit. All plan-specified strings, behaviours and file boundaries were honoured.

## Issues Encountered

- **The worktree started on the wrong base.** `HEAD` was at `ed2d620` (a master-line merge commit), not the expected `808b390`. The working tree was clean, so the startup guard's `git reset --hard 808b390` corrected it before any work began. No content was lost.
- **`--force` round-trip test coverage needed a flagged `default`.** `TestForceMerge`'s existing fixture deliberately leaves `[profiles.default]` unflagged to test D-01. The new ownership class uses its own fixture with a flagged `default` so the "delete branch never fires" assertion can check a second refreshed table; the two fixtures coexist and both assertion sets pass.
- **`TestForceMerge::test_merge_adds_a_missing_name_in_generated_key_order` asserts an exact TOML block.** Adding two leading owned keys necessarily changed it. The expected block gained the two lines and its docstring now explains that the human name leads the table by design; nothing else about that assertion changed.

## Known Stubs

None. Both new functions return real values for every `SourceKind`, and `generate_profiles` wires them into every profile it builds.

## Threat Flags

None. This plan adds no network endpoint, auth path, file-access pattern or schema change beyond the two `ProfileConfig` fields plan 30-02 already added and bounded. T-30-17 and T-30-19 are mitigated as the register specifies:

| Threat | Mitigation as built |
|---|---|
| T-30-17 (XSS via vendor source name) | Both functions select a developer-authored constant; asserted by `test_no_returned_string_carries_anything_from_the_source_name` with an injected marker and a companion branch-coverage test |
| T-30-19 (unbounded generated text) | `test_label_is_non_empty_and_within_the_config_cap` and `test_description_is_a_non_empty_sentence_within_the_config_cap`, parametrised over `list(SourceKind)`, assert every string fits its `max_length` |
| T-30-18 (`--force` destroying a hand-typed label) | Accepted per D-18; the how-to now states the rule and the escape hatch in plain words, which is the mitigation the decision asked for |

## Verification Results

All plan gates pass:

- `uv run pytest tests/test_auto_profiles.py -q` → **216 passed**
- `uv run pytest tests/test_deployment_config.py -q` → **29 passed**
- `uv run pytest -m "not browser and not sane_hardware" -q` → **2259 passed, 74 deselected**
- `uv run ruff check .` → All checks passed
- `uv run ruff format --check .` → 55 files already formatted
- `uv run ty check` → All checks passed
- `uv run pyrefly check src tests` → 0 errors

Acceptance greps:

- `def _profile_label(source: str) -> str:` → 1 match
- `def _profile_description(source: str) -> str:` → 1 match
- `"Feeder, double-sided"` → 1 match; `"eventual reader"` → 0 matches
- `_OWNED_KEYS` first two entries → `"label"`, `"description"`
- `"label": _profile_label(` → 1 match, at the top of the `values` dict literal, not inside an `if`
- `grep -c label docs/how-to/configure-scan-profiles.md` → 4 (≥3 required); `auto-profiles --force` → 3 matches
- `git diff` hunk headers for `auto_profiles.py` touch only `_OWNED_KEYS` and `_generated_values` — `_merge_profile` and `write_profiles_to_config` are byte-identical
- `pytest -k label` → 30 selected, all passing (≥12 required)

TDD gate sequence in `git log`: `test(30-05)` → `feat(30-05)` → `test(30-05)` → `feat(30-05)`. No `refactor` commit was needed; neither GREEN left code worth cleaning up.

## Next Phase Readiness

Ready for the plans that consume this text:

- A generated profile now carries `label` and `description`, so the S4 profile `<select>` and its `#profile-description` partial have data to render. `{{ p.label or p.name }}` is still required — Amendment A-3 — because a config generated before this release keeps `label = ""` until an operator runs `saneless auto-profiles --force`, and the how-to now tells them so.
- `_profile_description` is the single source of the sentence text. The `GET /api/profiles/description` route should read `ProfileConfig.description` from the loaded config rather than calling the helper, so an operator who took a profile over by removing `auto_generated` sees their own wording.

No blockers.

## Self-Check: PASSED

- All three modified files exist on disk.
- All four task commits (`25de735`, `0093428`, `f1e0dd5`, `6299c71`) are present in `git log`.
- Working tree clean, no untracked files, no file deletions across the plan's commit range.
- `STATE.md` and `ROADMAP.md` untouched — the orchestrator owns those writes.

---
*Phase: 30-appliance-layer*
*Completed: 2026-09-16*
