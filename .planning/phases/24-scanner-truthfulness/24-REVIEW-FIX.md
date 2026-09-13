---
phase: 24
fixed_at: 2026-09-13T00:00:00Z
review_path: .planning/phases/24-scanner-truthfulness/24-REVIEW.md
iteration: 1
findings_in_scope: 13
fixed: 13
skipped: 0
status: all_fixed
---

# Phase 24: Code Review Fix Report

**Fixed at:** 2026-09-13
**Source review:** `.planning/phases/24-scanner-truthfulness/24-REVIEW.md`
**Iteration:** 1

**Summary:**
- Findings in scope: 13 (fix_scope `all`; the review reported 3 critical, 10 warning, 0 info)
- Fixed: 13
- Skipped: 0

Every fix was committed atomically with the full gate green at that commit:
`ruff check`, `ruff format --check`, `ty check`, `pyrefly check src tests`
(0 errors, and the pre-existing 4 warnings neither grown nor suppressed), and
`pytest -m "not browser and not sane_hardware"`. The suite went from 905
passing at review time to **931 passing**; no test was deleted, and the three
that pinned defects were re-pointed at guarantees rather than removed. A final
`prek run --all-files` passes over the whole tree.

## Fixed Issues

### CR-01: The orphan prune deletes the required `default` profile

**Files modified:** `src/saneless/auto_profiles.py`, `tests/test_auto_profiles.py`
**Commit:** `f7d1d79`
**Applied fix:** Added an `_UNPRUNABLE = frozenset({"default"})` guard to the
orphan comprehension, with a comment recording *why* `default` is not an
ordinary profile. New `TestDefaultProfileSurvivesThePrune` proves the default
survives a feeder-only regeneration, that other orphans are still pruned (the
guard is one name wide, not a disabling of D-16), and — the assertion that
matters — that the written file round-trips through `load_settings`.

### CR-02: `auto-profiles` writes a config it cannot load

**Files modified:** `src/saneless/auto_profiles.py`, `tests/test_auto_profiles.py`
**Commit:** `2f5a977`
**Applied fix:** `generate_profiles` now emits `default` unconditionally,
falling back to the device's first reported source when there is no flatbed.
Extracted `_auto_source_mode(source, *, has_flatbed=...)` so the default
carries the *same* routing mode the per-source loop would have given it —
otherwise a default backed by an `Auto` source on a platen-less device would
route a whole stack as one page, disagreeing with the very profile it copied.

`test_feeder_only_device_has_no_default_profile` pinned the defect and was
re-pointed at the guarantee (renamed `..._still_gets_a_default_profile`), plus
a new `load_settings` round-trip test. Three further tests that counted
profiles (`test_duplex_feeder_is_not_mistaken_for_a_flatbed`,
`test_both_sources_survive_with_a_suffix`, `test_no_source_is_silently_lost`)
were updated: the last now asserts the N-09 invariant against the *source
strings the profiles carry* rather than a key count, because the required
`default` key aliases one of the sources and counting keys no longer expresses
the invariant.

Both CR-01 and CR-02 were implemented, though the review noted either alone
would close its own trigger. They fail for different reasons and both are cheap.

### CR-03: The clamp check ignored the top-left read-back

**Files modified:** `src/saneless/scanner/sane_backend.py`, `tests/test_scanner.py`
**Commit:** `db2ee01`
**Applied fix:** `_area_matches` now compares the reported *box*, `br - tl`,
instead of the far corner alone. New
`test_a_clamped_top_left_falls_through_to_the_crop` drives a device with a
`(10.0, 300.0, 1.0)` range and asserts `br` really was honoured while only `tl`
moved — which is what makes the far corner an insufficient test rather than a
redundant one — then asserts the warning and the crop.

### WR-01: A scale of `0.0` passed the `is None` guard

**Files modified:** `src/saneless/scanner/sane_backend.py`, `tests/test_scanner.py`
**Commit:** `9907cd6`
**Applied fix:** `if scale is None or scale <= 0.0`. New
`TestNonPositiveGeometryScale` first asserts the premise (`_units_per_mm(
UNIT_PIXEL, 0) == 0.0`), then that `_set_geometry` declines, then that no
corner is assigned to the device at all.

### WR-02: The silent `Auto` source fallback

**Files modified:** `src/saneless/scanner/sane_backend.py`, `tests/test_scanner.py`
**Commit:** `e87fa19`
**Applied fix:** Raised INFO to WARNING and named the consequence — that
routing is decided by `auto_source_mode` and may not be multi-page. Logged in
`_resolve_source`, which needs no signature change; the existing `scan_pages`
INFO still reports the actual `auto_source_mode` value, so the two together
give the operator both facts. No test asserted the old level.

### WR-03: The flatbed path applied no integrity checks

**Files modified:** `src/saneless/scanner/sane_backend.py`, `tests/test_scanner.py`
**Commit:** `84ea791`
**Applied fix:** The single-sheet path now runs `_validate_page_image` and
raises `ScanError` on failure — fatal rather than counted, because a flatbed
has no next sheet. Documented in the `Raises:` section. New
`TestFlatbedIntegrityChecks` covers a zero-dimension sheet, a sheet below the
byte floor, and that a good page still reports `pages_rejected == 0`.

### WR-04: A source slugging to `default` was silently lost

**Files modified:** `src/saneless/auto_profiles.py`, `tests/test_auto_profiles.py`
**Commit:** `044a79b`
**Applied fix:** The default's backing source is now decided *before* the loop
so its slug can be reserved in `claimed`, sending a colliding source to
`default-2` and producing the collision WARNING naming both. New
`TestReservedDefaultSlug` uses sources `["Default", "Flatbed"]`.

Note: the review's suggested seed — `{"default": ...} if has_flatbed else {}` —
was superseded by CR-02, which makes the default unconditional; the reservation
is therefore unconditional too, and is seeded with the actual backing source so
the collision warning reads truthfully.

### WR-05: A malformed `profiles` key crashed the writer

**Files modified:** `src/saneless/auto_profiles.py`, `tests/test_auto_profiles.py`
**Commit:** `d4f628e`
**Applied fix:** Narrowed `doc["profiles"]` with `isinstance(section, Mapping)`
before the `cast`, raising `ConfigError`, consistent with how
`_is_auto_generated` already guards each entry. New
`TestMalformedProfilesSection` asserts the typed error and that the user's
malformed file is left untouched.

### WR-06: The fake's `area` raised `KeyError`

**Files modified:** `tests/fake_sane.py`, `tests/test_scanner.py`
**Commit:** `280bd76`
**Applied fix:** Composed `area` from attribute reads as `sane.py:220` does, so
a geometry-less device raises `AttributeError`. New
`TestFakeDeviceAreaMatchesTheLibrary` asserts that and that the happy path is
unchanged.

### WR-07: `start()` ordering made `start_error_page` unreachable

**Files modified:** `tests/fake_sane.py`, `tests/test_scanner.py`
**Commit:** `c76b9a3`
**Applied fix:** The armed error is checked before the page budget, and the
delay now sits after the budget check so the end-of-feed probe is not charged
one. New `TestFakeFeederStartOrdering` proves an error armed at the probe index
now surfaces (it previously returned three pages and no error), and counts
`time.sleep` calls to prove the probe is undelayed.

### WR-08: Two CLI scanner stubs duck-typed the ABC

**Files modified:** `tests/test_cli.py`
**Commit:** `6f1188d`
**Applied fix:** `MockSaneBackend` and `FailScanner` now subclass
`ScannerBackend` with real parameter names and types; `FailScanner` gained the
two abstract methods it needed to be instantiable and declares the real
`-> ScanBatch` return type instead of the contradictory `-> None`.

### WR-09: The failed-directory warning fired once per rescued file

**Files modified:** `src/saneless/pipeline.py`, `tests/test_pipeline.py`
**Commit:** `c2d20fb`
**Applied fix:** Moved `_warn_if_failed_dir_growing` after the loop, guarded by
`if destinations:`, and corrected the docstring's Args wording (which described
a mid-loop, single-file state). New `TestFailedDirWarningFiresOncePerGuard`
drives `_preserving` over two PDFs with the directory one short of the
threshold — the arrangement that produced two warnings before — and asserts one
warning naming the final count.

### WR-10: Docs described the prune without its consequence

**Files modified:** `docs/how-to/configure-scan-profiles.md`, `docs/reference/cli-commands.md`
**Commit:** `035929f`
**Applied fix:** Because CR-01 and CR-02 both landed as specified, the docs now
state the guarantee rather than the hazard: a `default` is always written
(flatbed-backed when available, otherwise the first reported source) and
regenerating never removes it. Added exit code 2 to the `auto-profiles` table —
reachable for every command, since `cli()` loads settings before dispatch.

## Deviations from the review's suggested fixes

Each of these is a place where the suggestion was not applied verbatim.

- **WR-07 — declined the secondary suggestion.** The review adds "consider also
  raising in `__init__` when `start_error_page >= pages`". That directly
  contradicts the primary fix: reordering the checks is *precisely* what makes
  arming an error at the probe index reachable, so forbidding it in `__init__`
  would re-close what was just opened. Also note the finding's prose ("check the
  armed error first") disagrees with its own code block, which still shows the
  budget check first and so would not have fixed that half at all. The prose was
  followed.
- **WR-08 — scope held to the two named stubs.** `test_cli.py` contains four
  scanner-ish doubles, not two. `_RangeScanner` and `_AutoScanner` define no
  `scan_pages`, so they were never exposed to the `ScanBatch` contract change
  that motivated the finding; converting them would be a refactor beyond it.
- **WR-04 — seed expression superseded.** See above; CR-02 changed the
  precondition the review's snippet assumed.
- **WR-02 — logged in `_resolve_source`.** The review offered a snippet with a
  dangling argument list and suggested `scan_pages` as an alternative. Logging
  at the substitution site needs no signature change and keeps the message next
  to the decision it describes.

## Residual issues (not regressions, but not fully closed)

- **WR-05 — the CLI still shows a traceback.** The writer now raises a typed
  `ConfigError` naming the file and the refusal, which is the fix the finding
  specifies. But `cli.py`'s `auto-profiles` command wraps nothing in a
  `try/except`, so the user sees a `ConfigError` traceback rather than the
  friendly `Configuration error: ...` + exit 2 that `cli()` produces for config
  *loading* failures. Catching it there would invent an exit-code contract for
  that command, which felt like exactly the undocumented decision WR-10
  complains about. Worth a follow-up decision.

## Needs human verification

These changed production behaviour in ways passing tests cannot fully vouch for.
Per the fix protocol they are flagged rather than assumed correct.

- **WR-03 — `fixed: requires human verification`.** A flatbed page below
  `_MIN_PAGE_BYTES` (10 KB) is now a hard `ScanError` where it previously flowed
  through. That is the intended symmetry with the feeder path, but it is a new
  fatal path: a genuinely small legitimate scan — a receipt or business card at
  low dpi — would now fail the job outright rather than produce a small PDF.
  Confirm the byte floor is the right policy for single-sheet scans.
- **CR-02 — `fixed: requires human verification`.** "The device's first reported
  source backs the default on a flatbed-less scanner" is a product decision, not
  a derivable fact. It is the only honest candidate available, but confirm it is
  the desired default for a sheet-fed document scanner.
- **CR-03 — `fixed: requires human verification`.** The `br - tl` arithmetic is
  verified against the shared fake, and the phase's `sane_hardware` tests give a
  second anchor, but the original defect was found by reasoning about real
  device ranges. Worth one run against real hardware.

---

_Fixed: 2026-09-13_
_Fixer: Claude (gsd-code-fixer)_
_Iteration: 1_
