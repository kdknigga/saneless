---
phase: 20-ci-gate
plan: 02
subsystem: infra
tags: [git-filter-repo, github, repo-visibility, secret-sweep, publication, ci]

# Dependency graph
requires:
  - phase: 20-01
    provides: ".github/workflows/ci.yml, .github/dependabot.yml and CONTRIBUTING.md in the tree -- the filtered branch head must already carry the workflow or the PR has nothing to trigger"
provides:
  - "kdknigga/saneless visibility = PRIVATE, read back as true, with the remote still holding zero refs"
  - "Local branch autodev-filtered: 112 commits based on master's original root a87b3dd, zero .planning/ paths in any commit, unpushed"
  - "A fresh credential sweep over all 331 tracked files, clean"
  - "Authorization for Plan 03 to push and open the PR, and for Plan 04 to apply the ruleset -- auto-approved, provenance recorded below"
affects: [20-03 push and PR, 20-04 ruleset and seeded break, 20-05 toolchain bump, 31-delivery-and-identity]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "git-filter-repo is run ephemerally via uvx on a --no-local scratch clone, never on the working repo"
    - "History rewrites use --refs <base>..<branch> --partial so the PR base commit keeps its original SHA and signature"
    - "Verification greps use `command grep` -- the interactive shell's `grep` is a ugrep wrapper with a broken -qv exit status"

key-files:
  created: []
  modified: []

key-decisions:
  - "Filtered branch named autodev-filtered -- names its provenance (autodev with .planning stripped) rather than the phase"
  - "filter-repo run as --refs master..autodev --partial, not over whole history, to preserve the signed root commit a87b3dd as the PR merge base"
  - "The 112-commit and 346-commit figures supersede the plan's stale 109/335 constants; both deltas are Plan 01's own commits"
  - "Plan 04 must target refs/heads/master explicitly -- the repo has no default branch because it has no refs"

patterns-established:
  - "Every outward-facing irreversible step is preceded by a read-back of the reversible one (visibility before push)"
  - "History-rewrite correctness is proven by arithmetic reconciliation (dropped + kept == original), not by a plausible-looking count"

requirements-completed: [CI-01]

# Metrics
duration: 10min
completed: 2026-09-10
---

# Phase 20 Plan 02: Pre-Publication Gate Summary

**`kdknigga/saneless` switched PUBLIC to PRIVATE while its remote still held zero refs, and a 112-commit `autodev-filtered` branch built with `git-filter-repo` on a throwaway clone -- every `.planning/` blob gone from every commit, the signed root commit `a87b3dd` intact as the PR base, and nothing pushed.**

## Performance

- **Duration:** 10 min
- **Started:** 2026-09-10T13:17:04Z
- **Completed:** 2026-09-10T13:27:00Z
- **Tasks:** 3 (2 automated, 1 blocking checkpoint)
- **Files modified:** 0 in the working tree (one GitHub setting, one local git ref)

## Accomplishments

- `kdknigga/saneless` is **private** (`{"isPrivate":true,"visibility":"PRIVATE"}`), changed while `git ls-remote origin` still returned zero refs -- the D-23 window was met, not merely hoped for.
- Local branch **`autodev-filtered`** (head `99154e2`) exists with **112 commits**, **zero `.planning/` paths in its head tree and in all 112 commit diffs**, and `git merge-base master autodev-filtered` = `a87b3dd` so the pull request has a real base.
- The filter changed **no published file**: the full `git ls-tree -r` listing of `autodev-filtered` and of `autodev` with `.planning/` excluded is byte-identical -- same paths, same modes, same blob SHAs.
- The working repo was **not** rewritten: 346 commits on `autodev` before and after, `origin` still configured, `master` still `a87b3dd`, zero tags. The scratch clone is deleted.
- A fresh two-pass credential sweep over all 331 tracked files found nothing.

## Task Commits

Tasks 1 and 2 changed no file in the working tree by design -- their effects are one GitHub repository setting and one local git ref -- so neither produced a commit. Task 3 is a checkpoint. This plan's only commit is its metadata commit.

1. **Task 1: Switch the repository to private and re-verify no secrets** - no commit (GitHub setting only)
2. **Task 2: Build the .planning-stripped branch locally** - no commit (local ref `autodev-filtered` only)
3. **Task 3: Approve the outward-facing publication sequence** - checkpoint, auto-approved (see below)

## Files Created/Modified

None. `git status --porcelain` reports only the pre-existing ` M .planning/config.json`, which predates this phase and was left untouched.

## The filtered branch

**Branch:** `autodev-filtered` — head `99154e2` (`docs(20-01): add root CONTRIBUTING.md describing the CI gate`)
**Commits:** 112 (`git rev-list --count master..autodev-filtered`)
**Base:** `a87b3ddf45b094bb03dedc88a8aade5cd73d33c4`, the original `master` root commit

### The exact invocation used

```bash
git clone --no-local file:///home/kris/git/saneless "$SCRATCH/filter-clone"
cd "$SCRATCH/filter-clone"
git branch master origin/master
uvx git-filter-repo --path .planning --invert-paths --refs master..autodev --partial --force
cd /home/kris/git/saneless
git fetch "$SCRATCH/filter-clone" autodev:autodev-filtered
rm -rf "$SCRATCH/filter-clone"
```

**Plan 03, and anyone who ever re-filters this repo, must use the `--refs master..autodev --partial` form.** The plain whole-history invocation the plan specified (`uvx git-filter-repo --path .planning --invert-paths --force`) was run first and **discarded**, because it silently rewrote the root commit:

- `master` came out as `18d7911d1e50988cf35868ccc099559dc22f7a72` instead of `a87b3ddf45b094bb03dedc88a8aade5cd73d33c4`.
- Cause: `a87b3dd` carries an SSH `gpgsig` header. `git-filter-repo` round-trips history through `fast-export`/`fast-import`, which does not carry commit signatures. Comparing the two objects shows an identical `tree e994558…`, identical `author`/`committer` lines and identical message -- the *only* difference is the missing `gpgsig` block, and that alone changes the SHA.
- Consequence had it been pushed: `master` (`a87b3dd`, pushed from the working repo) and the filtered branch (rooted at `18d7911`) would have had **no merge base**, and `gh pr create --base master` would have had nothing coherent to compare.

`--refs master..autodev` makes `master` a boundary commit that filter-repo copies rather than rewrites, so its SHA and signature survive. The 112 commits above it are unsigned; that is inherent to any history rewrite and was not worked around.

### `.planning/` counts (the D-22 acceptance test)

| Check | Command | Result |
|---|---|---|
| Head tree | `git ls-tree -r --name-only autodev-filtered \| command grep -c '^\.planning/'` | **0** |
| Every commit | loop of `git diff-tree --no-commit-id --name-only -r` over all 112, piped to `command grep -c '^\.planning/'` | **0** |
| Head file count | `git ls-tree -r --name-only autodev-filtered \| wc -l` | 82 (vs 331 on `autodev`; 249 are `.planning/`) |
| `ci.yml` + `dependabot.yml` | `command grep -cE '^\.github/(workflows/ci\.yml\|dependabot\.yml)$'` | **2** |
| `CONTRIBUTING.md` | `command grep -c '^CONTRIBUTING.md$'` | **1** |
| Empty commits | commits in `master..autodev-filtered` with a zero-file diff | **0** |

Stronger than the plan required: `git diff --stat autodev-filtered:src autodev:src` and the same for `tests` are both empty, and `diff` of the two branches' full `git ls-tree -r` output with `.planning/` rows removed is **identical** -- paths, modes and blob SHAs. Metadata preservation was spot-checked on 6 commits spread across the history (oldest, ~20th, ~45th, ~70th, ~95th, newest): subject, author name and email, author-date and committer-date all match their `autodev` originals exactly. This is preserved history, not a squash (D-25).

### Proof the working repo was not rewritten

```text
git rev-list --count autodev   -> 346   (captured before filter-repo ran; identical after)
git rev-parse master           -> a87b3ddf45b094bb03dedc88a8aade5cd73d33c4
git remote -v                  -> origin  git@github.com:kdknigga/saneless.git (fetch/push)
git branch --show-current      -> autodev
git tag | wc -l                -> 0
scratch clone directory        -> deleted
```

`origin` still being configured is the decisive tell: `git-filter-repo` removes the remote wherever it runs, and it printed exactly that notice inside the scratch clone. It never ran here.

Note for anyone re-running this check later: `autodev` reads **347** after this plan's own metadata commit
(`docs(20-02): complete pre-publication gate plan`) lands on it. 346 is the figure that brackets the
filter-repo window -- captured immediately before it ran and identical immediately after.

### Reconciling 112 against the plan's "~109", and 346 against the plan's "335"

Both plan constants are stale measurements from 2026-09-09, not deviations:

| | CONTEXT / plan (2026-09-09) | Measured now |
|---|---|---|
| `git rev-list --count autodev` | 335 | **346** |
| `master..autodev` total | 338 | **345** |
| of those, `.planning/`-only | 229 | **233** |
| code-touching (= filtered count) | 109 | **112** |

Plan 01 and the phase-20 planning work landed 7 commits into `master..autodev` in between: 3 code commits (`bef7ab4`, `38541c9`, `a8f9d97`) and 4 planning-only ones. 229 + 4 = 233 and 109 + 3 = 112. The correctness argument is the arithmetic, not the resemblance to a predicted number: **233 dropped + 112 kept = 345 original**, exactly, with zero empty commits left behind. filter-repo dropped precisely the commits that became empty.

## Repository state, for Plan 04

```text
gh repo view kdknigga/saneless --json isPrivate,visibility  -> {"isPrivate":true,"visibility":"PRIVATE"}
gh repo view kdknigga/saneless --json defaultBranchRef      -> {"defaultBranchRef":{"name":""}}
git ls-remote origin                                        -> no refs (0 lines)
```

**`defaultBranchRef` is empty** because the repository has no refs at all. Plan 04 must target **`refs/heads/master` explicitly** in the ruleset body and cannot rely on a default-branch alias or on `~DEFAULT_BRANCH`. The default branch does not exist until Plan 03's first `git push origin master` creates it.

No repository setting other than visibility was touched -- not Actions permissions, not the default branch, not branch protection.

## Secret sweep (D-24, re-verified against the current tree)

- Token-shaped regex sweep over all 331 tracked files (`gh[pousr]_[A-Za-z0-9]{20,}`, `sk-[A-Za-z0-9]{20,}`, `AKIA[0-9A-Z]{16}`, `BEGIN [A-Z ]*PRIVATE KEY`) -> **0 matching files**.
- Wider second sweep over just the 82 files that will actually be published (`xox[baprs]-`, `-----BEGIN`, `glpat-`, `AIza…`, `secret|token|password = "…"`) -> 4 hits, all documentation placeholders: `"your-paperless-api-token"` (x2), `"abc123def456ghi789"`, `"your-api-token-here"`.
- `PASSWORD=|API_KEY=|ghp_` -> 3 hits, all benign: two are `20-02-PLAN.md` quoting the regex itself (stripped by the filter anyway), one is `uv.lock`'s `ghp_import` PyPI package, an mkdocs dependency.
- `git ls-files --error-unmatch saneless.toml` -> `did not match any file(s) known to git`; `git check-ignore -v saneless.toml` -> `.gitignore:312`. The real config with the real token is on disk, ignored, untracked.
- `git ls-files | command grep -c '^site/'` -> **0**. No `.pem`, `.key`, `.p12`, `.env` or `id_rsa` files are tracked.

**Result: clean.** Nothing blocked the checkpoint on D-24 grounds.

## Task 3 -- approval provenance and the open `bypass_actors` question

**No human reply was given, and none is quoted here.**

The checkpoint was **auto-approved by the orchestrator** under `workflow.auto_advance`. Auto-mode was active for this phase, and the user had selected "Auto-approve as configured" for this phase's blocking checkpoints when asked at phase start. The approval is therefore an automated decision derived from a standing configuration choice -- not a verbatim statement by the user about this specific evidence.

**Authorized by that approval:**

- Plan 03 may push `master`, push `autodev-filtered`, and run `gh pr create --base master --head autodev-filtered`.
- Plan 04 may apply the branch ruleset and run the deliberately-red seeded break.

**`bypass_actors: []` -- UNANSWERED / PENDING.** The user has **not** answered whether an empty bypass list on the `master` ruleset is acceptable. This is recorded as neither approved nor declined. The orchestrator will put the question to the user directly before Plan 04 is dispatched.

> **Plan 04 must treat the ruleset bypass list as an open input and must not read a settled answer out of this SUMMARY.** The consequence still needing an answer: with `bypass_actors: []`, every future change to `master` -- including the user's own, including as repository admin -- must go through a pull request with both CI checks green, with no escape hatch for anyone.

The unmerged-PR half of the checkpoint is not in question: **the pull request will not be merged by any plan, task or agent (D-21).** The phase's terminal state is an open PR handed to the user.

## Decisions Made

- **Branch named `autodev-filtered`.** The plan left `<PR_BRANCH>` unspecified. The name states its provenance -- `autodev` with `.planning/` stripped -- which is more useful on a PR that carries the entire project history than a phase-derived name like `ci-gate` would be.
- **`--refs master..autodev --partial` over whole-history filtering** (see the gpgsig finding above). This also means the scratch clone kept its `origin` remote, which is harmless in a throwaway directory.
- **The filtered branch was fetched, not cherry-picked or rebased, into the working repo** (`git fetch <scratch> autodev:autodev-filtered`). That is purely additive to the object store and cannot rewrite anything.
- **No fallback to squashing or cherry-picking was considered at any point**, including when the first filter-repo run misbehaved. D-25 reserves that choice for the user.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] The plan's filter-repo invocation silently destroyed the PR merge base**

- **Found during:** Task 2 (Build the .planning-stripped branch locally)
- **Issue:** `uvx git-filter-repo --path .planning --invert-paths --force`, exactly as the plan specified, rewrote the root commit from `a87b3dd` to `18d7911`. The plan asserted "`master` is a single root commit with no `.planning/` in its tree, so it is unaffected and remains the PR base" -- that assertion is false, because filter-repo's `fast-export` round-trip drops the commit's SSH `gpgsig` header and the SHA changes with it. Pushing local `master` alongside a branch rooted at `18d7911` would have produced two unrelated histories and a pull request with no merge base.
- **Fix:** Discarded that clone entirely, re-cloned, and re-ran as `uvx git-filter-repo --path .planning --invert-paths --refs master..autodev --partial --force`, which treats `master` as a boundary and leaves it byte-identical, signature included.
- **Files modified:** none (scratch clone only)
- **Verification:** `git rev-parse master` = `a87b3dd…` in the scratch clone after filtering; `git merge-base master autodev-filtered` = `a87b3dd…` in the working repo; oldest filtered commit's parent = `a87b3dd`.
- **Committed in:** n/a -- no working-tree change

**2. [Rule 1 - Bug] `grep` in this shell is a ugrep wrapper whose `-qv` exit status is wrong, corrupting the commit audit**

- **Found during:** Task 2, while reconciling the filtered commit count against the plan's expectation
- **Issue:** `grep` resolves to a shell *function* wrapping `ugrep` via the Claude Code binary. `grep -qv PATTERN` returns exit 1 even when a non-matching line is present -- demonstrated with `printf '.planning/a\ndocs/PRD.md\n' | grep -qv '^\.planning/'` returning 1 while the same command without `-q` prints `docs/PRD.md` and returns 0. Every classification loop built on `grep -qv` was therefore wrong: the audit reported 105 code-touching commits against a filtered branch that plainly held 112, and 7 commits (including `docs: map existing codebase`, which adds `docs/PRD.md`) were misfiled as `.planning/`-only. Chasing that 7-commit gap is what exposed the wrapper.
- **Fix:** Re-ran every verification -- the commit classification, both `.planning/` counts, the `ci.yml`/`CONTRIBUTING.md` presence checks and the full credential sweep -- using `command grep` to bypass the function.
- **Files modified:** none
- **Verification:** With `command grep`, 233 + 112 = 345 reconciles exactly against `git rev-list --count master..autodev`, and the filtered branch's own commit count independently confirms 112.
- **Committed in:** n/a -- no working-tree change

---

**Total deviations:** 2 auto-fixed (1x Rule 3 blocking, 1x Rule 1 bug -- both in verification/tooling, neither in project source)
**Impact on plan:** No scope creep. Deviation 1 prevented an unrecoverable publication defect; deviation 2 invalidated and then re-established every numeric claim in this SUMMARY. Both belong in the phase record because Plan 03 re-runs the filter and any future GSD execution in this repo inherits the `grep` hazard.

## Issues Encountered

The plan's `<verify>` blocks are themselves written with `grep -qv` / `grep -c` and would have produced the same wrong answers if trusted as-written. They were re-executed with `command grep`. Plan 03's and Plan 04's verification blocks should be read with the same caution.

The plan's numeric acceptance criteria (`335` commits on `autodev`, `~109` on the filtered branch) are stale rather than wrong-in-kind; they are reconciled above rather than treated as failures.

## Threat Model Coverage

- **T-20-06** (Info disclosure, publishing while PUBLIC): **mitigated** -- `git ls-remote origin` returned 0 refs before the visibility change, the change was read back as `isPrivate: true`, and the remote was re-checked as empty afterwards. No hard-stop condition was triggered.
- **T-20-07** (Info disclosure, `.planning/` reaching the remote): **mitigated** -- 0 `.planning/` paths in the head tree *and* across all 112 commit diffs, so no blob exists in the published history, not merely in its tip.
- **T-20-08** (Info disclosure, embedded credentials): **mitigated** -- two-pass sweep over all 331 tracked files plus a targeted pass over the 82 publishable ones; `saneless.toml` untracked and ignored; `site/` uncommitted.
- **T-20-09** (Repudiation, publication without the user's knowledge): **partially mitigated** -- the checkpoint fired and enumerated exactly what will be pushed, but it was resolved by automated approval rather than by a human reading the evidence. Nothing has been pushed, so the mitigation is intact for now; the residual risk transfers to Plan 03.
- **T-20-10** (EoP, admin-scope token use): **accepted as planned** -- one setting on one repo, reversible.

No new security surface beyond the register, so there are no threat flags.

## Known Stubs

None.

## User Setup Required

None in this plan.

**One question is outstanding and must be answered before Plan 04 runs:** whether `bypass_actors: []` on the `master` ruleset is acceptable. See the Task 3 section above.

## Next Phase Readiness

- **Plan 03 is unblocked.** `autodev-filtered` exists locally with a valid merge base against `master`, and the repo is private. The three commands are `git push origin master`, `git push origin autodev-filtered`, `gh pr create --base master --head autodev-filtered`. Plan 03 must not merge (D-21).
- **Plan 03 caveat:** if it re-filters for any reason, it must use the `--refs master..autodev --partial` form documented above, or it will reproduce the broken merge base.
- **Plan 04 caveat:** target `refs/heads/master` explicitly; there is no default branch until Plan 03 pushes. And the `bypass_actors` list is an open input, not a settled decision.
- Nothing was pushed, no pull request was created, no merge was performed, and no tag was created.

---
*Phase: 20-ci-gate*
*Completed: 2026-09-10*

## Self-Check: PASSED

`.planning/phases/20-ci-gate/20-02-SUMMARY.md` exists on disk. This plan produced no task commits by
design (Tasks 1 and 2 change no working-tree file), so there are no commit hashes to verify; the two
artifacts it claims -- the private repo setting and the local ref `autodev-filtered` (`99154e2`) --
were each re-read after the fact and both check out, with `git ls-remote origin` still returning 0 refs.
