---
phase: 20-ci-gate
plan: 05
subsystem: tooling
tags: [type-checking, ty, pyrefly, dependency-bump, suppressions, protocol, ci]

# Dependency graph
requires:
  - phase: 20-04
    provides: "Active `master gate` ruleset requiring lint@15368 and test@15368, and PR #1 open on autodev-filtered -- the gate this bump had to pass through"
  - phase: 20-03
    provides: "PR #1 (https://github.com/kdknigga/scanless/pull/1) and the pushed autodev-filtered branch the bump is cherry-picked onto"
provides:
  - "ty 0.0.80 and pyrefly 1.2.0 in pyproject.toml and uv.lock, both reporting zero errors"
  - "Zero `# type: ignore` comments anywhere in src/ or tests/ -- all three replaced by real typing constructs"
  - "src/saneless/config.py `_SettingsFactory` Protocol: the typed contract for the private `_toml_file` init kwarg"
  - "tests/test_scanner.py `MockSaneDev._options_impl`: pluggable option tuples matching the existing `_snap_impl` pattern"
  - "A green pull_request CI run (34488308016) on the bumped toolchain, both required contexts success"
affects: [32-suite-hygiene, 31-delivery-and-identity]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "A runtime-only private init kwarg is typed with a TYPE_CHECKING-safe Protocol plus `cast(\"Name\", value)`, never a suppression"
    - "Test doubles expose behaviour through a pluggable `_<name>_impl` attribute rather than by reassigning a bound method"
    - "A deliberately-red intermediate commit is impossible under prek's `always_run: true` type-checker hooks -- commit order must be chosen so every commit is green"

key-files:
  created:
    - ".planning/phases/20-ci-gate/20-05-SUMMARY.md"
  modified:
    - "pyproject.toml"
    - "uv.lock"
    - "src/saneless/config.py"
    - "tests/test_scanner.py"

key-decisions:
  - "Commit order swapped: source fixes (e7f63b0) before the bump (50929f2), because prek's ty hook is always_run and --no-verify is forbidden"
  - "Site 1 fixed with a `_SettingsFactory` Protocol rather than `Callable[..., Settings]` -- it names the real kwarg instead of erasing the whole signature"
  - "Sites 2/3 fixed with `_options_impl: list[tuple] | None`, the same shape MockSaneDev already uses for `_snap_impl`"
  - "PR #1 left OPEN and unmerged (D-21); master still a87b3dd; zero tags"

patterns-established:
  - "Predicted type-checker fallout is reconciled against measured output before any source is touched (3 predicted, 3 observed, byte-for-byte the predicted sites)"
  - "A lockfile bump is proven scoped by diffing package-block names, not by trusting the line count"

requirements-completed: [CI-01]

# Metrics
duration: 7min
completed: 2026-09-10
---

# Phase 20 Plan 05: Bump the Type Checkers and Fix the Fallout Summary

**`ty` 0.0.24 → 0.0.80 and `pyrefly` 0.57.1 → 1.2.0, the exactly-three predicted `ty` diagnostics fixed with a `_SettingsFactory` Protocol and a pluggable `_options_impl` attribute rather than any suppression, leaving `src/` and `tests/` with zero `# type: ignore` comments, 332 passed / 8 deselected unchanged, and a green `pull_request` run on PR #1 — which is still open and unmerged.**

## Performance

- **Duration:** 7 min
- **Started:** 2026-09-10T14:14:17Z
- **Completed:** 2026-09-10T14:21:05Z
- **Tasks:** 3 (all automated, no checkpoints)
- **Files modified:** 4

## Task Commits

| Task | Name | Commit | Files |
|---|---|---|---|
| 2 | Remove all three suppressions with real typing fixes | `e7f63b0` | `src/saneless/config.py`, `tests/test_scanner.py` |
| 1 | Bump ty and pyrefly | `50929f2` | `pyproject.toml`, `uv.lock` |
| 3 | Publish to the pull request | no local commit — cherry-picks `3fc4c1f`, `f66d132` on `autodev-filtered` | — |

Task 2's commit precedes Task 1's. That inversion is deliberate and is the plan's one deviation; see
*Deviations from Plan* below.

## Resolved versions

| Package | Before (locked) | After (locked) | pyproject lower bound |
|---|---|---|---|
| `ty` | 0.0.24 | **0.0.80** | `ty>=0.0.21` → `ty>=0.0.80` |
| `pyrefly` | 0.57.1 | **1.2.0** | `pyrefly>=0.55.0` → `pyrefly>=1.2.0` |

Both match RESEARCH.md's predicted targets exactly (`ty` 0.0.80, `pyrefly` 1.2.0 — read back from the
PyPI JSON API before pinning, not assumed). `pyrefly` crosses 1.0 as D-16 anticipated.

`git diff` on `pyproject.toml` for the bump commit is **two lines changed, two lines added** — no other
dependency line touched and no reordering of the `[dependency-groups].dev` list. `uv.lock` changed 68
lines, and the changed hunks' package blocks are exactly two: `name = "pyrefly"` and `name = "ty"`.
`uv sync --locked` exits 0, so there is no lockfile drift.

### `uv add --dev "ty@latest"` does not work here

The plan's literal invocation failed:

```text
$ uv add --dev "ty@latest" "pyrefly@latest"
Added `latest` to workspace members
Added `latest` to workspace members
error: Distribution not found at: file:///home/kris/git/saneless/latest
```

`uv` parsed `pkg@latest` as a local path requirement, not as a version selector. It rolled back cleanly
(`git diff pyproject.toml` was empty afterwards). Resolved by reading the current versions from PyPI and
running `uv add --dev "ty>=0.0.80" "pyrefly>=1.2.0"`, which produced the intended in-place bound raise
with no reordering. Recorded because the plan text will mislead anyone re-running it.

## The pre-fix diagnostics, verbatim

`uv run ty check` immediately after the bump, before any source change:

```text
error[unknown-argument]: Argument `_toml_file` does not match any known parameter
   --> src/saneless/config.py:168:29
    |
168 |             return Settings(_toml_file=toml_file)  # type: ignore[call-arg] -- ty cannot see BaseSettings dynamic __init__ kwargs
    |                             ^^^^^^^^^^^^^^^^^^^^

error[invalid-assignment]: Object of type `() -> list[tuple[int, str, str, str, int, int, int, int, list[str]] | tuple[int, str, str, str, int, int, int, int, list[int]]]` is not assignable to attribute `get_options` of type `def get_options(self) -> list[tuple[Unknown, ...]]`
   --> tests/test_scanner.py:706:9
    |
706 |         mock_dev.get_options = lambda: [  # type: ignore[assignment]
    |         ^^^^^^^^^^^^^^^^^^^^

error[invalid-assignment]: Object of type `() -> list[tuple[int, str, str, str, int, int, int, int, list[str]] | tuple[int, str, str, str, int, int, int, int, list[int]]]` is not assignable to attribute `get_options` of type `def get_options(self) -> list[tuple[Unknown, ...]]`
   --> tests/test_scanner.py:723:9
    |
723 |         mock_dev.get_options = lambda: [  # type: ignore[assignment]
    |         ^^^^^^^^^^^^^^^^^^^^

Found 3 diagnostics
```

`uv run pyrefly check` immediately after the bump:

```text
 INFO Checking project configured at `/home/kris/git/saneless/pyproject.toml`
 INFO 0 errors
```

**No divergence from the prediction.** RESEARCH.md Pitfall 8 predicted exactly three `ty` diagnostics at
`config.py:168`, `test_scanner.py:706` and `test_scanner.py:723`, with rules `unknown-argument`,
`invalid-assignment`, `invalid-assignment`, and zero new `pyrefly` findings. All six facts matched.
Task 2's scope was therefore unchanged from the plan.

Also measured on the bumped toolchain *before* any source change, so the bump alone is attributable:
`uv run pytest -m "not browser" -q` → **332 passed, 8 deselected in 26.78s** (exit 0, so no new
import-time `DeprecationWarning` tripped `filterwarnings = ["error"]`); `uv run ruff check .` → no
issues; `uv run ruff format --check .` → 37 files already formatted.

## The three fixes

### Site 1 — `src/saneless/config.py` (`unknown-argument`)

`_toml_file` is not a declared field on `Settings`; it is a private init kwarg that the class's own
`settings_customise_sources` pops out of `init_kwargs` (`config.py:133-141`). The suppression was hiding
a real gap between the declared constructor and the constructor the class actually implements.

Added a Protocol that *states* that constructor, then routed the call through the same
`cast("Name", value)` string-literal form the file already uses four lines above the failure site:

```python
class _SettingsFactory(Protocol):
    """
    Callable view of ``Settings`` that accepts the private ``_toml_file`` kwarg.

    ``_toml_file`` is not a declared field on ``Settings``; it is a private init
    kwarg popped out of ``init_kwargs`` by ``settings_customise_sources``. This
    protocol describes the constructor signature that mechanism really provides.
    """

    def __call__(self, *, _toml_file: Path) -> Settings:
        """Construct ``Settings`` from an explicit TOML file path."""
        ...
```

```python
            return cast("_SettingsFactory", Settings)(_toml_file=toml_file)
```

`Protocol` was added to the existing top-level `typing` import (ruff's flake8-type-checking exempts
`typing`, so no `TC` violation). `Path` is already imported at runtime and `from __future__ import
annotations` is in force, so the forward reference to `Settings` costs nothing. The runtime call is
byte-for-byte the same call it was: `Settings(_toml_file=toml_file)`.

`Callable[..., Settings]` was considered and rejected: it would have type-checked by erasing the entire
signature, which is a suppression wearing a `cast`. The Protocol names the one kwarg and rejects every
other, so a future typo in the kwarg name is still an error.

`Settings` itself is unchanged — `_toml_file` was **not** promoted to a declared field, because that
would alter the settings schema (env-var surface, `model_fields`, validation) and is a behaviour change
rather than a typing fix.

### Sites 2 and 3 — `tests/test_scanner.py` (`invalid-assignment`)

Applied the pluggable-impl pattern `MockSaneDev` already uses for `snap()`. One new attribute beside the
existing `_snap_impl`:

```python
        self._snap_impl: MagicMock | None = None
        self._options_impl: list[tuple] | None = None
```

…a three-line delegation at the head of the existing method, with its docstring updated (ruff's `D`
rules apply to `tests/` — the `tests/**` per-file-ignores cover only `S101`, `ARG` and `S104/5/6`):

```python
    def get_options(self) -> list[tuple]:
        """
        Return sample SANE option tuples, or _options_impl if set.

        SANE option format:
        (index, name, title, desc, type, unit, size, cap, constraint)
        """
        if self._options_impl is not None:
            return self._options_impl
        return [
            ...
```

…and both call sites become plain attribute assignments, identical in shape to the
`mock_dev._snap_impl = MagicMock(...)` lines they sit next to:

```python
        mock_dev._options_impl = [
            (1, "source", "Source", "", 3, 0, 1, 5, ["Flatbed", "ADF", "Auto"]),
            (2, "resolution", "Res", "", 1, 4, 1, 5, [300]),
            (3, "mode", "Mode", "", 3, 0, 1, 5, ["color"]),
        ]
```

The option tuples at both sites are preserved byte-for-byte — the edit replaced only the
`mock_dev.get_options = lambda: [` line, leaving every tuple untouched. The tests assert on their
contents, and both still pass.

## Verification

The full five-check sequence on the bumped toolchain, all locally green:

| Check | Result |
|---|---|
| `uv run ruff check .` | All checks passed! |
| `uv run ruff format --check .` | 37 files already formatted |
| `uv run ty check` | **All checks passed!** (exit 0) |
| `uv run pyrefly check` | **0 errors** (exit 0) |
| `uv run pytest -m "not browser"` | **332 passed, 8 deselected in 26.82s** (exit 0) |

Suite counts are identical to the pre-bump baseline (332/8), so nothing was deleted, skipped or
xfailed to reach green.

### Zero-suppression audit (T-20-23)

| Check | Result |
|---|---|
| `git grep -c 'type: ignore' -- src tests` | **no matches** (was 3) |
| `git grep -cE '# (ty\|pyrefly): ignore' -- src tests` | **no matches** |
| `git grep -c 'get_options = lambda' -- tests/test_scanner.py` | **no matches** |
| `git grep -c '_options_impl' -- tests/test_scanner.py` | **6** (≥ 3 required) |
| `git grep -c 'cast(' -- src/saneless/config.py` | **2** (was 1) |
| `pyproject.toml` in the source-fix commit `e7f63b0` | **absent** — no rule disabled, no `per-file-ignores` widened |
| `git diff 262bbf5..HEAD -- src tests \| grep '^+.*(# noqa\|type: ignore\|ty: ignore\|pyrefly: ignore)'` | **NONE ADDED** |

The `# noqa` criterion in the plan (`git grep -cE '# noqa' -- src tests` returns no matches) is **not
literally satisfiable and was never satisfiable** — seven `# noqa` comments pre-date this phase
(`config.py:124,125` `ARG003`, `scanner/__init__.py:23` and `sane_backend.py:47,49` `PLC0415`/`PLW0603`,
`test_cli.py:550` `PLC0415`, `test_web.py:406` `ANN202`). None were touched. The criterion's actual
intent — *none were introduced* — is proven by the diff row above, which is the stronger check anyway:
it inspects added lines rather than the whole tree.

## Publication and CI

Both code commits were cherry-picked onto `autodev-filtered` in a **linked git worktree** under the
session scratchpad (the same technique Plan 04 used), so the main working tree never had to change
branches and the pre-existing ` M .planning/config.json` was never at risk. The worktree was removed
afterwards; `git worktree list` shows only `/home/kris/git/saneless`.

```text
git worktree add "$SCRATCH/filtered-wt" autodev-filtered
git cherry-pick e7f63b0 50929f2      # -> 3fc4c1f, f66d132
git push origin autodev-filtered:autodev-filtered
   99154e2..f66d132  autodev-filtered -> autodev-filtered
git worktree remove "$SCRATCH/filtered-wt"
```

**The push was a fast-forward.** No `--force` and no `--force-with-lease` was used; `git merge-base
--is-ancestor 99154e2 autodev-filtered` returned true before pushing, and the push output shows the
two-dot range rather than a `+` forced update. Re-filtering with `git-filter-repo` was not needed and
was not run.

### D-22 re-verification (the repo is public now, so this is world-readable)

| Check | Command | Result |
|---|---|---|
| Head tree, locally | `git ls-tree -r --name-only autodev-filtered \| command grep -c '^\.planning/'` | **0** |
| Head tree, **on GitHub** | `gh api .../git/trees/f66d132?recursive=1 --jq '[.tree[].path \| select(startswith(".planning"))] \| length'` | **0** |
| New commit `3fc4c1f` | `git diff-tree --no-commit-id --name-only -r` | `src/saneless/config.py`, `tests/test_scanner.py` — **0** `.planning/` |
| New commit `f66d132` | same | `pyproject.toml`, `uv.lock` — **0** `.planning/` |

The remote-side tree check is deliberate rather than redundant: after Plan 04's visibility switch, the
authoritative statement is what GitHub serves, not what the local ref happens to contain.

### The run

| Fact | Value |
|---|---|
| Run | `34488308016` |
| `conclusion` / `event` | **`success`** / **`pull_request`** |
| Run `headSha` | `f66d1321e6eac7ea95bf83e81645beb74d7ceb42` |
| `gh pr view 1 --json headRefOid` | `f66d1321e6eac7ea95bf83e81645beb74d7ceb42` — **identical** |
| Check runs on that SHA | `[{"app":15368,"conclusion":"success","name":"test"},{"app":15368,"conclusion":"success","name":"lint"}]` |
| PR `mergeStateStatus` | `CLEAN` |

Both required contexts of the `master gate` ruleset (`lint@15368`, `test@15368`) report success on the
bumped tree, so the ruleset's requirements are satisfied by this commit and not merely by Plan 03's.
The run's only annotation is a benign `setup-uv` cache-reservation race between the two parallel jobs.

Because Plan 03 proved the workflow green on the locked toolchain and Plan 04 proved a seeded break
turns it red, this green run attributes cleanly to the bump — which is precisely what D-17's ordering
was built to produce.

### Terminal state (D-21)

| Invariant | Value |
|---|---|
| PR #1 | **`OPEN`**, `mergedAt: null` — https://github.com/kdknigga/scanless/pull/1 |
| `refs/heads/master` | `a87b3ddf45b094bb03dedc88a8aade5cd73d33c4` — unchanged |
| Tags | `gh api .../tags` → `0`; `git ls-remote --tags origin` → `0` lines |
| Ruleset | untouched |
| Repo visibility | untouched (still PUBLIC, per Plan 04) |

## Decisions Made

- **Commit order inverted relative to the plan.** See the deviation below.
- **A Protocol, not `Callable[..., Settings]`.** The plan allowed either ("cast the `Settings`
  constructor to a callable type that accepts the private kwarg"). `Callable[..., Settings]` accepts
  *any* arguments, so it would have type-checked the call by discarding the signature — the same safety
  loss as the suppression, just harder to grep for. `_SettingsFactory` accepts `_toml_file` and nothing
  else, so the fix is strictly stronger than the code was before the bump.
- **`_options_impl` typed `list[tuple] | None`, matching `get_options`' own declared return type**
  rather than a narrower literal tuple type, so the delegation needs no cast and the two call sites'
  heterogeneous tuples assign cleanly.
- **Lockfile scope proven by package name, not line count.** 68 changed lines could hide a transitive
  bump; diffing the changed hunks' `name = ` context lines shows exactly `ty` and `pyrefly`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] The plan's commit order cannot be committed — prek's type-checker hooks are `always_run: true`**

- **Found during:** Task 1, at the commit step
- **Issue:** The plan states "Expect this commit alone to leave `ty` red. That is the point of D-17's
  ordering and is not a reason to revert." But this repo has a **prek `pre-commit` hook installed**
  (`.git/hooks/pre-commit`, generated by prek), and `.pre-commit-config.yaml:47-62` declares the
  `ty-checker` and `pyrefly-checker` local hooks with `always_run: true` and `pass_filenames: false`.
  They therefore run on **every** commit regardless of which files are staged. A deliberately-red
  intermediate commit is impossible to create without `--no-verify`, which the execution constraints
  forbid outright. No plan in the phase anticipated this; D-17's "bump as its own commit" reasoning
  predates the hook's relevance being noticed.
- **Fix:** Kept both commits and kept them atomic — inverted their order. `e7f63b0` (source typing
  fixes) lands first, `50929f2` (the bump) second. Every commit in the history is green under the full
  hook suite, no bypass was used, and both plan acceptance criteria still hold at their respective
  commits: `git show --stat e7f63b0` lists exactly `src/saneless/config.py` and `tests/test_scanner.py`,
  and `git show --stat 50929f2` lists exactly `pyproject.toml` and `uv.lock`.
- **Attribution is preserved, which is what D-17 actually protects.** The two diffs remain separately
  bisectable and separately revertable; only their sequence changed. Nothing in D-17's stated purpose —
  "a red run after the bump unambiguously implicates the bump rather than the workflow" — depends on the
  *bump* being the earlier of the two, because CI runs on the pushed branch head, not on each commit.
  The plan's diagnostic capture requirement was satisfied independently and before either commit: the
  verbatim three-diagnostic `ty` output above was taken on the bumped toolchain with the sources still
  untouched, which is the evidence D-17 was really asking for.
- **Files modified:** none beyond the plan's four
- **Commits:** `e7f63b0`, `50929f2`

**2. [Rule 3 - Blocking] `uv add --dev "pkg@latest"` is not valid `uv` syntax**

- **Found during:** Task 1
- **Issue:** The plan's literal command resolved `latest` as a local path
  (`error: Distribution not found at: file:///home/kris/git/saneless/latest`) and briefly added `latest`
  as a workspace member before erroring out. `uv` rolled the file back itself; `git diff pyproject.toml`
  was empty immediately afterwards.
- **Fix:** Read the current versions from the PyPI JSON API (`ty` 0.0.80, `pyrefly` 1.2.0 — both the
  versions RESEARCH.md audited as `[OK]`), then ran `uv add --dev "ty>=0.0.80" "pyrefly>=1.2.0"`. Same
  intended outcome: bounds raised in place, `uv.lock` regenerated, dev list order untouched.
- **Files modified:** `pyproject.toml`, `uv.lock` (Task 1's own files)
- **Commit:** `50929f2`

### Out of scope, not fixed

**`uv run prek run --all-files` exits 1** — solely because `pretty-format-json` wants
`.planning/config.json`'s keys reordered. That file carries a **pre-existing, unrelated modification**
that predates this phase and that the execution constraints explicitly require be left untouched; it was
verified byte-identical before and after this plan (`git diff --stat` unchanged at 25 insertions,
1 deletion). It is also stripped from everything published, so it cannot reach CI. The plan's
`uv run prek run` criterion is satisfied where it matters: prek ran in full on **both** of this plan's
commits as the real `pre-commit` hook and passed every hook, including `ty type checker` and
`pyrefly type checker`.

## Threat Flags

None. The four modified files introduce no network endpoint, no auth path, no file-access pattern and no
schema change. `Settings`' field set is deliberately unchanged (see Site 1), so the configuration trust
boundary is exactly where it was.

## Notes for Future Work

- **Phase 32's toolchain sweep** now has a smaller `ty` delta to absorb, but should expect the same class
  of fallout from `ruff`, `pytest` and `playwright`. The lesson generalises: a checker bump that
  invalidates a suppression syntax surfaces as N diagnostics at exactly the N suppression sites, so
  `git grep -c 'type: ignore'` is a usable pre-bump estimate of the blast radius.
- **The suppression count is now zero and worth keeping there.** A future `# type: ignore` in `src/` or
  `tests/` would be the first in the repo; a CI grep guard for it would be cheap and is a natural
  companion to Phase 31's CI-02 naming guard.
- **`_SettingsFactory` is the documented home for anything else `settings_customise_sources` grows.** If
  a second private init kwarg is ever added, it belongs in that Protocol's `__call__` signature, not in
  a new cast.

## Self-Check: PASSED

- `.planning/phases/20-ci-gate/20-05-SUMMARY.md` — FOUND
- Commit `e7f63b0` — FOUND on `autodev`
- Commit `50929f2` — FOUND on `autodev` (HEAD)
- Commit `3fc4c1f` — FOUND on `autodev-filtered` and on `origin/autodev-filtered`
- Commit `f66d132` — FOUND on `autodev-filtered` and on `origin/autodev-filtered` (PR #1 head)
