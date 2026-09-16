---
phase: 30-appliance-layer
plan: 02
subsystem: config
tags: [pydantic, pydantic-settings, toml, schema, validation, security]

# Dependency graph
requires:
  - phase: 27-config-errors
    provides: the D-10/D-11 unknown-key error renderer, `_SECTION_MODELS`, and the `extra="forbid"` convention every section follows
  - phase: 26-web-robustness
    provides: `TITLE_MAX_LENGTH` as the precedent for bounding a config string that reaches HTML
provides:
  - "`is_placeholder_token(value: str) -> bool` — the one placeholder-token predicate doctor, the web status strip, the scan route and `saneless scan` all share"
  - "`PLACEHOLDER_TOKENS` — the fixed literal set, pinned against `saneless.toml.example` by a test that reads the file"
  - "`ProfileConfig.label` / `.description` — bounded, persisted, tool-owned profile text"
  - "`PROFILE_LABEL_MAX_LENGTH` (64) / `PROFILE_DESCRIPTION_MAX_LENGTH` (200)"
  - "`WebConfig` and `Settings.web` — the `[web] show_tags` / `show_correspondent` form-shape section"
  - "`[web]` registered in `_SECTION_MODELS`, plus a parametrised test that keeps the mapping and `Settings`' fields from drifting apart"
affects: [30-03, 30-04, 30-05, 30-06, 30-08, 30-09, 30-10, 30-11]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "One shared predicate for a cross-surface truth, rather than three surfaces each deciding for themselves"
    - "A new `Settings` section must be registered in `_SECTION_MODELS`, and a parametrised test now enforces it"

key-files:
  created: []
  modified:
    - src/saneless/config.py
    - tests/test_config.py

key-decisions:
  - "D-14 implemented literally: `PLACEHOLDER_TOKENS` is a fixed literal set compared by exact membership after strip+lower, never a substring or shape heuristic, so `changeme7f3a91` is a real token"
  - "The predicate takes an already-unwrapped `str` and returns only a `bool`; config.py's `get_secret_value` call-site count stays at zero, and the test asserts on the call form `.get_secret_value(` so naming the method in a comment is not mistaken for a call"
  - "`label`/`description` are declared first in `ProfileConfig` so a human reads the human name before the machine settings; both default to `\"\"`, which is what keeps a pre-phase config loading under `extra=\"forbid\"`"
  - "`web_host`/`web_port` stay in `[output]`; moving them would be a breaking config change, so `[web]` holds only the form-shape keys and the incoherence is commented in place"
  - "`WebConfig` had to be added to `_SECTION_MODELS` by hand — the section would otherwise have loaded fine and then rendered a bare pydantic message instead of the D-11 unknown-key line"

patterns-established:
  - "Placeholder-set/example pinning: the test reads `saneless.toml.example` rather than hard-coding its token, so the shipped example and the predicate cannot drift"
  - "Section-registry drift guard: `TestEverySectionRendersUnknownKeys` parametrises over `Settings.model_fields`, so a future section that is not wired into `_SECTION_MODELS` fails immediately"

requirements-completed: [APPL-05, APPL-07, APPL-10]

# Metrics
duration: 26min
completed: 2026-09-16
---

# Phase 30 Plan 02: Config Schema Growth Summary

**One exact-match placeholder-token predicate shared by four surfaces, bounded persisted `label`/`description` on `ProfileConfig`, and a new `extra="forbid"` `[web]` section wired into the D-11 unknown-key renderer.**

## Performance

- **Duration:** 26 min
- **Started:** 2026-09-16T16:05:12Z
- **Completed:** 2026-09-16T16:31:04Z
- **Tasks:** 3 (all TDD, 6 commits)
- **Files modified:** 2

## Accomplishments

- `is_placeholder_token` exists as the single predicate `doctor`, the web status strip, the scan route and `saneless scan` will share, so all four agree on whether the appliance can upload (APPL-07). It is exact-match only: `changeme7f3a91` is a real token.
- `PLACEHOLDER_TOKENS` covers the two values the project actually ships (`changeme` at `docker-compose.yml:30` and `docs/reference/docker.md:178`; `your-api-token-here` in `saneless.toml.example`) plus the `your-token-here` family, and a test reads the example file rather than hard-coding its value so the two cannot drift.
- `ProfileConfig.label` and `.description` are real persisted fields bounded at 64 and 200 characters, defaulting to `""` so a config written before this phase still loads under `extra="forbid"`.
- `[web] show_tags` / `show_correspondent` exist, both default `true`, and an unknown key under `[web]` produces the project's own error line rather than a bare pydantic message.
- A parametrised drift guard now covers every plain section, so the next one cannot be added to `Settings` without being wired into the error machinery.

## Task Commits

Each task was committed atomically, RED before GREEN:

1. **Task 1: the one placeholder-token predicate**
   - `13ec30f` (test) — `TestPlaceholderToken`, 40 cases including the example-file pin
   - `7ad3df5` (feat) — `PLACEHOLDER_TOKENS` + `is_placeholder_token`
2. **Task 2: `ProfileConfig.label` and `.description`**
   - `2e1cd25` (test) — defaults, round-trip, pre-phase load, both bounds, no alias
   - `e7599e3` (feat) — both fields plus the two named length constants
3. **Task 3: the `[web]` section**
   - `2482529` (test) — `TestWebConfig`, defaults/TOML/env/unknown-key/misplaced-key
   - `6bf9d8a` (feat) — `WebConfig`, `Settings.web`, `_SECTION_MODELS` registration, drift guard

## Files Created/Modified

- `src/saneless/config.py` — added `PLACEHOLDER_TOKENS`, `is_placeholder_token`, `PROFILE_LABEL_MAX_LENGTH`, `PROFILE_DESCRIPTION_MAX_LENGTH`, `ProfileConfig.label`, `ProfileConfig.description`, `WebConfig`, `Settings.web`; registered `web` in `_SECTION_MODELS`; five new `__all__` entries.
- `tests/test_config.py` — added `TestPlaceholderToken`, `TestProfileLabelAndDescription`, `TestWebConfig`, `TestEverySectionRendersUnknownKeys`; updated one existing valid-sections assertion for the new section.

## Decisions Made

- **The unwrap-site assertion tests the call form, not the bare name.** The plan's acceptance criterion is "the `get_secret_value` grep count is unchanged", and the honest invariant behind it is "config.py contains no unwrap *call*". `config.py` has always named the method in a comment on `PaperlessConfig.token`, so a naive `"get_secret_value" not in source` test can never pass. The test asserts `".get_secret_value(" not in source`; the raw grep count is still 1, unchanged from before the plan, because the new docstring says "secret-unwrapping call site" rather than repeating the method name.
- **`web` is declared between `output` and `profiles` on `Settings`.** That is where it reads naturally, and it puts `profiles` — the only dict-of-tables section — last. The order is user-visible: it is the "valid sections" list in an unknown-section error.
- **`WebConfig` needed explicit registration.** `_valid_keys`, `_section_owning`, `_describe_unknown_top_level` and `_unknown_env_lines` all derive from `Settings.model_fields` and picked `[web]` up for free, but `_render_error` looks the section's model up in the hand-maintained `_SECTION_MODELS`. Without the entry, `[web]\nshow_tag = false` loaded into the renderer and came out as a bare `Extra inputs are not permitted` — exactly the silence CFG-01 exists to remove. The plan anticipated this ("if they do not, extend them in the same change").

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] The RED test for the unwrap-site invariant asserted something that was never true**

- **Found during:** Task 1 (placeholder-token predicate)
- **Issue:** `test_config_module_adds_no_secret_unwrap_site` asserted `"get_secret_value" not in source`. `config.py:293` has named the method in a comment since Phase 27 ("Unwrapped with get_secret_value only where PaperlessClient is built"), so the assertion failed on the *baseline* file, not on anything the plan added — it tested the wrong thing.
- **Fix:** Changed the assertion to the call form, `".get_secret_value(" not in source`, which is the invariant T-30-05 actually names. Reworded the new docstring to say "secret-unwrapping call site" so the raw grep count also stays at 1, satisfying the plan's literal acceptance criterion.
- **Files modified:** `tests/test_config.py`, `src/saneless/config.py`
- **Verification:** `grep -c get_secret_value src/saneless/config.py` returns 1, unchanged from the pre-plan file; the test passes.
- **Committed in:** `7ad3df5` (Task 1 GREEN commit)

**2. [Rule 2 - Missing Critical] `_SECTION_MODELS` had no guard against a section being registered in one place and not the other**

- **Found during:** Task 3 (`[web]` section)
- **Issue:** Adding `[web]` exposed that `_SECTION_MODELS` is hand-maintained while every other reader derives from `Settings.model_fields`. A section present in the fields but missing from the mapping loads successfully and silently loses its D-11 unknown-key line, degrading to pydantic's bare message. This is a correctness requirement for CFG-01, not a nicety: a mistyped key under a security-relevant section would stop naming itself.
- **Fix:** Added `TestEverySectionRendersUnknownKeys`, parametrised over `set(Settings.model_fields) - {"profiles"}`, asserting each section renders `[<section>] unknown key '...'; valid keys: ...`. Added a comment on `_SECTION_MODELS` explaining the coupling and pointing at the test.
- **Files modified:** `src/saneless/config.py`, `tests/test_config.py`
- **Verification:** The test passes for all four sections; removing the `"web": WebConfig` entry makes it fail.
- **Committed in:** `6bf9d8a` (Task 3 GREEN commit)

**3. [Rule 3 - Blocking] An existing assertion pinned the pre-`[web]` section list**

- **Found during:** Task 3 (`[web]` section)
- **Issue:** `test_unknown_env_misspelled_section_suggests_variable` asserted `valid sections: scanner, paperless, output, profiles`, which the new section necessarily changes.
- **Fix:** Updated the expected string to `scanner, paperless, output, web, profiles`. The assertion is doing its job — the section list is user-visible output and should be pinned.
- **Files modified:** `tests/test_config.py`
- **Verification:** Full non-browser suite green (2110 passed).
- **Committed in:** `6bf9d8a` (Task 3 GREEN commit)

**4. [Rule 3 - Blocking] `TestWebConfig`'s methods were not selectable by the plan's `-k web_config` criterion**

- **Found during:** Task 3 acceptance check
- **Issue:** pytest `-k` matches node ids, and `TestWebConfig::test_both_default_on` contains no `web_config` substring, so the plan's `uv run pytest -k web_config` selected zero tests.
- **Fix:** Renamed all eight methods to a `test_web_config_*` form.
- **Files modified:** `tests/test_config.py`
- **Verification:** `uv run pytest tests/test_config.py -k web_config -q` selects and passes 8 tests.
- **Committed in:** `6bf9d8a` (Task 3 GREEN commit)

---

**Total deviations:** 4 auto-fixed (1 bug, 1 missing critical, 2 blocking)
**Impact on plan:** No scope creep. Deviation 2 is the only behaviour-adjacent addition and it is a CFG-01 correctness guard directly caused by this plan's new section. `_OWNED_KEYS` in `auto_profiles.py` was deliberately left alone — plan 30-05 owns extending it — and `resolve_job_title` was not touched, per the plan's explicit instruction.

## Issues Encountered

- **Serena's `insert_before_symbol` wrote into the parent repository, not this worktree.** The edit landed in `/home/kris/git/saneless/src/saneless/config.py` while every other tool targeted the worktree copy. Detected immediately (the worktree file still had no `is_placeholder_token`), the block was moved into the worktree and the parent file restored byte-for-byte; `grep -c` on the parent confirms zero traces. No commit ever contained the stray edit. Serena's write tools were not used again for the rest of the plan.

## Verification

All plan-level criteria met:

- `uv run pytest tests/test_config.py -q` → 242 passed
- `uv run pytest -m "not browser and not sane_hardware" -q` → 2110 passed
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests` → all clean
- A pre-phase `config.toml` (no `label`, no `description`, no `[web]`, with `[output] web_port`) loads and yields `label == ""`, `description == ""`, `show_tags is True`, `show_correspondent is True`, `web_port == 8080`
- Zero suppressions added; the only `# noqa` in `config.py` are the two pre-existing `ARG003` ones on `settings_customise_sources`

## User Setup Required

None — no external service configuration required. This plan installs nothing (T-30-SC: the phase adds no dependency).

## Next Phase Readiness

Ready for the wave-2 plans that read this schema:

- **30-03/30-04 (doctor, status strip) and 30-08/30-09 (scan route, `saneless scan`)** can import `is_placeholder_token` directly. It takes an already-unwrapped `str`, so each caller unwraps at its own existing site — the predicate adds none.
- **30-05 (auto-profile generation)** must add `"label"` and `"description"` to `auto_profiles._OWNED_KEYS` and write them from `_generated_values()`; this plan deliberately left generation untouched, and `tests/test_auto_profiles.py` passes unchanged.
- **30-06/30-11 (form shape, tag picker)** can read `settings.web.show_tags` / `.show_correspondent`. D-29 is a hard constraint on those plans: hiding a control must change the form and never the scan, so `default_tags` and `default_correspondent` must still be applied when the controls are hidden.
- **Docs are not updated.** `docs/reference/configuration.md` still lists only `[scanner]`, `[paperless]`, `[output]` and `[profiles]`. The `[web]` keys and the `label`/`description` profile keys need documenting, and the how-to guide needs D-18's "remove `auto_generated` to take the profile over" escape hatch. A later plan in this phase owns that.

## Self-Check: PASSED

- All modified files exist on disk: `src/saneless/config.py`, `tests/test_config.py`, `.planning/phases/30-appliance-layer/30-02-SUMMARY.md`
- All seven commits present on `worktree-agent-ae1cf3d21174d057f`, RED before GREEN for each of the three TDD tasks: `13ec30f`, `7ad3df5`, `2e1cd25`, `e7599e3`, `2482529`, `6bf9d8a`, `caba98f`
- No files deleted relative to the plan base `6867e79`; working tree clean
- No modifications to `STATE.md` or `ROADMAP.md` (orchestrator-owned)

---
*Phase: 30-appliance-layer, Plan: 02*
*Completed: 2026-09-16*
