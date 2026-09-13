---
phase: 24-scanner-truthfulness
plan: 02
subsystem: auto-profiles
tags: [slugs, classification, tomlkit, documentation]
requires:
  - saneless.scanner.base.classify_source
  - saneless.scanner.base.SourceKind
provides:
  - identity profile slugs over a strict [a-z0-9-] character set
  - deterministic slug-collision tie-break with a logged warning
  - orphaned auto-generated profile prune on config write
affects:
  - saneless.auto_profiles
  - docs/how-to/configure-scan-profiles.md
  - docs/reference/configuration.md
  - docs/how-to/set-up-adf-duplex.md
tech-stack:
  added: []
  patterns:
    - module-level private helper to keep a function under ruff PLR0912
    - isinstance narrowing over cast-typed tomlkit values, never a suppression
    - lazy %s/%r logging interpolation (ruff G rules)
key-files:
  created: []
  modified:
    - src/saneless/auto_profiles.py
    - tests/test_auto_profiles.py
    - tests/test_cli.py
    - tests/test_worker.py
    - docs/how-to/configure-scan-profiles.md
    - docs/reference/configuration.md
    - docs/how-to/set-up-adf-duplex.md
decisions:
  - "Empty-slug fallback is the literal \"source\""
  - "Collision tie-break suffix is -2, -3, ... in capabilities.sources order"
  - "Prune runs independent of force"
  - "scanner-host-discovery.md deliberately not edited"
  - "No job.profile migration, on a confirmed grep"
requirements: [SCNR-01]
metrics:
  duration: 22min
  tasks: 3
  files: 7
  commits: 6
  completed: 2026-09-13
---

# Phase 24 Plan 02: Scanner Truthfulness — Profile Slugs and the Single Classifier Summary

`classify_source()` is now the only classification rule in `auto_profiles.py`, every profile is
named from the device's own source wording over a strict `[a-z0-9-]` character set, colliding names
get a deterministic logged tie-break instead of silently overwriting each other, and the profiles
that the rename strands are pruned on write.

## What Changed

**`source_to_slug` (D-14)** collapsed to a one-line passthrough over `_slugify`. The four hard-coded
friendly names and the `"ADF Back"` special case they forced are gone. The reasoning that special
case recorded is preserved in the new docstring, because it is what makes the rename a correctness
fix rather than a cosmetic one: `"ADF Front"` and `"ADF Back"` are both feeders for *routing*, so
naming a profile from its `SourceKind` collapsed two distinct sources into one profile and silently
lost one (N-09). Naming from the source itself removes the need for that rule entirely.

**`_slugify` (D-15)** now has the character set as its contract. It lowercases internally so no
caller can hand it a half-normalised string, replaces each run outside `[a-z0-9]` with a single
hyphen, strips leading and trailing hyphens, and guards the empty result. It remains deliberately
separate from the PDF filename sanitiser, per Phase 23's D-19.

**`generate_profiles` (D-02 + Q9)** asks `classify_source` at all three former string-rule sites.

**`write_profiles_to_config` (D-16)** gained the orphan prune.

**Four documentation sentences** that this plan's behaviour made false were corrected.

## Decisions Made

**The empty-slug fallback is `"source"`.** Chosen because it is already the domain's own word, so it
cannot be mistaken for a device's wording, and because the tie-break turns a second degenerate name
into `source-2` rather than losing it. A whitespace-only source name used to degenerate to `--`,
which is not addressable as a `--profile` value.

**The tie-break suffix is `-2`, `-3`, …, in `capabilities.sources` order.** The first source to claim
a slug keeps it bare. "Last wins" was rejected because it silently drops a source the device
reported, which is N-09's actual complaint. Because the walk follows the device's own stable source
ordering, generating twice from the same capabilities yields identical slugs — asserted directly.
The tie-break lives in a module-level private helper `_claim_slug`, following the house pattern, so
the added loop could not push `generate_profiles` over ruff's `PLR0912` limit. Nothing was
suppressed and no limit was raised.

**The prune runs independent of `force`, and the docstring says why.** `force` governs overwriting
keys that are *present* in the generated set; an orphan is by definition absent from it, so `force`
has nothing to say about it. A profile without a truthy `auto_generated` flag is never touched —
CFG-07's literal wording, which keeps this from pre-empting Phase 27's general `--force` merge
semantics. This plan supplies only the cleanup D-14's rename makes necessary.

**`docs/how-to/scanner-host-discovery.md` was deliberately not edited.** The phase goal's wording
implies work there, but the page was read end to end during pattern mapping and every claim matches
`sane_backend.py:250-257`. Verified mechanically: the file has zero diff across this entire plan. A
verifier should read its absence as the mapped finding, not an oversight.

**No job-store migration was written for historic `job.profile` slugs (A2).** Accepted on the
confirmed grep rather than assumed: the stored slug reaches exactly three display-only sites
(`cli.py:246`, `cli.py:276`, `web/templates/partials/history.html:5`), there is no
retry/rerun/resubmit path anywhere, and the profile dropdown iterates live `settings.profiles` keys
rather than historic rows. A renamed profile simply leaves old jobs displaying the name that was
true when they ran, which is accurate history. Re-confirmed here: `grep -rn 'migrat' src/saneless/job.py
| grep -ci profile` is 0.

**`docs/how-to/configure-scan-profiles.md:128` was a claim that had never been true.** It advertised
`flatbed-color-300` and `adf-gray-150`; no version of `source_to_slug` has ever produced that shape.
It was replaced with the real rule, plus a note that regenerating can rename a profile so a
`--profile` value in a script or cron entry may need updating.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `classify_source`/`SourceKind` imports could not be "kept" through Task 1**

- **Found during:** Task 1 GREEN
- **Issue:** The plan says to keep both imports because Task 2 needs them. But once `source_to_slug`
  stops dispatching on the classifier, both are unused, and ruff `F401` fails the commit. The two
  instructions cannot both hold, and no suppression is permitted.
- **Fix:** Task 1 drops them (along with the now-unused `assert_never`); Task 2 re-adds them when
  `generate_profiles` starts calling `classify_source`. Recorded in both commit messages.
- **Commit:** 33acf9f, d8b0ed3

**2. [Rule 3 - Blocking] The prune fixture cannot spell the retired slugs**

- **Found during:** Task 3 RED
- **Issue:** The plan's behaviour spec names `[profiles.adf-simplex]` and `[profiles.flatbed-scan]`
  as the orphan fixture, but this same task's acceptance criterion requires those literals to appear
  nowhere in `docs/`, `src/` or `tests/`. Both cannot hold.
- **Fix:** The fixture's orphan is named `legacy-feeder`, with a comment explaining that it stands
  for a profile generated under the pre-D-14 scheme and is named neutrally on purpose. The
  substitution is faithful because the prune keys on the `auto_generated` flag, never on the name.
- **Commit:** b2d523c

**3. [Rule 1 - Bug] Two `test_cli.py` assertions would have passed before and after the rename**

- **Found during:** Task 1 RED
- **Issue:** Rewriting `assert "flatbed-scan" in result.output` to `assert "flatbed" in result.output`
  produces an assertion that is a substring of the *old* name too, so it could not witness the
  change — a silently weakened test.
- **Fix:** Assert the whole printed line, `"  flatbed: source=Flatbed"`. Confirmed genuinely RED
  (failures rose 22 → 24).
- **Commit:** 1493585

**4. [Rule 1 - Bug] `RUF012` / `D401` / `D403` in the new tests**

- **Found during:** Task 2 RED, Task 3 RED
- **Fix:** `_COLLIDING` became a tuple rather than a mutable class attribute; two docstrings were
  reworded to imperative mood and capitalised. Fixed properly, not suppressed.
- **Commits:** 4965482, b2d523c

### Corrections to the Plan's Own Claims

These are recorded because a verifier checking the plan literally would otherwise read them as
failures:

- **The plan's Task 2 criterion says a lowercase `"auto"` test "fails before the change and passes
  after." It does not** — `source.lower() == "auto"` already matches that spelling. The genuinely
  RED spellings are `" AUTO "` (stray whitespace defeats the equality rule) and `"Flatbed Duplex"`
  (the substring rule calls a duplex feeder a flatbed, so it backed the default profile). Both are
  asserted; the lowercase case is kept as an explicitly-labelled regression guard, not claimed as RED.
- **The Task 2 criterion `grep -n 'flatbed" in\|== "auto"\|lower() == '` does not return empty**, and
  should not. Two hits remain and both are benign: `pick_preferred_mode` compares scan *modes*
  (Color/Gray), not sources, and the other is the new docstring naming the rules it replaced. No
  ad-hoc source-classification rule survives in `src/`.
- **No `tests/test_scanner.py:326` slug reference exists** to leave for plan 24-08; the retired slugs
  return zero matches across all of `tests/`. Nothing was touched in that file — it belongs to 24-01.

### Threat Model

`T-24-04` (character-set hardening) and `T-24-06` (degenerate names collapsing) are mitigated by
`_slugify` plus the tie-break. `T-24-07` (prune deleting user data) is mitigated by the flag
predicate and asserted by a test that re-reads the file. `T-24-05` is correctly recorded as **accept
/ not exploitable**: no test asserts that an un-hardened slug breaks the config file, because it
does not — tomlkit quotes such a key and `tomllib` round-trips it cleanly.

## Verification

| Gate | Result |
|---|---|
| `pytest -m "not browser"` | **779 passed** (pre-phase baseline 742) |
| `pytest` on the three touched test files | 149 passed |
| `ruff check .` | clean |
| `ruff format --check .` | 41 files formatted |
| `ty check` | All checks passed |
| `pyrefly check src tests` | 0 errors |
| `grep -rn 'adf-simplex\|flatbed-scan\|auto-scan' docs/ src/ tests/` | 0 matches |
| `grep -c 'flatbed-color-300\|adf-gray-150' docs/how-to/configure-scan-profiles.md` | 0 |
| `grep -c 'classify_source(' src/saneless/auto_profiles.py` | 3 |
| `grep -c 'auto_generated' src/saneless/auto_profiles.py` | 13 |
| `git diff` on `docs/how-to/scanner-host-discovery.md` | empty (deliberate) |

Executable acceptance probes printed exactly the required values:
`flatbed adf adf-duplex auto`, `flachbett-einzug adf-left-aligned adf-duplex`, and `ok`.

Every commit ran the full hook suite with no `--no-verify`, no `SKIP=`, no `# type: ignore`, no
`# noqa`, and no stub.

## TDD Gate Compliance

All three tasks followed RED → GREEN, each gate its own atomic commit:

| Task | RED | GREEN |
|---|---|---|
| 1 — identity slugs, hardened character set | `1493585` (24 failing) | `33acf9f` |
| 2 — single classifier, collision tie-break | `4965482` (5 failing) | `d8b0ed3` |
| 3 — orphan prune, doc corrections | `b2d523c` (2 failing) | `ad6811b` |

No RED commit needed a suppression: since Phase 23.1 the commit-stage hooks type-check `src/` only.

## Known Stubs

None. No placeholder, hardcoded-empty or TODO value was introduced.

## Notes for Future Plans

- Plan 24-08's sweep has nothing to remove here: the retired slugs are gone from `docs/`, `src/` and
  `tests/`, including from prose.
- The tie-break's `-2` suffix is applied at profile-assembly time. If a future plan needs the same
  rule elsewhere, reuse `_claim_slug`; do not re-derive it.
- `_slugify` is still **not** safe for filesystem paths by intent. D-19's separation stands.

## Self-Check: PASSED

All 7 modified files exist, all 6 commit hashes resolve, and the plan's commit range contains zero
file deletions.
