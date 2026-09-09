# Phase 20: CI Gate - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-09-09
**Phase:** 20-CI Gate
**Areas discussed:** Triggers & merge gate, Job layout & release reuse, Hang guard policy, Toolchain & supply chain

---

## Triggers & merge gate

### Q1 — The review's snippet says `push: branches: [main]`, but this repo has no `main`. What should trigger the gate?

| Option | Description | Selected |
|--------|-------------|----------|
| All pushes + all PRs | No branch filter; every push to every branch runs the gate | |
| Named branches + all PRs | Long-lived branches plus every PR; needs the canonical branch confirmed | |
| PRs only | Purely a merge gate; direct pushes ungated | |

**User's choice:** Free text — "master is the branch, not main. As you found, there is no main, but there is a master."
**Notes:** Resolved a factual gap rather than selecting an option. `origin/HEAD` is not set locally, which is why the canonical branch was ambiguous from the repo alone.

### Q2 — With `master` as canonical, how wide should the push trigger be?

| Option | Description | Selected |
|--------|-------------|----------|
| master + all PRs (Recommended) | `push: branches: [master]`, `pull_request: {}`; scratch/agent branches quiet until they open a PR | ✓ |
| Every push, every branch | Literal match for CI-01's wording; noisy, and PR branches get double runs | |
| master + development + all PRs | Gate both long-lived branches directly | |

**User's choice:** master + all PRs

### Q3 — Criterion #1 says a red run "blocks merge", but that's a repo setting. How should the phase deliver it?

| Option | Description | Selected |
|--------|-------------|----------|
| Apply it via `gh api` (Recommended) | Create the ruleset in-phase and read it back to prove it stuck | ✓ |
| Document as a manual step | Ship the click-path in the contributing doc; verification is the user's word | |
| Ruleset committed as config + manual apply | Version-controlled JSON, applied by hand; can drift silently | |

**User's choice:** Apply it via `gh api`
**Notes:** `gh` was confirmed authenticated as `kdknigga` against the canonical remote, so this is executable in-phase.

### Q4 — Where should the contributing docs describing the gate live?

| Option | Description | Selected |
|--------|-------------|----------|
| New root CONTRIBUTING.md (Recommended) | GitHub surfaces it in the PR and issue UI | ✓ |
| Docs site page | Fits the existing Diataxis structure but invisible from the PR UI | |
| Both — root file pointing at docs | Best discoverability, two files to keep in sync | |

**User's choice:** New root CONTRIBUTING.md
**Notes:** No CONTRIBUTING.md exists in the repo today — this creates one.

---

## Job layout & release reuse

### Q1 — How should the five checks be laid out across jobs?

| Option | Description | Selected |
|--------|-------------|----------|
| One job, all five steps (Recommended) | One `libsane-dev` install and one resolve; fails at the first bad step | |
| Split: lint/types vs tests | Two parallel jobs; both failure classes visible in the same run | ✓ |
| Four parallel jobs | Maximum parallelism; four apt installs and four resolves per run | |
| One job, non-failing steps | `continue-on-error` plus an aggregate step; non-standard UI semantics | |

**User's choice:** Split: lint/types vs tests
**Notes:** Chose against the recommendation. Accepts a second `libsane-dev` install and dependency resolve in exchange for seeing lint and test failures in the same run.

### Q2 — Should `release.yml`'s test job be refactored to `workflow_call` reuse in this phase?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — wire the reuse now | Gate defined once from day one; closes the hole M-25 names | |
| No — leave release.yml to Phase 31 (Recommended) | Phase 31 owns every release.yml change (M-26) | ✓ |
| Add the `workflow_call` trigger only | Callable seam now, wired in Phase 31; untested until then | |

**User's choice:** No — leave release.yml to Phase 31
**Notes:** The duplicate weaker test job in `release.yml` is harmless in the interim because it fires only on tags, and this project never tags.

### Q3 — What happens to the 3 `@pytest.mark.browser` tests?

| Option | Description | Selected |
|--------|-------------|----------|
| Excluded, no browser job (Recommended) | Matches CI-01; Phase 26 adds the job with vendoring and the C-10 test | ✓ |
| Add a browser job now, blocking | Full coverage now, but CDN-loaded assets and a hard-coded port mean likely flake | |
| Browser job, non-blocking | Signal without red-blocking; a check nobody must care about gets ignored | |

**User's choice:** Excluded, no browser job

### Q4 — How should the red-run proof for criterion #1 be produced?

**First pass** — options offered: throwaway PR with four seeded breaks (recommended) / four separate throwaway pushes / local `act` simulation / verify against real Phase 21 work. **User selected: local `act` simulation.**

Follow-up revealed `act` was not installed and needs Docker. When asked how the plan should handle that, the user declined the question and asked: **"What is it we're trying to test here?"**

The criterion was then decomposed against what is genuinely uncertain:

| Claim | Actually in doubt? |
|---|---|
| The four tools exit non-zero on a problem | No — their own contract |
| A non-zero step reddens a GitHub job | No — platform contract |
| The workflow triggers and gets through setup to reach the checks | **Yes** |
| Nothing swallows a failure | **Yes**, mildly |
| A red check blocks merge | **Yes** — but that's the ruleset, not the workflow |

`act` was found to be weak on exactly the two uncertain items: it runs its own runner images (so a green `act` says nothing about `ubuntu-latest` + `libsane-dev` + `uv sync --locked`) and knows nothing about rulesets.

**Second pass:**

| Option | Description | Selected |
|--------|-------------|----------|
| Green run + ruleset + one seeded break (Recommended) | Green proves setup; ruleset read-back proves blocking; one one-line break proves red propagates | ✓ |
| Keep act | Fast local iteration, but different runner images and silent on merge-blocking | |
| Four seeded breaks on real GitHub | Literal reading of the criterion; four setup runs to confirm four linters return non-zero | |
| Green run + ruleset only | Cheapest; catches no swallowed failure | |

**User's choice:** Green run + ruleset + one seeded break — superseding the earlier `act` answer.
**Notes:** This is the most consequential exchange in the discussion. The user's challenge changed the deliverable: the phase no longer installs `act`, no longer needs Docker, and no longer runs a per-tool break matrix. Recorded as D-08/D-09/D-10 with an explicit instruction that a planner must not silently restore the matrix.

---

## Hang guard policy

### Q1 — Where should the timeout be configured?

| Option | Description | Selected |
|--------|-------------|----------|
| pyproject, applies everywhere (Recommended) | Local and CI behave identically; kills the "hangs only in CI" bug class | ✓ |
| CI-only flag | `--timeout=60` in the workflow; local debugging undisturbed | |
| pyproject generous + tight CI override | Two numbers; local value too loose to catch much | |

**User's choice:** pyproject, applies everywhere

### Q2 — Which timeout method?

| Option | Description | Selected |
|--------|-------------|----------|
| signal (Recommended) | Fails one test with a traceback, tears down fixtures, run continues | ✓ |
| thread | All-thread stack dump, survives a wedged main thread, but `os._exit(1)` ends the run | |
| signal by default, thread where needed | Per-test markers on the concurrency tests | |

**User's choice:** signal
**Notes:** The per-test `@pytest.mark.timeout(60, method="thread")` escape hatch was documented in CONTEXT.md for Phases 22/26 to use if a two-thread deadlock ever needs an all-thread dump.

### Q3 — What per-test timeout value?

Measured live during the discussion: **332 tests, 26.8s total, slowest test 3.0s.**

| Option | Description | Selected |
|--------|-------------|----------|
| 60s (Recommended) | ~20x the slowest test; matches project research | ✓ |
| 30s | 10x headroom; faster deadlock feedback, tighter margin for later thread tests | |
| 15s | 5x headroom; likely needs raising once real-thread worker tests land | |

**User's choice:** 60s

### Q4 — Also add `session_timeout`?

| Option | Description | Selected |
|--------|-------------|----------|
| No — per-test timeout is enough (Recommended) | Job-level `timeout-minutes` is the cleaner run-level backstop | ✓ |
| Yes — add a session cap | Catches many-tests-got-slower; needs revisiting as the suite grows | |
| Job-level `timeout-minutes` instead | Bounds setup too, but yields "cancelled" not a pytest report | |

**User's choice:** No — per-test timeout is enough

---

## Toolchain & supply chain

**Correction issued during this area:** the initial framing repeated research's claim that `ty`/`pyrefly` must be bumped "so CI and local agree." Checking `uv.lock` showed that `uv sync --locked` already makes them agree (lock pins `ty` 0.0.24, `pyrefly` 0.57.1), so that rationale did not apply. All four checks were also confirmed passing clean at those versions.

### Q1 — Bump `ty` (0.0.24 → 0.0.79) and `pyrefly` (0.57.1 → 1.2+) in this phase?

| Option | Description | Selected |
|--------|-------------|----------|
| No — gate the locked versions (Recommended) | First run green; avoids source changes the roadmap excludes | |
| Yes — bump now, fix what falls out | Gate guards current tooling from day one; breaks zero-source-changes | ✓ |
| Bump in its own phase later | Cleanest separation; no roadmap home yet | |

**User's choice:** Yes — bump now, fix what falls out
**Notes:** Chose against the recommendation, and against the ROADMAP's "zero source changes in this phase" note. The concern was raised in the option description and the user selected it anyway; recorded in CONTEXT.md as D-16 with an explicit statement that the roadmap note is superseded and that type-error fixes are planned work, not an execution deviation.

### Q2 — Sequencing?

| Option | Description | Selected |
|--------|-------------|----------|
| CI first green, then bump (Recommended) | A red run after the bump unambiguously implicates the bump | ✓ |
| Bump first, then CI | One green run, but type errors fixed with no gate watching | |
| Both in one commit | Fewest commits, worst diagnosis story | |

**User's choice:** CI first green, then bump

### Q3 — How much supply-chain hardening lands here?

| Option | Description | Selected |
|--------|-------------|----------|
| SHA-pin ci.yml + dependabot (Recommended) | Pin what this phase introduces; Dependabot in the same commit | ✓ |
| Pin all three workflows now | Consistent repo-wide, but touches release.yml which Phase 31 rewrites | |
| Version tags now, pin in Phase 31 | Simplest to read; gate trusts mutable tags for 11 phases | |
| Pin + dependabot + zizmor as a check | Strongest, but adds a sixth check and reddens CI over Phase 31's broken file | |

**User's choice:** SHA-pin ci.yml + dependabot

---

## Claude's Discretion

- `libsane-dev` installed before `uv sync --locked` in every job (`python-sane` is sdist-only).
- `uv sync --locked` over bare `uv sync`.
- Workflow-level `permissions: { contents: read }`.
- `concurrency` group with `cancel-in-progress`.
- uv caching via `astral-sh/setup-uv`.
- `timeout-minutes` on both jobs as the run-level backstop.
- The prose and structure of `CONTRIBUTING.md`, within the content requirements agreed in Q4 of area 1.
- Cleanup of the throwaway branch used for the seeded break.
- Adding `pytest-timeout` to `[dependency-groups].dev` as a plain requirement rather than an optional extra (applying the standing preference on mandatory dependencies; surfaced and accepted at the area 3 hand-off).

## Deferred Ideas

- A browser-test CI job — Phase 26, with htmx/PicoCSS vendoring, the C-10 regression test, and the `test_browser.py` hard-coded port (M-34).
- `workflow_call` reuse so the gate is defined once — Phase 31, with the rest of M-26.
- SHA-pinning `release.yml` and `docs.yml` — Phase 31.
- `zizmor` as a workflow-audit CI step — Phase 31, once `release.yml` is no longer known-broken.
- The CI-02 naming grep guard — already assigned to Phase 31 by the roadmap.
- Bumping the rest of the toolchain (`ruff`, `pytest`, `playwright`, `pytest-playwright`, `prek`, `mkdocs-material`, and the runtime pins) — not discussed; Phase 32's sweep is the natural home.

**No scope creep occurred during this discussion** — every thread stayed inside the CI-gate boundary. The deferrals above are pre-existing roadmap assignments plus the toolchain remainder, not redirected requests.
