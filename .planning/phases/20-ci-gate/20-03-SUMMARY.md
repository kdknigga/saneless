---
phase: 20-ci-gate
plan: 03
subsystem: infra
tags: [github-actions, ci, pull-request, publication, check-runs, ruleset-inputs]

# Dependency graph
requires:
  - phase: 20-02
    provides: "Private repo with zero refs, local branch autodev-filtered (99154e2) with zero .planning/ paths, and the auto-approved authorization to push and open the PR"
  - phase: 20-01
    provides: ".github/workflows/ci.yml on the filtered branch head -- without it the pull_request trigger has nothing to run"
provides:
  - "origin/master = a87b3dd and origin/autodev-filtered = 99154e2 on the private remote; GitHub default_branch resolved to master"
  - "Open, unmerged pull request kdknigga/saneless#1 (autodev-filtered -> master)"
  - "Green ci.yml run 34483634690, event=pull_request, both jobs success, all five checks executed"
  - "The verbatim required_status_checks contexts for Plan 04: `lint` and `test`, integration_id 15368"
  - "The warning that a THIRD check-run (copilot-pull-request-reviewer) exists on the head commit under the same app id and must NOT be required"
affects: [20-04 ruleset and seeded break, 20-05 toolchain bump, 31-delivery-and-identity]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Published-history verification is done against a freshly-fetched remote ref AND the GitHub trees API, not against local objects alone"
    - "Status-check contexts are read back from GET /commits/{sha}/check-runs, never inferred from the workflow YAML's job names"

key-files:
  created: []
  modified: []

key-decisions:
  - "PR body describes the app and the gate with zero references to the planning trail (D-22/T-20-15)"
  - "No `gh repo edit --default-branch` was needed: the first push of master made it the default automatically"
  - "The copilot-pull-request-reviewer check-run is explicitly excluded from Plan 04's required contexts"

patterns-established:
  - "Every irreversible outward step (push, PR) is followed by an independent read-back from the remote's own API"

requirements-completed: [CI-01]

# Metrics
duration: 11min
completed: 2026-09-10
---

# Phase 20 Plan 03: Publish and Prove the Gate Summary

**`master` and `autodev-filtered` are on the private remote with zero `.planning/` paths anywhere in the published history, pull request [#1](https://github.com/kdknigga/saneless/pull/1) is open and unmerged, and its `pull_request`-triggered `ci.yml` run finished green with all five checks executed — and the check-run names Plan 04 needs were read back off the head commit rather than guessed from the YAML, which is how the unexpected third check-run was found.**

## Performance

- **Duration:** 11 min
- **Started:** 2026-09-10T13:31:48Z
- **Completed:** 2026-09-10T13:42:49Z
- **Tasks:** 3 (all automated)
- **Files modified:** 0 in the working tree (two remote refs, one pull request, one read-only API query)

## THE DELIVERABLE FOR PLAN 04 — check-run contexts, read verbatim from the API

Queried read-only via `gh api repos/kdknigga/saneless/commits/99154e21f14d6f1211318789725473afadd497b0/check-runs`.

| `name` (verbatim) | `app.slug` | `app.id` | `conclusion` | Source |
|---|---|---|---|---|
| `lint` | `github-actions` | **15368** | `success` | `ci.yml` job `lint` |
| `test` | `github-actions` | **15368** | `success` | `ci.yml` job `test` |
| `copilot-pull-request-reviewer` | `github-actions` | **15368** | `success` | Workflow "Copilot", event `dynamic` — **not this repo's** |

**Plan 04 must write exactly these two contexts into `required_status_checks`:**

```
`lint`
`test`
```

**Actions app integration_id: `15368`** (confirmed identical for all three check-runs; `app.name` is `GitHub Actions`, `app.slug` is `github-actions`). Pin every required context to this `integration_id` so a same-named status from another source cannot satisfy the rule (T-20-14).

The legacy statuses API returns nothing for this commit — `GET /commits/{sha}/status` is `{"state":"pending","total_count":0,"contexts":[]}`. There are no commit statuses at all, only check-runs. Plan 04 should not go looking for a Statuses-API context string; it does not exist.

### ⚠️ Do NOT require `copilot-pull-request-reviewer`

The plan's acceptance criterion said the query "returns exactly two check runs". It returned **three**. The third is not from `ci.yml` and not from any file in this repository:

```text
gh run view 34483645042 --json workflowName,name,event
  -> {"workflowName":"Copilot","name":"Running Copilot Code Review","event":"dynamic"}
```

It is GitHub's account-level Copilot code-review bot, which posts its check-run through the same GitHub Actions app (hence the identical `app.id` 15368, which is exactly why app-id pinning alone cannot distinguish it — the *name* must be matched too). It took ~3.5 minutes to complete, far longer than the whole CI run.

Requiring it in the ruleset would be a live hazard: it is a review assistant, not a gate; it is toggled by an account/enterprise setting entirely outside this repo; and if it is ever disabled, `master` becomes permanently unmergeable with no visible cause. It is also absent from `gh pr view --json statusCheckRollup`, which lists only `lint` and `test` — so it is not treated as a gating check by GitHub's own PR rollup either.

## Task Commits

All three tasks change no working-tree file by design — their effects are two remote refs, one pull request, and one read-only API query — so none produced a task commit. This plan's only commit is its metadata commit.

1. **Task 1: Push master and the filtered branch** — no commit (remote refs only)
2. **Task 2: Open the PR and drive the workflow to green** — no commit (one pull request)
3. **Task 3: Read the real check-run contexts** — no commit (read-only `GET`s)

## Files Created/Modified

None. `git status --porcelain` reports only the pre-existing ` M .planning/config.json`, which predates this phase and was left untouched, exactly as in Plan 02.

## Task 1 — the publication

Preconditions re-confirmed immediately before the first push: `gh repo view … --jq .isPrivate` → `true`; `git ls-remote origin` → **0 refs**; the Plan 02 authorization present in `20-02-SUMMARY.md`.

```bash
git push -u origin master            # * [new branch] master -> master
git push -u origin autodev-filtered  # * [new branch] autodev-filtered -> autodev-filtered
```

`master` first, so it existed as the PR base. **No other ref was pushed** — the local repo also holds `development`, and the stale `worktree-agent-a0045dfe` / `worktree-agent-ac7cb017` branches, and none of them was published. `git push --all` was never run.

### Read-back against the remote

| Check | Result |
|---|---|
| `git ls-remote --heads origin` | exactly 2: `refs/heads/master` = `a87b3dd…`, `refs/heads/autodev-filtered` = `99154e2…` |
| `gh api repos/kdknigga/saneless/branches --jq '[.[].name]'` | `["autodev-filtered","master"]` |
| `gh api repos/kdknigga/saneless/tags --jq length` | **0** |
| `gh repo view --json isPrivate,defaultBranchRef` | `{"private":true,"default":"master"}` |

**The default branch resolved to `master` on its own.** Plan 02 recorded `defaultBranchRef` as empty (the repo had no refs); the first `git push origin master` both created the ref and made it the default, so the fallback `gh repo edit --default-branch master` in the plan's action was not needed and was not run. Plan 04's decision to target `refs/heads/master` explicitly remains correct regardless.

### `.planning/` re-verified against the REMOTE (D-22 / T-20-11)

Not trusted from local objects. Both refs were re-fetched from the remote into throwaway refs, *and* independently cross-checked against GitHub's own trees API:

| Check | Command | Result |
|---|---|---|
| Remote `master` head tree | `git ls-tree -r --name-only <fetched> \| command grep -c '^\.planning/'` | **0** |
| Remote `autodev-filtered` head tree | same | **0** |
| Every commit in the published range | loop of `git diff-tree --no-commit-id --name-only -r` over all 112, summed | **0** |
| GitHub trees API, `master` | `gh api …/git/trees/a87b3dd…?recursive=1` paths starting `.planning` | **0** |
| GitHub trees API, `autodev-filtered` | same | **0** planning / **82** blobs / `truncated: false` |
| `ci.yml` present on the PR head | `command grep -c '^\.github/workflows/ci\.yml$'` | **1** |
| Commits published | `git rev-list --count <master>..<prbranch>` | **112** |

The fetched SHAs matched the pushed ones exactly, so the remote holds the same objects that were verified. All greps used `command grep` (Plan 02 deviation 2 — the shell's `grep` is a `ugrep` wrapper with a broken `-qv` exit status).

**Neither push triggered a workflow**, as D-02 predicts: `gh run list` returned `[]` after both. `master`'s root commit predates `ci.yml`, and `push: { branches: [master] }` does not match `autodev-filtered`.

## Task 2 — the pull request and the green run

**Pull request: [kdknigga/saneless#1](https://github.com/kdknigga/saneless/pull/1)** — `autodev-filtered` → `master`, title *"v2.0 prep: publish application history and add the CI gate"*, head `99154e21f14d6f1211318789725473afadd497b0`.

The body describes the application and the new gate (the two jobs, the five checks, `libsane-dev` before `uv sync --locked`, SHA-pinned actions with Dependabot, `pytest-timeout`, the deferred browser job) and **cites no `.planning/` path and no internal review** (T-20-15). It closes by stating the PR is left open for the repository owner to merge.

**Run: [34483634690](https://github.com/kdknigga/saneless/actions/runs/34483634690)**

```text
gh run list --workflow=ci.yml --limit 1 --json conclusion,headBranch,event
  -> {"conclusion":"success","event":"pull_request","headBranch":"autodev-filtered"}
gh run view 34483634690 --json jobs
  -> 2 jobs: [{"name":"lint","conclusion":"success"},{"name":"test","conclusion":"success"}]
```

Started 13:35:53Z, finished 13:37:13Z — **1m20s wall**, `lint` in 39s and `test` in 1m17s, running in parallel as D-05 intended.

### All five checks executed (success criterion 2), with the setup path proven

Pulled from `gh run view 34483634690 --log`, in log order — note the `libsane-dev` line precedes `uv sync --locked` in **both** jobs, which is the ordering D-Discretion requires and the genuinely uncertain part of the run:

```text
lint  Install SANE development headers   Setting up libsane-dev:amd64 (1.2.1-7build4) ...
lint  Run uv sync --locked               Resolved 70 packages / Installed 70 packages in 377ms
lint  Run uv run ruff check .            All checks passed!
lint  Run uv run ruff format --check .   37 files already formatted
lint  Run uv run ty check                All checks passed!
lint  Run uv run pyrefly check src tests           INFO 0 errors
test  Install SANE development headers   Setting up libsane-dev:amd64 (1.2.1-7build4) ...
test  Run uv sync --locked               Resolved 70 packages / Installed 70 packages in 390ms
test  Run uv run pytest -m "not browser"  collected 340 items / 8 deselected / 332 selected
test  Run uv run pytest -m "not browser"  ====== 332 passed, 8 deselected in 28.72s ======
```

`uv sync --locked` reported **no lockfile mismatch** in either job (a grep for mismatch/out-of-date/"would be updated" over the 1010-line log returns 0) — it resolved 70 packages from the lockfile in ~1ms, which is the lockfile-hit path. `python-sane` compiled from sdist against the installed headers without incident.

**332 passed / 8 deselected in 28.72s on the runner matches the local baseline exactly** (332/8 in 26.8s, measured 2026-09-09). CI and local agree, which is what `uv sync --locked` is for.

The gate was not weakened in any way to achieve this: no `continue-on-error`, no `|| true`, no removed step, no `-m` filter beyond `not browser`. `ci.yml` was not edited by this plan at all — it ran exactly as Plan 01 committed it. `release.yml` and `docs.yml` were not touched (D-06).

### Nothing was merged, nothing advanced (D-21 / T-20-12)

```text
gh pr view 1 --json state,mergedAt,mergeStateStatus
  -> {"state":"OPEN","mergedAt":null,"mergeStateStatus":"CLEAN"}
git ls-remote origin refs/heads/master
  -> a87b3ddf45b094bb03dedc88a8aade5cd73d33c4    (unchanged, the original signed root commit)
gh api repos/kdknigga/saneless/tags --jq length
  -> 0
```

No `gh pr merge`, no `git merge`, no `<branch>:master` push, no force-push, and no tag was run at any point in this plan. `mergeStateStatus: CLEAN` means the PR *could* be merged — that is the user's action alone, and the phase's terminal state is this PR sitting open.

## Authorization provenance

This plan was authorized by the blocking checkpoint in Plan 20-02 Task 3, which was **AUTO-APPROVED by the orchestrator under `workflow.auto_advance`**, deriving from a standing configuration choice the user made at phase start.

**No verbatim human reply exists, and none is quoted here.** The approval covered pushing `master`, pushing `autodev-filtered`, and opening the pull request — all three of which this plan performed. It did not, and could not, cover merging: D-21 forbids that unconditionally.

**`bypass_actors: []` remains UNANSWERED.** Plan 02 recorded it as an open input and nothing in this plan changed that. Plan 04 must still get a direct answer before applying the ruleset.

## Decisions Made

- **The PR body was written from the application's own vocabulary, not the phase's.** It reads as an ordinary first pull request for the project rather than as a GSD artifact — which is both what T-20-15 requires and what a repository being prepared for release should show.
- **`copilot-pull-request-reviewer` is excluded from Plan 04's required contexts** rather than being reported neutrally as "a third check-run appeared". See the reasoning above; a required context that depends on an account-level toggle is a merge-deadlock waiting to happen.
- **The Copilot run was polled to completion before the check-run table was finalized.** Reading it while `in_progress` would have recorded `conclusion: null` and left an ambiguous row in the deliverable.
- **Verification was done twice through independent channels** (re-fetched git refs and the GitHub trees API). For an irreversible publication, a single channel that says "clean" is a weaker claim than it looks.

## Deviations from Plan

### Findings that changed the deliverable

**1. [Rule 2 — missing critical guard] The head commit carries THREE check-runs, not two, and the third would poison the ruleset**

- **Found during:** Task 3
- **Issue:** The plan's acceptance criterion asserted "exactly two check runs (D-05: two parallel jobs, therefore two required contexts)". The API returned three. `copilot-pull-request-reviewer` is posted by GitHub's account-level Copilot code-review bot through workflow "Copilot" (event `dynamic`, run `34483645042`) — a workflow that exists in no file in this repository. Critically, it reports under the **same** `app.id` 15368 as the real jobs, so the app-id pinning the plan relies on for T-20-14 does not discriminate between them. Had Plan 04 taken "all check-runs on the head commit, from app 15368" as its context list — the obvious reading of the plan's own criterion — it would have required a context controlled by a toggle outside the repository, and `master` would become unmergeable the moment Copilot review is disabled.
- **Fix:** Traced the third check-run to its workflow, waited for it to reach `completed`, and recorded it explicitly as an **exclusion** with the reasoning, rather than letting Plan 04 rediscover it. Cross-checked against `gh pr view --json statusCheckRollup`, which lists only `lint` and `test` — corroborating that it is not a gating check.
- **Files modified:** none
- **Verification:** `gh run view 34483645042 --json workflowName,name,event` → `{"workflowName":"Copilot","name":"Running Copilot Code Review","event":"dynamic"}`; final conclusion `success`; `statusCheckRollup` = `[lint SUCCESS, test SUCCESS]`.
- **Committed in:** n/a — no working-tree change

**2. [Not a deviation, recorded for accuracy] The default-branch fallback was unnecessary**

The plan's action anticipated `defaultBranchRef` coming back as something other than `master` (Plan 02 observed it empty) and instructed a `gh repo edit --default-branch master` if so. After `git push origin master` the default was already `master`, so **no `gh repo edit` was run**. Recording this because Plan 04's ruleset targeting decision was written under the assumption that no default branch would exist.

### Observed but out of scope

A GitHub Actions annotation on the `test` job: `Failed to save: Unable to reserve cache with key setup-uv-2-… another job may be creating this cache.` This is the two parallel jobs racing to populate the same uv cache key — the inherent, accepted cost of D-05's two-job layout. It is a warning, not a failure; both jobs still resolved 70 packages in ~1ms from the lockfile. **Not fixed** (SCOPE BOUNDARY: not caused by this plan's changes, and "fixing" it would mean restructuring the job layout D-05 decided on).

---

**Total deviations:** 1 substantive (Rule 2 — a guard added to the deliverable that the plan did not anticipate needing), 1 clarification, 1 out-of-scope observation logged.
**Impact on plan:** No scope creep, no source change, nothing merged. The substantive finding strengthens Plan 04's input rather than altering this plan's work.

## Issues Encountered

None blocking. The run was green on the first attempt, so the plan's red-run diagnosis path (RESEARCH Pitfalls 5 and 11) was never entered.

As Plan 02 warned, this plan's `<verify>` blocks are written with `grep -c` / `grep -qv`; every check whose exit code or count drives a claim in this SUMMARY was re-run with `command grep`. The one visible artifact of the wrapper here is benign — a `grep -c` returning 0 propagates exit 1 and reddens the surrounding command — and did not corrupt any count.

## Threat Model Coverage

- **T-20-11** (Info disclosure, published git history): **mitigated** — 0 `.planning/` paths on both pushed refs and across all 112 published commit diffs, verified against a fresh fetch from the remote *and* against GitHub's trees API; repo confirmed `private: true` immediately before the first push and again after.
- **T-20-12** (Tampering, accidental advance of `master`): **mitigated** — `origin/master` still `a87b3dd`, the original signed root commit. No merge, no `<branch>:master` push, no force-push, no tag.
- **T-20-13** (EoP, weakened gate forcing green): **mitigated** — `ci.yml` was not edited by this plan; the run log is quoted above showing all five check commands executing with real output (`All checks passed!`, `37 files already formatted`, `INFO 0 errors`, `332 passed`).
- **T-20-14** (Spoofing, a same-named status from a non-Actions app): **mitigated, and materially strengthened** — `app.id` 15368 recorded for pinning, *and* the discovery that a non-CI check-run shares that app id means Plan 04 must match on name **and** integration_id, not integration_id alone.
- **T-20-15** (Info disclosure, PR body leaking the planning trail): **mitigated** — the body cites no `.planning/` path and no internal review.
- **T-20-09** (Repudiation, publication without the user's knowledge) — inherited from Plan 02 as residual risk transferred to this plan: **the publication has now happened under an automated approval, so the risk is realized rather than mitigated.** The repository is private and nothing is merged, which bounds it; the user should be told plainly that #1 exists.

No new security surface beyond the register, so there are no threat flags.

## Known Stubs

None.

## User Setup Required

**Two things need the user's attention:**

1. **Pull request [#1](https://github.com/kdknigga/saneless/pull/1) is open and green** on the now-private `kdknigga/saneless`. Merging it is the user's decision alone; no plan or agent will merge it.
2. **`bypass_actors: []` is still unanswered** and blocks Plan 04. With an empty bypass list, every future change to `master` — including the user's own, as repository admin — must go through a pull request with both `lint` and `test` green, with no escape hatch for anyone.

## Next Phase Readiness

- **Plan 04 is unblocked on inputs but still blocked on the `bypass_actors` question.** It has everything else it needs: required contexts `lint` and `test`, `integration_id` 15368, target `refs/heads/master` (which now exists and is the default branch), and an open PR to seed the break against.
- **Plan 04 must exclude `copilot-pull-request-reviewer`** from `required_status_checks` and must match required contexts by name *and* integration_id.
- **Plan 04's seeded break** should go on a throwaway branch and a separate PR, so it does not redden #1, which is being handed to the user green.
- **Plan 05's toolchain bump** (D-16) now has its precondition satisfied: the gate is proven green on the locked versions (`ty` 0.0.24, `pyrefly` 0.57.1, `ruff` 0.15.7), so a red run after the bump unambiguously implicates the bump (D-17).
- Any future push to the published branches must be re-filtered with `--refs master..autodev --partial`, per Plan 02.

---
*Phase: 20-ci-gate*
*Completed: 2026-09-10*

## Self-Check: PASSED

`.planning/phases/20-ci-gate/20-03-SUMMARY.md` exists on disk. This plan produced no task commits by
design (no task changes a working-tree file), so there are no task commit hashes to verify. Every
out-of-repo artifact this SUMMARY claims was re-read after the fact and all check out:
`git ls-remote --heads origin` = exactly 2 refs, `refs/heads/master` still `a87b3dd…`, PR #1 `OPEN`
with `mergedAt: null`, run `34483634690` `success`/`pull_request`, 0 tags locally and 0 on the remote,
and the working tree holds only the pre-existing ` M .planning/config.json` plus this SUMMARY.
