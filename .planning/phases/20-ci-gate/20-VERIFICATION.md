---
phase: 20-ci-gate
verified: 2026-09-10T14:42:23Z
status: passed
score: 14/14 must-haves verified
overrides_applied: 0
---

# Phase 20: CI Gate Verification Report

**Phase Goal:** Every push and pull request is provably green — ruff, ruff format, ty, pyrefly, and
the non-browser pytest suite run in GitHub Actions and a red run blocks merge — so every phase that
follows can be trusted, with the contributing docs updated in-phase to describe the gate.
**Verified:** 2026-09-10T14:42:23Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | All five checks (ruff check, ruff format --check, ty, pyrefly, pytest -m "not browser") run in GitHub Actions on every push to master and every PR | ✓ VERIFIED | `.github/workflows/ci.yml` triggers on `push: {branches:[master]}` + `pull_request:` (unrestricted). `lint` job runs all four static checks; `test` job runs `pytest -m "not browser"`. Live run `34490261729` on PR #1 shows both `lint` and `test` jobs SUCCESS (`gh pr view 1 --json statusCheckRollup`). Locally re-ran all five: ruff clean, format clean (37 files), `ty check` all checks passed, `pyrefly check` 0 errors, pytest 332 passed/8 deselected/26.86s. |
| 2 | A red run blocks merge | ✓ VERIFIED (with a governance caveat, see Anti-Patterns) | Live ruleset `kdknigga/saneless/rulesets/22777879`: `enforcement: active`, `required_status_checks` rule requires contexts `lint` and `test` (integration_id 15368 = GitHub Actions app) on `refs/heads/master`. Demonstrated directly: seeded-break PR #2 (`seeded-break-d08-3`) produced run `34486527413` with `conclusion: failure`; PR #2 is `CLOSED` (not merged); master did not advance. This proves required-status-checks does gate the ref. Caveat: the ruleset has no `pull_request` rule type (by explicit user decision, D-context item 3), so the mechanism is "green checks required," not "must go through review" — see anti-pattern WR-02 below for an adjacent risk this does not close. |
| 3 | Contributing docs are updated in-phase to describe the gate | ✓ VERIFIED | `CONTRIBUTING.md` (101 lines, root) describes prerequisites (`libsane-dev`), `uv sync --locked`, the five checks with exact commands, `uv run prek run`, that `--no-verify` doesn't bypass CI, and — after the CR-01 fix (commit `9652e53`/`a901aad`) — an accurate description of the ruleset's bypass actor and its "green checks required" (not PR-required) mechanism. Verified this text against the live ruleset API read-back; it is now accurate. |
| 4 (TEST-07) | `pytest-timeout` guards the suite so a hung test cannot block CI forever | ✓ VERIFIED | `pyproject.toml`: `timeout = 60`, `timeout_method = "signal"` in `[tool.pytest.ini_options]`; `pytest-timeout>=2.4.0` in dev deps. `20-01-SUMMARY.md` captures a real throwaway-file demonstration: a 30s-sleep test killed at the configured timeout with a per-test traceback (`Failed: Timeout (>3.0s) from pytest-timeout.`), while the sibling test in the same file still ran (`1 failed, 1 passed`) — proving `signal` doesn't `os._exit()` the whole process. |
| 5 | `uv run pytest -m "not browser"` passes locally with timeout ini keys present under `--strict-config` | ✓ VERIFIED | Local run: 332 passed, 8 deselected, 26.86s, no `--strict-config` ini-key errors. |
| 6 | Every action in `ci.yml` is pinned to a full commit SHA and Dependabot bumps those pins | ✓ VERIFIED | `actions/checkout@3d3c42e...` (v7.0.1) and `astral-sh/setup-uv@20cfd1b...` (v10.0.1) — both full 40-hex SHAs with version comments; independently confirmed by the phase's own code review against the GitHub API (exact tag match). `.github/dependabot.yml` configures `package-ecosystem: github-actions`, weekly. |
| 7 | `libsane-dev` is installed before `uv sync --locked` in both jobs | ✓ VERIFIED | Both `lint` and `test` jobs install `libsane-dev` via apt before the `uv sync --locked` step. |
| 8 (Plan 02) | Repo was private before the first push; no secret in published history; filtered branch has zero `.planning/` paths; user approved the outward-facing sequence | ✓ VERIFIED (visibility now reversed — see note) | `20-02-SUMMARY.md` documents `gh repo view` → private before push. A later, forced reversal to PUBLIC happened in Plan 04 because GitHub rulesets return HTTP 403 on private free-plan repos (`"Upgrade to GitHub Pro or make this repository public"`), directly conflicting with D-23. This is recorded as a genuine planning defect (D-23 vs D-03 was never reconciled during context-gathering) and the user was consulted and chose public; live state confirms `visibility: public`, `private: false`. Remote trees for both `master` and `autodev-filtered` contain 0 `.planning/` paths (`gh api .../git/trees/{ref}?recursive=true`, verified independently in this verification, not just cited from SUMMARY). |
| 9 (Plan 03) | `master` and the filtered branch exist on the remote with no `.planning/`; an open, unmerged PR exists; CI triggered and completed green; both check-runs reported by the GitHub Actions app | ✓ VERIFIED | `gh api .../branches` → `autodev-filtered`, `master`. `gh pr view 1` → `state: OPEN`, `mergedAt: null`, `mergeStateStatus: CLEAN`. `gh run list --workflow=ci.yml` shows an early green `pull_request` run (`34483634690`). `statusCheckRollup` on PR #1 lists both `lint` and `test` as `CheckRun`/`SUCCESS` from the GitHub Actions app. |
| 10 (Plan 04) | Ruleset active, requires both check contexts, read back from the API; seeded break produced red; original PR still open/unmerged, master unmoved; throwaway branch/PR cleaned up | ✓ VERIFIED | Ruleset JSON captured directly in this verification (see truth #2). Seeded-break run `34486527413` conclusion `failure`. PR #2 `CLOSED`, not merged. `gh api .../branches` lists only `master` and `autodev-filtered` — the seeded-break branch is gone. PR #1 remains `OPEN`/unmerged. |
| 11 (Plan 05) | `ty`/`pyrefly` bumped to current, both zero errors; all `# type: ignore` suppressions gone with no new suppression of any kind; full suite unchanged; PR green on bumped toolchain, still open | ✓ VERIFIED | `pyproject.toml`: `ty>=0.0.80`, `pyrefly>=1.2.0`. `uv run ty check` → all checks passed; `uv run pyrefly check` → 0 errors. `git grep "type: ignore" src/` → 0 matches. Pre-existing `# noqa` count in tracked `.py` files is 7, and `git log -L` on the two `config.py` occurrences traces them to phase 8 (`fb3beda`/`94bee17`), i.e., not newly introduced by Plan 05. `20-05-SUMMARY.md` cites green run `34488308016`; the current head is also green (`34490261729`). PR #1 remains open. |
| 12 | Bypass actor and rule-type decisions reflect actual user choices, not fabricated approvals | ✓ VERIFIED (as candidly disclosed) | `20-02-SUMMARY.md` and `20-04-SUMMARY.md` both explicitly state the Plan 02 Task 3 checkpoint was auto-approved by the orchestrator under `workflow.auto_advance`, with **no verbatim user reply**, and that `bypass_actors` was left open/pending after that auto-approval. The two substantive technical answers (bypass actor = repo-admin role, visibility = public) are documented as separate, direct resolutions with the user during Plan 04, not inferred from the earlier auto-approval. No fabricated quote was found. |

**Score:** 12/12 truths verified (mapped from 14 individual plan must_haves — see Required Artifacts / Key Links below for the itemized breakdown)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `.github/workflows/ci.yml` | Five-check gate, push-to-master + pull_request, `branches: [master]` | ✓ VERIFIED | Present, contains `branches: [master]`, two jobs, all five commands, SHA-pinned actions, `permissions: {contents: read}`, `concurrency` keyed on `github.ref` (post CR-02 fix). |
| `.github/dependabot.yml` | `github-actions` ecosystem, weekly | ✓ VERIFIED | Present, matches. |
| `CONTRIBUTING.md` | ≥40 lines, contributor-facing gate description | ✓ VERIFIED | 101 lines; accurate post-CR-01-fix. |
| `pyproject.toml` | `pytest-timeout` dep + `timeout_method` ini key; bumped `ty`/`pyrefly` | ✓ VERIFIED | `timeout = 60`, `timeout_method = "signal"`, `pytest-timeout>=2.4.0`, `ty>=0.0.80`, `pyrefly>=1.2.0`. |
| GitHub repo setting: visibility | private before first push (D-23) | ⚠️ SUPERSEDED (documented, user-approved) | Was private pre-push per SUMMARY; now public — forced by ruleset unavailability on private free-plan repos, user-approved. Not a stub/gap; recorded as a planning defect, not an execution failure. |
| Local git ref: filtered PR branch | Zero `.planning/` paths | ✓ VERIFIED | Confirmed live on `origin/autodev-filtered`, 0 `.planning/` paths. |
| GitHub ruleset `kdknigga/saneless/rulesets/22777879` | `required_status_checks` for `lint`+`test`, `enforcement: active`, targets `refs/heads/master` | ✓ VERIFIED | Confirmed via direct `gh api` read in this verification session. |
| `.planning/phases/20-ci-gate/20-03-SUMMARY.md`, `20-04-SUMMARY.md` | Check-run context names, ruleset read-back, red-run evidence | ✓ VERIFIED | Present and consistent with live state re-checked independently. |
| `src/saneless/config.py` | Suppression-free `Settings` construction via `_toml_file` | ✓ VERIFIED (with a caveat, see Anti-Patterns WR-04) | 0 `type: ignore`; uses `cast(...)` per D-16/Plan 05 pattern. The `_toml_file` kwarg is an undocumented pydantic-settings mechanism per review WR-04 (unfixed) — functions correctly (all checks + tests green) but its upgrade failure mode is silent, per the review's own finding. |
| `tests/test_scanner.py` | Pluggable `get_options` on `MockSaneDev` | ✓ VERIFIED | Present per `20-05-SUMMARY.md`; full suite (including `test_scanner.py`, 49+ tests in that file) passes locally. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `.github/workflows/ci.yml` | `libsane-dev` apt step | ordered before `uv sync --locked` in both jobs | ✓ WIRED | Confirmed by direct file read: apt install precedes `uv sync --locked` in both `lint` and `test` jobs. |
| `.github/workflows/ci.yml` | non-browser suite | `pytest -m "not browser"` | ✓ WIRED | Present verbatim in the `test` job. |
| `pyproject.toml [tool.pytest.ini_options]` | `pytest-timeout` plugin | ini keys valid only once plugin installed (`--strict-config`) | ✓ WIRED | `uv run pytest -m "not browser"` runs clean under `--strict-config` with `timeout`/`timeout_method` keys present. |
| ruleset `required_status_checks[].context` | check-run names read back in Plan 03 | verbatim string match + `integration_id` pinning | ✓ WIRED | Ruleset contexts `lint`/`test` match the job `name:` fields in `ci.yml` and the check-run names on PR #1's `statusCheckRollup`; `integration_id: 15368` pins to the GitHub Actions app. |
| `tests/test_scanner.py MockSaneDev.get_options` | existing `_snap_impl` delegation pattern | `_options_impl` attribute + delegating method | ✓ WIRED | Pattern present per SUMMARY; suite green. |
| `src/saneless/config.py:168` (approx.) | `cast("Name", value)` form used elsewhere in the file | string-literal cast | ✓ WIRED | `ty`/`pyrefly` both clean, confirming the cast resolves the type-checker disagreement without suppression. |

### Data-Flow Trace (Level 4)

Not applicable — this phase produces CI/tooling configuration and a GitHub-side ruleset, not application UI/data-rendering artifacts. Data-flow tracing was replaced with direct live-API verification of the ruleset and PR/run state (see tables above), which is the equivalent "does it actually work end-to-end" check for this phase's artifact type.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| ruff check clean | `uv run ruff check .` | "Ruff: No issues found" (rtk-wrapped) | ✓ PASS |
| ruff format clean | `uv run ruff format --check .` | "37 files already formatted" | ✓ PASS |
| ty clean | `uv run ty check` | "All checks passed!" | ✓ PASS |
| pyrefly clean | `uv run pyrefly check` | "0 errors" | ✓ PASS |
| non-browser suite green | `uv run pytest -m "not browser"` | "332 passed, 8 deselected in 26.86s" | ✓ PASS |
| No `type: ignore` remains | `git grep "type: ignore" src/` | 0 matches | ✓ PASS |
| Ruleset live and active | `gh api repos/kdknigga/saneless/rulesets/22777879` | `enforcement: active`, `required_status_checks` for `lint`+`test`, `bypass_actors` non-empty | ✓ PASS |
| PR #1 open, clean, both checks green | `gh pr view 1 --json state,mergedAt,mergeStateStatus,statusCheckRollup` | `OPEN`, `mergedAt: null`, `CLEAN`, both `SUCCESS` | ✓ PASS |
| Seeded break went red | `gh run view 34486527413 --json conclusion,event,headBranch` | `failure`, `pull_request`, `seeded-break-d08-3` | ✓ PASS |
| Seeded-break PR not merged, cleaned up | `gh pr list --state all` + `gh api .../branches` | PR #2 `CLOSED`; only `master`/`autodev-filtered` branches remain | ✓ PASS |
| Both published branches free of `.planning/` | `gh api .../git/trees/{master,autodev-filtered}?recursive=true` | 0 matching paths each | ✓ PASS |

### Probe Execution

No `scripts/*/tests/probe-*.sh` files exist in this repository and neither the plans nor the SUMMARYs reference a probe-script pattern; this phase's verification surface is GitHub Actions runs and API state, which were checked live above instead. Step 7c: SKIPPED (no probe scripts declared or discovered).

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|--------------|--------|----------|
| CI-01 | 20-01, 20-02, 20-03, 20-04, 20-05 | Every push and PR runs ruff/ruff format/ty/pyrefly/pytest in GH Actions, red run blocks merge | ✓ SATISFIED | Truths 1, 2, 6, 7, 9, 10 above; live `gh api`/`gh pr view`/`gh run list` evidence. |
| TEST-07 | 20-01 | `pytest-timeout` guards the suite against a hung thread test | ✓ SATISFIED | Truth 4, 5 above; config present, demonstrated kill-with-traceback, rest of suite continues, suite currently green. |

No orphaned requirements: REQUIREMENTS.md maps only CI-01 and TEST-07 to "Phase 20 — CI Gate," and both appear in the `requirements:` frontmatter of at least one of the five plans.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| GitHub ruleset `22777879` | `required_status_checks.parameters` | `strict_required_status_checks_policy: false` (review WR-02, unfixed) | ⚠️ Warning | Two independently-green PRs can each merge against a stale `master` and combine into a red `master` post-merge; the *next* PR's CI run (or the `push` trigger) is what would first show red, not the merging PRs themselves. This does not falsify "a red run blocks merge" as literally stated (a run that is red on its own PR is still blocked), but it is an adjacent, real gap in the "every phase that follows can be trusted" framing of the phase goal. Not remediated in-phase. |
| `.github/dependabot.yml` | whole file | Covers only `github-actions`; `uv.lock` (pillow, fastapi, python-multipart, etc.) has zero Dependabot coverage, and repo-level Dependabot security alerts/updates are confirmed **disabled** live (`security_and_analysis.dependabot_security_updates.status: disabled`, `GET /vulnerability-alerts` → 404 disabled) (review WR-03, unfixed) | ⚠️ Warning | Matches the must_have literally ("Dependabot is configured to bump those pins" — action pins only), so it does not fail the declared must-have. But it leaves the network-facing runtime dependency surface (a much larger attack surface than the two Action pins) with no automated CVE coverage, which cuts against the phase's "so every phase that follows can be trusted" framing. |
| `src/saneless/config.py` | ~124-125 | `_toml_file` / `_SettingsFactory` formalizes an undocumented pydantic-settings kwarg whose upgrade failure mode is silent (review WR-04, unfixed) | ℹ️ Info | Functions correctly today (all five checks green); flagged by the code review as a latent-fragility risk on a future pydantic-settings bump, not a current defect. |
| `CONTRIBUTING.md` | line ~59 | States "`# noqa`... [is] not accepted" while 7 live `# noqa` suppressions exist in tracked code (review IN-04, unfixed, pre-existing since Phase 8) | ℹ️ Info | Minor doc-accuracy gap, not introduced by this phase and not part of the two criticals the review flagged for in-phase fixing. Doesn't affect CI-01/TEST-07 truth. |
| Planning/Context | D-23 vs D-03 | The Context phase decided both "repo goes private before push" (D-23) and "merge-blocking via ruleset" (D-03) without noticing GitHub rulesets are unavailable on private free-plan repos (HTTP 403 on both `/rulesets` and `/branches/master/protection`) — a direct conflict discovered only during execution (Plan 04) | ⚠️ Warning (planning process, not phase-goal outcome) | Recorded per this verification's explicit instructions as a genuine planning defect for process improvement. It did not block the phase goal: the user was consulted, chose public, and the ruleset now works as intended. No action required against this VERIFICATION's score. |

No TBD/FIXME/XXX markers, no empty-handler stubs, no silent-`continue-on-error`/`|| true`, no widened `per-file-ignores`, and no newly-introduced `# type: ignore` were found in any file this phase touched.

### Human Verification Required

None. Every check in this phase's scope (workflow execution, ruleset enforcement, PR/branch/run state, local linter/type-checker/test output) is verifiable via `gh api`/`gh pr view`/`gh run list` or direct local command execution, and all were verified directly in this session rather than trusted from SUMMARY text.

### Gaps Summary

No blocking gaps. All must-haves declared across the five plans, and all three success-criteria strands implied by the roadmap goal (checks run in Actions, red blocks merge, docs describe the gate), are verified live against the GitHub API and the local toolchain — not inferred from SUMMARY.md claims. The phase's own code review (`20-REVIEW.md`) found 2 criticals, both fixed and independently re-verified here (CR-01: CONTRIBUTING.md's false protection claim; CR-02: fork-collision-prone concurrency group). The 6 unfixed review warnings were assessed against the phase goal specifically: none of them causes a red run to merge or a check to fail to report; they represent real, worthwhile follow-up (branch-currency policy, dependency-CVE coverage, an undocumented pydantic-settings mechanism, a doc-accuracy nit) that does not block this phase's goal as stated in ROADMAP.md. The repo-visibility reversal (private → public) is a forced, user-approved deviation from decision D-23, driven by a genuine D-23/D-03 planning conflict (rulesets require a public repo or a paid plan) — the goal's merge-blocking mechanism works correctly in the repo's actual (public) state, which is what was verified live.

---

*Verified: 2026-09-10T14:42:23Z*
*Verifier: Claude (gsd-verifier)*
