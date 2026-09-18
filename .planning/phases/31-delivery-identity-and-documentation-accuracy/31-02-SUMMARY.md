---
phase: 31-delivery-identity-and-documentation-accuracy
plan: 02
subsystem: testing
tags: [pytest, subprocess, ruff, docs, mkdocs, ghcr, packaging]

# Dependency graph
requires:
  - phase: 31-01
    provides: the Phase 31 test section in tests/test_deployment_config.py and the 0.2.0 packaging identity the renamed [project.urls] block sits in
provides:
  - "`test_no_shipped_file_references_the_old_owner` — a repo-wide naming guard whose file set is `git ls-files` minus `.planning/`, so a file added in a later phase is covered without anyone remembering"
  - "24 renamed project URLs across README.md, docker-compose.yml, five docs pages, mkdocs.yml and pyproject.toml — every published reference now names kdknigga"
  - "`_shipped_files()` — a reusable S603-clean tracked-file enumerator other doc-truth tests can scan over"
  - "three README static assertions (--title in the scan example, source spelling read off ProfileConfig, deep links checked against the docs/ tree)"
  - "the three Phase 30 rename-deferral artifacts deleted"
affects: [31-03, 31-04, 31-05, 31-06, 31-07, 31-08, 31-09, 31-10, release, docs-deploy]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "version-control-derived file sets: a guard enumerates what is tracked rather than what someone listed"
    - "runtime string assembly to keep a scanner from matching its own source"
    - "README examples pinned to model defaults and to the filesystem"

key-files:
  created: []
  modified:
    - tests/test_deployment_config.py
    - README.md
    - docker-compose.yml
    - docs/getting-started/first-cli-scan.md
    - docs/getting-started/quick-start.md
    - docs/how-to/deploy-docker-compose.md
    - docs/how-to/scanner-host-discovery.md
    - docs/reference/docker.md
    - mkdocs.yml
    - pyproject.toml

key-decisions:
  - "The forbidden owner slug is assembled from two module constants via an f-string, so the guard is not exempt from its own scan; a `\"-\".join` over an inline tuple would have been folded back into a literal by ruff FLY002"
  - "The binary path is an inline literal at the subprocess call site, not a module constant: ruff S603 treats a Name as untrusted input even when it resolves to a literal, and the plan's `_GIT` constant form did not pass"
  - "The two-type `except` is written as two separate clauses: ruff format rewrites a parenthesised tuple into PEP 758's bracketless form at this project's py314 target, and the pre-commit debug-statements hook runs on an older interpreter that cannot parse it"
  - "The rename, the guard and the Phase 30 deletions are a single commit, because the deleted Phase 30 test asserted the compose file still carried the old name"
  - "`test_readme_scan_example_carries_a_title` asserts every `saneless scan` line carries --title, not merely that the fenced block contains one somewhere — the weaker reading was already green and would not have caught row 16"

patterns-established:
  - "Naming guard: scan every tracked path outside .planning/, report offenders as file:line, assemble the needle at runtime so no file is exempt"
  - "S603-clean child process from a test: literal argv at the call site, repository path via cwd=, always timeout="
  - "Documentation deep links validated against the docs/ tree rather than a hard-coded list"

requirements-completed: [CI-02, DLVR-01, DOCS-02]

# Metrics
duration: ~25min
completed: 2026-09-18
---

# Phase 31 Plan 02: Owner Rename and README Accuracy Summary

**A `git ls-files`-derived naming guard that fails a plain `uv run pytest` if the old GitHub owner reappears anywhere outside `.planning/`, landed in the same commit as the 24-line rename it enforces, plus three README assertions pinned to `ProfileConfig` and to the `docs/` tree.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-09-18T18:56Z (approx; first commit 19:16Z)
- **Completed:** 2026-09-18T19:21Z
- **Tasks:** 2
- **Files modified:** 10

## Accomplishments

- **The identity guard.** `test_no_shipped_file_references_the_old_owner` enumerates every tracked path outside `.planning/` via `git ls-files -z` and reports `file:line` for every occurrence of the old owner slug. It ran RED against the unmodified tree with exactly **29 offenders across 10 files**, matching the research inventory line for line, then GREEN at zero. It carries no marker, so it runs on a plain `uv run pytest` and in CI (CI-02).
- **The rename.** All **24** real references renamed across nine files — 6 in `README.md`, 2 in `docker-compose.yml`, 1 each in `first-cli-scan.md`, `quick-start.md` and `scanner-host-discovery.md`, 2 in `deploy-docker-compose.md`, 5 in `docker.md`, 3 in `mkdocs.yml`, 3 in `pyproject.toml`'s `[project.urls]`. The `authors` entry (which spells the name with a space) and `name = "saneless"` were left alone; the distribution name is unchanged and still pinned by `test_pyproject_distribution_name_is_saneless`.
- **The Phase 30 artifacts deleted, not weakened.** `COMPOSE_KRIS_KNIGGA_COUNT` with its rationale block, `test_phase_30_does_not_perform_the_dlvr_01_rename`, and the module-docstring sentence that named the old account are gone. The diff of removed lines in the test module contains those three artifacts and nothing else — no pre-existing assertion was touched.
- **README rows 16, 17, 18 corrected and defended.** The bare `saneless scan` example now shows `--title "Some Document"`; `source = "flatbed"` became `"Flatbed"`, the spelling read off `ProfileConfig.model_fields["source"].default`; the tutorial link now points at `getting-started/first-cli-scan/`, the page that exists. The other four deep links were checked by the same test and all resolve.

## Task Commits

1. **Task 1: the naming guard, the rename and the Phase 30 deletions** — `3657afa` (feat) — one commit, 10 files, 117 insertions / 47 deletions
2. **Task 2 (RED): the failing README accuracy tests** — `16800aa` (test)
3. **Task 2 (GREEN): the README corrections** — `3eb1ce9` (fix)

_TDD gate sequence: task 1's RED was observed in the working tree and necessarily squashed with its GREEN (see Decisions); task 2 has a discrete `test(...)` → `fix(...)` pair._

## Files Created/Modified

- `tests/test_deployment_config.py` — new `Phase 31: the identity guard` section: `FORBIDDEN_OWNER_SLUG` assembled from `_OWNER_GIVEN`/`_OWNER_FAMILY`, `_shipped_files()`, the guard test, and the three README tests. Three Phase 30 artifacts removed; module docstring updated.
- `README.md` — 6 URLs renamed; `--title` added to the scan example (with the usage block's trailing comments re-aligned); `source = "Flatbed"`; tutorial deep link repointed.
- `docker-compose.yml` — image reference and the `#configuration` anchor link.
- `docs/getting-started/first-cli-scan.md`, `docs/getting-started/quick-start.md`, `docs/how-to/deploy-docker-compose.md`, `docs/how-to/scanner-host-discovery.md`, `docs/reference/docker.md` — image references.
- `mkdocs.yml` — `site_url`, `repo_url`, `repo_name`.
- `pyproject.toml` — `[project.urls]` Homepage / Repository / Issues.

## Decisions Made

- **One commit for the rename, the guard and the deletions.** The deleted Phase 30 test asserted the compose file *still* carried the old name while the new guard asserts it does not; no other ordering leaves the suite green at every commit. The RED observation for task 1 was therefore made in the working tree (29 offenders, listed above) rather than recorded as its own commit.
- **The scan-example test asserts *every* `saneless scan` line carries `--title`.** The plan's looser phrasing ("the fenced block containing `saneless scan` includes `--title`") was already satisfied by the neighbouring `--profile duplex --title "Invoice"` line and would never have gone RED. The stricter reading matches the plan's own acceptance criterion and actually catches row 16.
- **Link text updated with the link.** "Scan Your First Document" became "Your First CLI Scan", so the entry names the page it now points at.
- **No maturity prose, no hedges** were added anywhere (D-07 respected).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `ruff S603` rejects a module constant as argv[0]**
- **Found during:** Task 1
- **Issue:** The plan (and RESEARCH §8's prototype) specified a module constant `_GIT = "/usr/bin/git"` used as `[_GIT, "ls-files", "-z"]`. Ruff flagged this: `S603 subprocess call: check for execution of untrusted input`. S603 stands down only when every argv element is a string literal **at the call site**; a `Name` is untrusted input to the rule even when it resolves to a literal. Suppression is forbidden by CLAUDE.md and by the plan.
- **Fix:** Inlined the absolute binary path as a literal in the argv list, which is exactly the shape `tests/test_atomic_write.py:711-717` and `tests/test_scanner.py:2623-2634` already use. The repository path still travels via `cwd=REPO_ROOT`, as the plan requires. The unused constant was removed.
- **Files modified:** `tests/test_deployment_config.py`
- **Verification:** `uv run ruff check .` → All checks passed, with no `# noqa` anywhere.
- **Committed in:** `3657afa`

**2. [Rule 3 - Blocking] `D413` on the `_shipped_files` docstring**
- **Found during:** Task 1
- **Issue:** `D413 Missing blank line after last section ("Returns")`.
- **Fix:** Added the blank line before the closing quotes.
- **Files modified:** `tests/test_deployment_config.py`
- **Verification:** `uv run ruff check .` clean.
- **Committed in:** `3657afa`

**3. [Rule 3 - Blocking] `ruff format` and the `debug-statements` hook disagree about PEP 758**
- **Found during:** Task 1 (the first `git commit` attempt aborted)
- **Issue:** `except (UnicodeDecodeError, OSError):` was rewritten by `ruff format` into PEP 758's bracketless `except UnicodeDecodeError, OSError:` — correct for `target-version = "py314"`, and the only such construct in the repository. The `debug-statements` pre-commit hook runs on its own bundled Python 3.12 environment and died with `SyntaxError: multiple exception types must be parenthesized`, failing the commit.
- **Fix:** Split into two separate `except` clauses with identical bodies. Both readings are identical, ruff format leaves single-type clauses alone, and the older interpreter parses them. A rationale comment in the house form (what the reader would assume → why it is wrong → what happens if you change it back) sits above the `try`. `.pre-commit-config.yaml` was *not* touched — it is outside this plan's file set and pinning a hook's interpreter version is a change another agent may own.
- **Files modified:** `tests/test_deployment_config.py`
- **Verification:** `uv run prek run --all-files` and `uv run prek run --stage pre-push --all-files` both fully green; commit succeeded with all hooks passing.
- **Committed in:** `3657afa`

**4. [Rule 1 - Cosmetic correctness] Usage-block comment alignment**
- **Found during:** Task 2
- **Issue:** Adding `--title "Some Document"` to line 44 pushed that line past the column the block's trailing comments were aligned to, leaving the README's Usage block visibly ragged.
- **Fix:** Re-aligned all four trailing comments to one column. The `# Scan a document` comment style the plan asked to keep is preserved.
- **Files modified:** `README.md`
- **Verification:** Block read back; the three README tests pass.
- **Committed in:** `3eb1ce9`

---

**Total deviations:** 4 auto-fixed (3 blocking, 1 cosmetic). **No scope creep** — every one was required to get the plan's own code past the project's mandatory quality gates with no suppressions.

**Impact on plan:** None on substance. Deviations 1 and 3 mean two of the plan's literal code shapes (the `_GIT` module constant, the parenthesised two-type `except`) could not be used as written; both were replaced with forms that satisfy the same stated constraints — literal argv, `cwd=`-borne path, no suppression.

## Issues Encountered

- **Serena MCP is rooted at the main checkout, not this worktree.** The first two edits (the `subprocess` import and the guard block) were applied to `/home/kris/git/saneless/tests/test_deployment_config.py` instead of the worktree copy; `pytest` collecting 52 tests with none matching `-k old_owner` surfaced it immediately. Both edits were reverted through Serena and the main checkout's file verified byte-identical to the worktree's (`diff` → identical, 1182 lines, working tree clean). All subsequent edits were made with absolute-path scripts against the worktree. **Serena symbol edits are unsafe in this worktree setup until its project root is reconfigured** — worth recording for the sibling and later agents.
- The RED run for task 1 reproduced the research inventory exactly (29 offenders, 10 files, same line numbers modulo plan 31-01's insertions), which is the evidence that the file set and the needle are right.

## Known Stubs

None.

## Threat Flags

None — no new network endpoint, auth path, file-access pattern or schema change. The one new trust-boundary crossing (the `git ls-files` child process) is the mitigation T-31-16 called for and is implemented to its stated shape: literal argv, `cwd=`, `check=True`, `timeout=30`, no shell.

## Verification

| Check | Result |
|-------|--------|
| `uv run pytest -m "not browser and not sane_hardware" -q` | 3077 passed, 130 deselected |
| `uv run pytest tests/test_deployment_config.py -q` | 55 passed (52 before: −1 deleted, +4 new) |
| old slug in tracked files outside `.planning/` | 0 occurrences across 119 files |
| `uv run ruff check .` | All checks passed (no `# noqa` anywhere) |
| `uv run ruff format --check .` | 63 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --all-files` | all hooks passed |
| `uv run prek run --stage pre-push --all-files` | all hooks passed |

## User Setup Required

None — no external service configuration required. No tag was created; per the standing project rule, tags are the user's alone.

## Next Phase Readiness

- Every published URL now names the current owner, so the GHCR reference in the compose template and the docs-site links in `mkdocs.yml` and `README.md` are consistent for whichever plan wires up the release and docs workflows.
- `_shipped_files()` is available to any later doc-truth test that needs "every tracked file".
- **For sibling and later agents:** the two-clause `except` in the guard is load-bearing — a well-meaning "simplification" back to a tuple will be reformatted into PEP 758 syntax and break the `debug-statements` hook. Likewise, folding `FORBIDDEN_OWNER_SLUG` back into one literal makes the guard report its own source.
- STATE.md and ROADMAP.md were deliberately **not** touched; the orchestrator owns those writes.

---
*Phase: 31-delivery-identity-and-documentation-accuracy*
*Completed: 2026-09-18*

## Self-Check: PASSED

All modified files present on disk; all three task commits present in the branch history (`3657afa`, `16800aa`, `3eb1ce9`).
