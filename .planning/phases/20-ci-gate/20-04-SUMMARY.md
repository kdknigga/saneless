---
phase: 20-ci-gate
plan: 04
subsystem: infra
tags: [github-rulesets, ci, merge-gate, branch-protection, seeded-break, bypass-actors]

# Dependency graph
requires:
  - phase: 20-03
    provides: "The verbatim required contexts `lint` and `test` with integration_id 15368, PR #1 open on master, and the warning to exclude copilot-pull-request-reviewer"
  - phase: 20-01
    provides: ".github/workflows/ci.yml on the PR head -- without it the seeded break has nothing to redden"
provides:
  - "Active branch ruleset `master gate` (id 22777879) on refs/heads/master requiring lint@15368 and test@15368, plus deletion and non_fast_forward"
  - "A repository-admin bypass actor (RepositoryRole id 5, bypass_mode always), resolved and named via the GraphQL API"
  - "End-to-end proof the gate blocks: green PR #1 mergeStateStatus CLEAN vs red PR #2 mergeStateStatus BLOCKED"
  - "Red run 34486527413 (event=pull_request, conclusion=failure) from one single-line seeded break, now fully removed"
affects: [20-05 toolchain bump, 31-delivery-and-identity]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Bypass-actor ids are resolved to human-readable role names through GraphQL (repositoryRoleName), never assumed from community lore"
    - "An admin-scope settings write is preceded by printing the literal request body and its operational consequence to the transcript"
    - "A merge gate is proven by a state DIFFERENCE (CLEAN vs BLOCKED on two live PRs), not by the settings read-back alone"
    - "A deliberately-broken throwaway commit is built in a linked git worktree so the main working tree is never contaminated"

key-files:
  created:
    - ".planning/phases/20-ci-gate/20-04-SUMMARY.md"
  modified: []

key-decisions:
  - "bypass_actors is NOT [] -- the user granted the repository-admin role an always bypass, overriding the plan's D-03 default"
  - "The repository is PUBLIC, not private -- rulesets are unavailable on private free-plan repos, so D-23 and D-03 were mutually unsatisfiable"
  - "The seeded break targets pytest, not ruff -- the local ruff --fix hook would have silently deleted an unused-import break"
  - "The optional pull_request rule type was NOT added; the add-now/Phase-31/never question has no human answer and is escalated"

patterns-established:
  - "Probe a settings API's validator with a deliberately-invalid value first, so a later acceptance is known to be meaningful rather than ignored"

requirements-completed: [CI-01]

# Metrics
duration: 9min
completed: 2026-09-10
---

# Phase 20 Plan 04: Apply the Merge Gate and Prove a Failure Propagates Summary

**The `master gate` ruleset is active on `refs/heads/master` requiring exactly `lint@15368` and `test@15368`, and the gate was proven not by its own read-back alone but by a live state difference — the green pull request reports `mergeStateStatus: CLEAN` while a one-line seeded break's pull request reported `BLOCKED` — after which every trace of the break was deleted, PR #1 is still open and unmerged, and `master` is still `a87b3dd`.**

## Performance

- **Duration:** 9 min
- **Started:** 2026-09-10T13:56:45Z
- **Completed:** 2026-09-10T14:05:48Z
- **Tasks:** 3 (2 automated, 1 checkpoint auto-resolved)
- **Files modified:** 0 in the working tree (one ruleset, one throwaway branch and PR, both deleted)

## Task Commits

Tasks 1 and 3 change no working-tree file by design. Task 2's only commit (`efe96a5`) lived on the
throwaway branch `seeded-break-d08-3`, which was deleted locally and remotely as the task required —
so it is deliberately absent from every surviving ref. This plan's only surviving commit is its
metadata commit.

1. **Task 1: Apply the master branch ruleset and read it back** — no commit (GitHub settings only)
2. **Task 2: Seed one single-line break and prove the run goes red** — `efe96a5`, intentionally destroyed
3. **Task 3: Confirm the merge gate (checkpoint)** — no commit

## Files Created/Modified

None in the working tree. `git status --porcelain` reports only the pre-existing
` M .planning/config.json`, which predates this phase and was left untouched, exactly as in Plans 02
and 03.

---

## Authorization provenance and the two user answers folded in

The blocking checkpoint in Plan 20-02 Task 3 was **auto-approved by the orchestrator under
`workflow.auto_advance`**, a standing configuration choice made at phase start. **No verbatim human
reply exists and none is quoted anywhere in this SUMMARY.**

However, two substantive inputs *were* answered by the user directly during this run, and both
override the plan text where they conflict:

| Open input | Plan text | User's answer, applied here |
|---|---|---|
| `bypass_actors` | D-03 / RESEARCH default of `[]` | **Not empty.** Grant the repository admin (`kdknigga`) a bypass so an emergency path to `master` exists. Enforcement stays `active`, never `evaluate`. |
| Repository visibility | D-23: private before the first push | **Public.** Rulesets are not available on private free-plan repos; the user chose public over paying for GitHub Pro or accepting no merge gate at all. |

Plan 03's SUMMARY recorded `bypass_actors` as UNANSWERED and required Plan 04 to get a direct answer
before applying the ruleset. That condition is satisfied: the answer above is a real user decision,
not an inference from the auto-approval.

## PLANNING DEFECT: D-23 and D-03 are mutually unsatisfiable on a free plan

This is recorded as a genuine Phase 20 planning defect, not as an execution deviation, because no plan
in the phase anticipated it.

- **D-23** mandates the repository be **private** before the first push, asserting flatly that
  "CI, rulesets, and Actions all work on private repos."
- **D-03** mandates that the merge gate be delivered by **applying a branch ruleset via `gh api`**.

On this free-plan, user-owned account those two decisions cannot both hold. While the repository was
private, **both** `repos/{repo}/rulesets` and the legacy `repos/{repo}/branches/master/protection`
returned **HTTP 403 — "Upgrade to GitHub Pro or make this repository public"**. Rulesets are simply
not available on private free-plan repositories; the D-23 assertion is wrong for this account tier.

RESEARCH's Pitfall 12 did anticipate a 403 on `POST /rulesets`, but diagnosed it as a **token-scope**
problem and offered token-shaped fallbacks (a fine-grained PAT with `Administration: write`, or a UI
click-path). Neither would have worked — the token was never the issue, the plan tier was. An executor
following Pitfall 12 literally would have burned the phase re-authenticating against a wall.

**Resolution taken by the user:** make the repository public. Visibility was **not** changed by this
plan and must not be changed back — doing so would silently delete the merge gate this plan just built.

Consequences now live, which Phase 31 should carry forward:

- The `master` and `autodev-filtered` histories are **publicly readable**. Because that changes the
  blast radius of D-22/T-20-11, the `.planning/` exclusion was **re-verified after the switch** rather
  than inherited from Plan 03's private-repo verification: GitHub's trees API reports **0** paths
  beginning `.planning` on `a87b3dd` and **0** on `99154e2`.
- D-24's "no secrets are published" finding was made when the repo was private and is now load-bearing
  for a public repository. It still holds (`saneless.toml` gitignored and untracked, no key-shaped
  strings tracked), but it now deserves Phase 31's attention rather than a footnote's.
- Actions minutes on public repositories are unmetered, so the D-05 two-job layout costs nothing.

---

## Task 1 — the ruleset

### Pre-POST disclosure (printed to the transcript before any write)

The plan requires the literal request body and a one-line statement of the `bypass_actors`
consequence to be shown **before** the admin-scope write. Both were printed. Because the user's answer
changed `bypass_actors` away from the plan's default, the write was staged in two steps so the change
was visible as a diff rather than as a fait accompli:

- **Step 1 body** — `bypass_actors: []`. Stated consequence: *"with `[]`, every future change to
  `master` — including the repository owner's own — must go through a pull request with both `lint`
  and `test` green, with no escape hatch for anyone."*
- **Step 2 body** — `bypass_actors: [{actor_id: 5, actor_type: RepositoryRole, bypass_mode: always}]`.
  Stated consequence: *"the repository-admin role (which the owner `kdknigga` holds) keeps an emergency
  path to `master`; everyone and everything else is still gated. This is the user's explicit override
  of the plan's D-03 default of `[]`."*

`gh api repos/kdknigga/scanless/rulesets` returned `[]` immediately beforehand, so no pre-existing
ruleset was replaced and no second ruleset was created.

### Resolving the bypass actor from the API rather than guessing

The plan said to use "the bypass actor the user asked for" without saying how to identify it, and
GitHub's REST documentation **does not publish** the numeric ids of repository roles (this is an open
documentation request, `github/rest-api-description#4406`). Three independent steps were used instead
of a hardcoded guess:

**1. Prove the validator is real** — a deliberately-invalid id was submitted first, so that a later
acceptance would be known to be meaningful rather than silently ignored:

```text
POST /repos/kdknigga/scanless/rulesets   {"actor_id": 999999, "actor_type": "RepositoryRole", ...}
  -> 422 {"message":"Validation Failed",
          "errors":["Invalid bypass actor: '{{actor_id: 999999}, {actor_type: RepositoryRole}}'"]}
GET  /repos/kdknigga/scanless/rulesets  ->  []      # nothing was created by the rejected probe
```

**2. Get the role's NAME from the API, not from community lore** — the REST read-back returns only a
bare integer, which proves nothing about *which* role it is. GraphQL exposes the resolved name:

```text
gh api graphql -f query='{ repository(owner:"kdknigga",name:"scanless"){ rulesets(first:5){ nodes{
    name enforcement bypassActors(first:10){ nodes{
      bypassMode repositoryRoleDatabaseId repositoryRoleName organizationAdmin deployKey } } } } } }'

{"data":{"repository":{"rulesets":{"nodes":[{"name":"master gate","enforcement":"ACTIVE",
 "bypassActors":{"nodes":[{"bypassMode":"ALWAYS","repositoryRoleDatabaseId":5,
 "repositoryRoleName":"admin","organizationAdmin":false,"deployKey":false}]}}]}}}}
```

`repositoryRoleDatabaseId: 5` ↔ `repositoryRoleName: "admin"`, stated by GitHub itself. That is the
resolution the plan asked for.

**3. Confirm it actually applies to this user** — the REST read-back carries
`"current_user_can_bypass": "always"`, GitHub's own evaluation of the authenticated identity against
the bypass list. `gh api repos/kdknigga/scanless/collaborators` shows exactly one admin, `kdknigga`,
so the bypass currently grants exactly one human an escape hatch.

> A fourth approach was tried and **failed to prove anything**, recorded so it is not retried:
> `GET /repos/{owner}/{repo}/rules/branches/master` returns all three rules **both** before and after
> the bypass actor is added. That endpoint does not filter by the caller's bypass, so it cannot be used
> as evidence a bypass is effective. `current_user_can_bypass` is the field that answers it.

### THE READ-BACK — full verbatim JSON (the proof D-03 asks for)

`gh api repos/kdknigga/scanless/rulesets/22777879`:

```json
{
    "id": 22777879,
    "name": "master gate",
    "target": "branch",
    "source_type": "Repository",
    "source": "kdknigga/scanless",
    "enforcement": "active",
    "conditions": {
        "ref_name": {
            "exclude": [],
            "include": [
                "refs/heads/master"
            ]
        }
    },
    "rules": [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": false,
                "do_not_enforce_on_create": true,
                "required_status_checks": [
                    {
                        "context": "lint",
                        "integration_id": 15368
                    },
                    {
                        "context": "test",
                        "integration_id": 15368
                    }
                ]
            }
        },
        {
            "type": "deletion"
        },
        {
            "type": "non_fast_forward"
        }
    ],
    "node_id": "RRS_lACqUmVwb3NpdG9yec5Gx81GzgFbkBc",
    "created_at": "2026-09-10T08:58:38.376-05:00",
    "updated_at": "2026-09-10T08:59:45.100-05:00",
    "bypass_actors": [
        {
            "actor_id": 5,
            "actor_type": "RepositoryRole",
            "bypass_mode": "always"
        }
    ],
    "current_user_can_bypass": "always",
    "_links": {
        "self": {
            "href": "https://api.github.com/repos/kdknigga/scanless/rulesets/22777879"
        },
        "html": {
            "href": "https://github.com/kdknigga/scanless/rules/22777879"
        }
    }
}
```

The plan's own `--jq` projection, run verbatim:

```text
gh api repos/kdknigga/scanless/rulesets --jq '.[] | {id, name, target, enforcement}'
  -> {"enforcement":"active","id":22777879,"name":"master gate","target":"branch"}

gh api repos/kdknigga/scanless/rulesets/22777879 --jq '{name, target, enforcement, refs: ..., checks: ..., other: ...}'
  -> {"checks":["lint@15368","test@15368"],
      "enforcement":"active",
      "name":"master gate",
      "other":["required_status_checks","deletion","non_fast_forward"],
      "refs":["refs/heads/master"],
      "target":"branch"}
```

### Acceptance criteria, item by item

| Criterion | Result |
|---|---|
| `name == "master gate"`, `target == "branch"`, `enforcement == "active"` | ✅ (`active`, never `evaluate`) |
| `conditions.ref_name.include == ["refs/heads/master"]` | ✅ — not `main`, not `~ALL`, not `~DEFAULT_BRANCH` |
| Exactly two contexts, byte-identical to Plan 03's recorded names | ✅ `lint`, `test` |
| Both pinned to the Actions app id from Plan 03 | ✅ `15368` on both |
| Rule types include `required_status_checks`, `deletion`, `non_fast_forward` | ✅ all three |
| No `pull_request` rule, no unnamed rule type | ✅ exactly three rules |
| `copilot-pull-request-reviewer` NOT required | ✅ absent from `required_status_checks` |
| `bypass_actors` matches the user's decision | ✅ repository-admin role, `always` — the user's override, not the `[]` default |
| Full POST body + `bypass_actors` consequence printed before the write | ✅ both step bodies printed |
| PR #1 still `OPEN`, `mergeStateStatus` reflects the new required checks | ✅ see below |

### The required contexts bind to real check-runs (T-20-19)

The single most dangerous failure mode here is a context string no check-run ever produces, which
blocks every future merge with no visible cause. GitHub confirms the binding on the live pull request:

```text
gh api graphql ... pullRequest(number:1) ... isRequired(pullRequestNumber:1)
  -> {"state":"SUCCESS","contexts":[
       {"name":"lint","conclusion":"SUCCESS","isRequired":true},
       {"name":"test","conclusion":"SUCCESS","isRequired":true}]}
```

Both contexts report `isRequired: true` **and** are matched to actual check-runs. Note that
`copilot-pull-request-reviewer` does not appear in the rollup at all, corroborating Plan 03's decision
to exclude it.

---

## Task 2 — one seeded break, and the red run

### The break had to target pytest, not ruff — the plan's suggestion would have been silently erased

The plan proposed "an unused import in a source file … since ruff flags it deterministically." That
would have failed, quietly and confusingly: `.pre-commit-config.yaml` runs the ruff hook with
`args: [--fix]`, and an unused import is auto-fixable. The local hook would have **deleted the seeded
break during `git commit`**, producing a green run that looks like the gate is broken. Committing with
`--no-verify` was explicitly forbidden for this run.

The break used instead is one false assertion appended to an existing test. It is invisible to ruff,
`ruff format`, `ty`, and `pyrefly`, and fatal to pytest — so it commits cleanly through every local
hook and still reddens CI:

```diff
     def test_a4_dimensions(self) -> None:
         """PAPER_SIZES_MM['a4'] is (210.0, 297.0)."""
         assert PAPER_SIZES_MM["a4"] == (210.0, 297.0)
+        assert PAPER_SIZES_MM["a4"] == (999.0, 999.0)  # seeded CI break (D-08.3)
```

`git show --stat efe96a5` → **1 file changed, 1 insertion(+), 0 deletions**. Exactly one line, exactly
one break (D-10: no per-tool matrix was created, and none should be).

This also makes the run a **better** demonstration than the plan's version, because it exercises D-05
directly: one job red while the other stays green.

All local hooks passed on the seeded commit, which is itself the evidence that the break is invisible
to the four static checks:

```text
ruff (legacy alias) ... Passed     ruff format ... Passed
ty type checker ..... Passed       pyrefly type checker ... Passed
```

### The run

**Red run: [34486527413](https://github.com/kdknigga/scanless/actions/runs/34486527413)**

```text
gh run list --workflow=ci.yml --limit 1 --json conclusion,event,headBranch
  -> {"conclusion":"failure","event":"pull_request","headBranch":"seeded-break-d08-3"}

gh run view 34486527413 --json jobs
  -> [{"name":"test","conclusion":"failure"},
      {"name":"lint","conclusion":"success"}]
```

**Both run URLs side by side — two of the three pieces D-08 defines:**

| Piece | Run | Result |
|---|---|---|
| Green (Plan 03) | [34483634690](https://github.com/kdknigga/scanless/actions/runs/34483634690) | `success`, `pull_request`, all five checks executed |
| Red (this plan) | [34486527413](https://github.com/kdknigga/scanless/actions/runs/34486527413) | `failure`, `pull_request`, `test` red / `lint` green |
| Ruleset read-back | [rules/22777879](https://github.com/kdknigga/scanless/rules/22777879) | `active`, both contexts pinned to app 15368 |

**The other job's conclusion (D-05, as the acceptance criteria require):** `lint` finished
`success` in 46s while `test` failed in 54s. Both ran in parallel; one being green while the other is
red is the intended behaviour, and it does not weaken the gate because the ruleset requires **both**
contexts — which is precisely what PR #2's `BLOCKED` state below demonstrates.

### Nothing swallowed the failure

The failing step is the last real step in the job, and no subsequent step ran green over it:

```text
gh run view 34486527413 --json jobs --jq '.jobs[] | select(.name=="test") | .steps'
  3  Install SANE development headers                success
  4  astral-sh/setup-uv@20cfd1bf...                  success
  5  Run uv sync --locked                            success
  6  Run uv run pytest -m "not browser"              FAILURE   <-- stops here
 11  Post setup-uv                                   skipped
 12  Post checkout                                   success   (cleanup only)
 13  Complete job                                    success   (cleanup only)
```

The log shows the non-zero exit propagating, and that only the seeded assertion failed:

```text
>       assert PAPER_SIZES_MM["a4"] == (999.0, 999.0)  # seeded CI break (D-08.3)
E       assert (210.0, 297.0) == (999.0, 999.0)
FAILED tests/test_paper_sizes.py::TestPaperSizesMM::test_a4_dimensions
================= 1 failed, 331 passed, 8 deselected in 28.55s =================
##[error]Process completed with exit code 1.
```

331 passed + 1 failed = the same 332 selected as the green run, so nothing else regressed and the
delta is attributable to the one seeded line alone.

### THE GATE ACTUALLY BLOCKS — a state difference, not just a settings read-back

The read-back proves the ruleset *exists*. This proves it *bites*, on two live pull requests against
the same base branch at the same moment:

| PR | Head | Checks | `mergeStateStatus` |
|---|---|---|---|
| [#1](https://github.com/kdknigga/scanless/pull/1) (real, green) | `autodev-filtered` | `lint` SUCCESS (required), `test` SUCCESS (required) | **`CLEAN`** |
| #2 (seeded break, red) | `seeded-break-d08-3` | `lint` SUCCESS (required), `test` **FAILURE** (required) | **`BLOCKED`** |

This is the strongest evidence produced by the phase for success criterion 1, and it was not something
the plan asked for — the plan's criteria stop at the read-back. `mergeable` was `MERGEABLE` on both
(no conflicts); the difference is entirely the ruleset.

### Cleanup — every trace removed

```text
gh pr close 2                        -> ✓ Closed pull request kdknigga/scanless#2   (NEVER merged)
gh pr view 2 --json state,mergedAt   -> {"state":"CLOSED","mergedAt":null}
git push origin --delete seeded-break-d08-3 -> - [deleted]  seeded-break-d08-3
git worktree remove <scratch>/seedwt --force ; git worktree prune
git branch -D seeded-break-d08-3     -> Deleted branch seeded-break-d08-3 (was efe96a5)
```

Post-conditions, all asserted:

| Check | Result |
|---|---|
| `git ls-remote --heads origin \| command grep -c 'seed'` | **0** |
| `git ls-remote --heads origin` | exactly 2: `master` = `a87b3dd…`, `autodev-filtered` = `99154e2…` |
| `git for-each-ref refs/heads/` | `autodev`, `autodev-filtered`, `development`, `master`, 2 stale `worktree-agent-*` — no seed branch |
| `git worktree list` | one entry, the main tree on `autodev` |
| `git status --porcelain` | only the pre-existing ` M .planning/config.json` |
| `git branch --show-current` | `autodev` |
| `git ls-remote origin refs/heads/master` | `a87b3ddf45b094bb03dedc88a8aade5cd73d33c4` — unchanged |
| tags, local / remote | **0** / **0** |
| PR #1 | `OPEN`, `mergedAt: null`, `mergeStateStatus: CLEAN` |

**Exactly one seeded break exists in this SUMMARY: one red run URL, not four.**

---

## Task 3 — the checkpoint, auto-resolved

**Auto-resolved under `workflow.auto_advance`. No verbatim human reply exists, and none is quoted.**

The orchestrator confirmed the user pre-authorized this phase's blocking checkpoints, and supplied the
user's two direct answers (`bypass_actors`, visibility) which are folded into Task 1 above. The six
`how-to-verify` items are answered by this document:

1. **Ruleset read-back** — the full verbatim JSON, above.
2. **Green and red run URLs side by side** — the table in Task 2, plus the read-back = the three D-08 pieces.
3. **The real PR** — [#1](https://github.com/kdknigga/scanless/pull/1), `OPEN`, `mergedAt: null`, both checks `isRequired: true`.
4. **`origin/master` unchanged, no tag** — `a87b3dd…`, 0 tags local and remote.
5. **The optional `pull_request` rule** — see below. **Not added.**
6. **Bypass lock-out** — `current_user_can_bypass: "always"`; the owner is the sole admin and retains an emergency path to `master`.

Per RESEARCH Pitfall 13: Dependabot's action-bump PRs are ordinary PRs, will produce both required
checks, and will need a human merge click. Intended, not a defect.

### ⚠️ The `pull_request` rule question has NO human answer — escalated, not decided

The plan's acceptance criterion asks for an explicit decision recorded as *add now / Phase 31 / never*.
**That criterion is not met and cannot honestly be marked met.** Auto-approval means "accept the gate
as configured"; the gate as configured does **not** contain the `pull_request` rule, so the rule was
**not added** — which matches the plan's own standing instruction ("Do not add it without an explicit
yes"). But "not added because nobody said yes" is not the same as a decision, and it is not recorded
as one here.

What the user still needs to decide, carried forward to Phase 31:

> Adding `{"type": "pull_request", "parameters": {...}}` would additionally **require** a pull request
> for `master` changes, rather than merely blocking direct pushes as a side effect of the status-check
> rule. All five parameters are mandatory if used: `required_approving_review_count`,
> `dismiss_stale_reviews_on_push`, `require_code_owner_review`, `require_last_push_approval`,
> `required_review_thread_resolution`.
>
> Applying it later is a `PUT` full replace (there is no `PATCH`) against ruleset `22777879`, and the
> replacement body **must re-send `bypass_actors`** or the admin bypass is silently dropped.

## Decisions Made

- **The write was staged as `[]` → then the admin bypass, rather than posting the final body directly.**
  It costs one extra API call and makes the user's override visible as an explicit transition in the
  transcript, against a plan default that says `[]`. It also produced the control observation that
  `GET /rules/branches/{branch}` is useless as bypass evidence.
- **A deliberately-invalid `actor_id` was submitted before the real one.** Without it, "the API
  accepted `5`" would have been consistent with the API ignoring the field entirely. The 422 makes the
  later acceptance load-bearing. The rejected probe created nothing.
- **GraphQL was used to name the role.** REST returns `5`; only GraphQL returns
  `repositoryRoleName: "admin"`. Community sources agree that 5 is admin, but "several blog posts agree"
  is not the standard this phase has been holding itself to for irreversible settings writes.
- **The seeded break was built in a linked git worktree**, not by switching the main tree's branch.
  Beyond the mechanical reason (below), it makes it structurally impossible for the break to touch
  `autodev` — the constraint that the break must never be seeded on a real branch is enforced by the
  filesystem rather than by care.
- **The break targets pytest rather than ruff**, reversing the plan's suggestion, because the local
  `ruff --fix` hook would have erased a ruff-shaped break. Recorded prominently since a future executor
  reading only the plan would walk into it.
- **PR #2's `BLOCKED` state was captured before closing it.** Once the PR is closed the evidence is
  gone; this was the only window in which the gate could be observed actually refusing a red change.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 — Blocking] The plan's seeded break would have been silently deleted by the local ruff hook**

- **Found during:** Task 2
- **Issue:** The plan specifies "an unused import in a source file … ruff flags it deterministically."
  `.pre-commit-config.yaml` runs the ruff hook with `args: [--fix]` and the hook is installed at
  `.git/hooks/pre-commit`. An unused import is auto-fixable, so `git commit` would have stripped the
  seeded line and committed a clean file. The resulting green run would have been misread as "the gate
  does not catch failures." `--no-verify` was not an option (explicitly forbidden for this run).
- **Fix:** Seeded one false assertion in an existing test instead — invisible to ruff/`ruff format`/`ty`/
  `pyrefly`, fatal to pytest. Still exactly one added line. Confirmed by all four static hooks passing
  on the commit and by the run going red on pytest alone.
- **Files modified:** `tests/test_paper_sizes.py` on the throwaway branch only; deleted with the branch.
- **Verification:** `git show --stat efe96a5` → 1 file, 1 insertion, 0 deletions; run 34486527413
  `conclusion: failure`, `test` red / `lint` green; log shows `1 failed, 331 passed`.
- **Committed in:** `efe96a5`, intentionally destroyed with the branch.

**2. [Rule 3 — Blocking] `git checkout` could not create the throwaway branch without disturbing a file that must stay untouched**

- **Found during:** Task 2
- **Issue:** The plan says "branch a throwaway from the pushed filtered branch." `.planning/config.json`
  is tracked and locally modified on `autodev`, and does not exist on `autodev-filtered` (D-22 strips
  `.planning/`). Git therefore refuses the switch: *"Your local changes to the following files would be
  overwritten by checkout."* The three obvious escapes were all unavailable — `git stash` is forbidden
  (shared across worktrees), committing the file violates "leave it untouched", and discarding it
  destroys user state.
- **Fix:** Created a linked worktree at a scratch path
  (`git worktree add <scratch>/seedwt -b seeded-break-d08-3 origin/autodev-filtered`), did the break
  there, and removed the worktree during cleanup. The main working tree was never switched or modified.
- **Files modified:** none in the main tree.
- **Verification:** `git status --porcelain` before and after both report only ` M .planning/config.json`;
  `git worktree list` shows a single entry; `git branch --show-current` → `autodev`.
- **Committed in:** n/a

### Findings recorded rather than fixed

**3. [Planning defect] D-23 (private) and D-03 (ruleset) cannot both be satisfied on a free plan**

Documented in full above. Not an execution deviation — the conflict is in CONTEXT.md, and RESEARCH's
Pitfall 12 misdiagnoses the resulting 403 as a token-scope problem. Resolved by the user choosing
public. **Phase 31 must not "restore" privacy without deleting the merge gate first, and should not
follow Pitfall 12's token-shaped fallbacks if a 403 recurs.**

**4. [Clarification] `GET /repos/{owner}/{repo}/rules/branches/{branch}` does not filter by bypass**

Attempted as a functional test of the bypass actor; returns all three rules identically before and
after the bypass exists. Recorded so it is not retried as evidence. `current_user_can_bypass` on the
ruleset read-back is the field that answers the question.

---

**Total deviations:** 2 substantive (both Rule 3 — blocking issues where the plan's literal instruction
would have produced a wrong or impossible result), 1 planning defect escalated, 1 clarification.
**Impact on plan:** No scope creep, no source change that survives, nothing merged, no extra rule types.
Both blocking fixes preserved the plan's intent exactly — one seeded line, one red run, one throwaway
branch.

## Issues Encountered

None unresolved. The two blocking issues above were both fixed inline within the task.

As Plans 02 and 03 warned, this plan's `<verify>` blocks are written with `grep -c` / `grep -qv`; every
check whose exit code or count drives a claim in this SUMMARY was run with `command grep`. One further
shell artifact was observed and worked around: `git branch --list '*seed*'` printed a bare `* ` under
the `rtk` git wrapper, which is not a branch; `git for-each-ref refs/heads/` was used instead to
enumerate local branches definitively.

## Threat Model Coverage

- **T-20-16** (Spoofing, a non-Actions integration posting a status named like a required context):
  **mitigated** — both `required_status_checks[]` entries carry `integration_id: 15368`, confirmed in
  the read-back. Note Plan 03's finding still stands: app-id pinning alone does not discriminate
  `copilot-pull-request-reviewer`, which shares app 15368; the *name* match is what excludes it, and it
  is absent from the required list.
- **T-20-17** (EoP, ruleset bypass letting a red change reach `master`): **mitigated, but narrowed by
  explicit user decision.** The register specified `bypass_actors: []`. The user chose a
  repository-admin bypass with `bypass_mode: always` so an emergency path exists. Residual risk, stated
  plainly: **the repository owner can push a red change to `master` at will, and so could any future
  collaborator granted the admin role.** Today `gh api repos/kdknigga/scanless/collaborators` lists
  exactly one admin (`kdknigga`), so the bypass is currently one person wide. `enforcement` remains
  `active` — the gate was not softened to `evaluate` to achieve this.
- **T-20-18** (Tampering, history rewrite or deletion of `master`): **mitigated** — `deletion` and
  `non_fast_forward` are both present in the read-back's rule list.
- **T-20-19** (DoS, a required context no check-run produces): **mitigated, and proven live** — contexts
  copied verbatim from Plan 03, and GitHub reports `isRequired: true` against *matched, real* check-runs
  on PR #1, whose `mergeStateStatus` is `CLEAN`. A phantom context would have left PR #1 blocked.
- **T-20-20** (Tampering, the seeded break surviving): **mitigated** — PR #2 `CLOSED` with
  `mergedAt: null`, remote branch deleted, worktree removed, local branch deleted, working tree clean,
  `master` still `a87b3dd`.
- **T-20-21** (Repudiation, an irreversible-feeling admin setting applied without user sight):
  **partially mitigated, honestly.** The full request bodies and the `bypass_actors` consequence were
  printed before the write, and the full read-back is in this SUMMARY. But the checkpoint that was
  designed to *ask* the user "are you locked out?" was auto-resolved, so nobody answered it in words.
  What bounds the risk is that the user's own answer went the other way — they asked for a bypass, so
  the lock-out the threat contemplates does not exist.

### Threat Flags

| Flag | File | Description |
|------|------|-------------|
| threat_flag: exposure-scope-change | (repository visibility) | The repository is now PUBLIC, not private as D-23/D-24 assumed. `.planning/` exposure was re-verified as 0 paths on both published refs after the switch, and D-24's secret sweep still holds — but every future push is now world-readable at the moment it lands, and Phase 31 should re-run the credential sweep against that assumption rather than against a private repo. |
| threat_flag: admin-bypass-present | (ruleset 22777879) | An `always` bypass exists for the repository-admin role. Any future grant of the admin role silently grants the ability to push red changes directly to `master`. |

## Known Stubs

None.

## User Setup Required

1. **Pull request [#1](https://github.com/kdknigga/scanless/pull/1) is open, green, and now gated** —
   both `lint` and `test` show as *required*, `mergeStateStatus: CLEAN`. Merging it is the user's
   decision alone; no plan or agent will merge it.
2. **The `pull_request` rule type is still undecided** (add now / Phase 31 / never). It was not added.
   See the escalation above, including the `PUT`-must-re-send-`bypass_actors` trap.
3. **The repository is public.** Switching it back to private will remove the merge gate entirely —
   rulesets are unavailable on private free-plan repositories.

## Next Phase Readiness

- **Success criterion 1 is evidenced by all three D-08 pieces**, plus a fourth the plan did not ask for:
  green run 34483634690, ruleset read-back 22777879, red run 34486527413, and the live
  `CLEAN` vs `BLOCKED` state difference between PR #1 and PR #2.
- **Plan 05's toolchain bump (D-16) is unblocked and its precondition is now stronger than D-17
  required:** the gate is proven green on the locked versions *and* proven to go red on a real failure,
  so a red run after the bump unambiguously implicates the bump.
- **Plan 05 must push through a pull request.** Direct pushes to `master` are now blocked for everyone
  except the admin bypass, and the bypass must not be used by any agent. Any branch pushed for the bump
  must still be re-filtered with `--refs master..autodev --partial` per Plan 02, since `.planning/` is
  now publicly visible if it leaks.
- **Phase 31 inherits three items:** the `pull_request` rule decision, the public-repository
  consequences of the D-23 reversal (including re-running D-24's credential sweep), and the standing
  `release.yml`/`docs.yml` work.

---
*Phase: 20-ci-gate*
*Completed: 2026-09-10*

## Self-Check: PASSED

`.planning/phases/20-ci-gate/20-04-SUMMARY.md` exists on disk. This plan produced no surviving task
commit by design — Task 2's `efe96a5` was deliberately destroyed with the throwaway branch — so there
are no task commit hashes to verify beyond the metadata commit. Every out-of-repo artifact claimed
above was re-read after the fact and all check out: ruleset `22777879` still `enforcement: active` with
`refs: ["refs/heads/master"]`, `checks: ["lint@15368","test@15368"]`, rule types
`[required_status_checks, deletion, non_fast_forward]`, `bypass_actors` = the repository-admin role
with `bypass_mode: always`, and `current_user_can_bypass: "always"`; **0** required contexts matching
`copilot`; PR #1 `OPEN` / `mergedAt: null`; PR #2 `CLOSED` / `mergedAt: null`; `origin/master` still
`a87b3ddf45b094bb03dedc88a8aade5cd73d33c4`; **0** tags; exactly **one** distinct red-run URL in this
document; the working tree on `autodev` holding only the pre-existing ` M .planning/config.json` plus
this SUMMARY.
