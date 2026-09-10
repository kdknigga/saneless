# Phase 20: CI Gate - Context

**Gathered:** 2026-09-09
**Status:** Ready for planning

<domain>
## Phase Boundary

A GitHub Actions workflow that runs ruff check, `ruff format --check`, `ty`, `pyrefly`, and the non-browser pytest suite on every push to `master` and every pull request, with a branch ruleset that makes a red run block merge, plus `pytest-timeout` so a hung test cannot consume the CI job's time budget.

Requirements: **CI-01**, **TEST-07**.

**In scope:** `.github/workflows/ci.yml`, `.github/dependabot.yml`, `pytest-timeout` added to the dev group and configured in `pyproject.toml`, a branch ruleset on `master`, a new root `CONTRIBUTING.md`, and — by explicit user decision — bumping `ty` and `pyrefly` to current and fixing whatever type errors that surfaces.

**Out of scope:** the CI-02 naming grep guard (Phase 31), any change to `release.yml` or `docs.yml` (Phase 31, M-26/N-31), a browser-test job (Phase 26, alongside htmx/PicoCSS vendoring and the C-10 regression test).

**Also in scope, forced by an empty remote (see D-20..D-24):** switching the repo to private, the first push, a filtered (`.planning/`-stripped) branch, and an unmerged pull request. **Merging that PR is out of scope permanently — see D-21.**

</domain>

<decisions>
## Implementation Decisions

### Triggers and merge gate

- **D-01:** The canonical branch is **`master`**. There is no `main`. The review's own `ci.yml` snippet (`push: { branches: [main] }`) is wrong for this repo and must not be copied literally. `origin/HEAD` is not set locally.
- **D-02:** Triggers are `push: { branches: [master] }` and `pull_request: {}`. Direct pushes to `master` and every PR from any branch are gated. `development`, `autodev`, and the `worktree-agent-*` branches do not run the gate until they open a PR — deliberate, so in-progress agent commits neither burn Actions minutes nor produce red runs from half-finished work.
- **D-03:** "A red run blocks merge" is delivered by **applying a branch ruleset on `master` via `gh api`** during the phase, then reading it back to prove it stuck — not by documenting a click-path. `gh` is already authenticated as `kdknigga` with the canonical remote `git@github.com:kdknigga/saneless.git`, so this is executable in-phase.
- **D-04:** The contributing docs live in a **new root `CONTRIBUTING.md`** (none exists today). Root placement is deliberate: GitHub surfaces it in the PR and issue UI. It must describe the five checks, the `libsane-dev` prerequisite, `uv sync --locked`, running `uv run prek run` locally, and that `--no-verify` no longer bypasses the gate.

### Job layout and workflow scope

- **D-05:** **Two parallel jobs**, not one and not four: a lint/types job (ruff check, `ruff format --check`, `ty`, `pyrefly`) and a tests job (`pytest -m "not browser"`). Rationale: lint and test failures surface in the same run rather than serially, and the fast job returns in well under a minute. Accepted cost: a second `libsane-dev` install and dependency resolve per run — the lint job needs the venv for `ty`/`pyrefly` regardless.
- **D-06:** **`release.yml` is not touched in this phase.** No `workflow_call` refactor, no trigger added. All of it belongs to Phase 31 (M-26), which must fix the unresolvable `pypa/gh-action-pypi-publish@v1.12` ref, the missing SANE headers, and the browser tests with no browser anyway. The duplicate, weaker test job in `release.yml` is harmless in the interim because it fires only on tags, and this project never tags.
- **D-07:** The three `@pytest.mark.browser` tests in `tests/test_browser.py` are **excluded via `-m "not browser"`** and no browser job is added. Phase 26 adds it, alongside the vendoring and the C-10 Scan-button regression test that make browser CI reliable. Adding one now would buy CDN flake and a hard-coded-port problem (M-34) that Phase 26 must fix regardless.

### Verification of the success criteria

- **D-08:** Success criterion 1 is proven by **three complementary pieces**, not by seeding a break per tool:
  1. the **green run** proves the workflow triggers and gets through `libsane-dev` + `uv sync --locked` to actually execute all five checks — the setup path is the genuinely uncertain part;
  2. the **`gh api` ruleset read-back** (D-03) proves the merge-blocking half, which nothing else can;
  3. **one single one-line seeded break** on a throwaway branch proves a failure propagates to red and nothing swallows it (a stray `continue-on-error`, a `|| true`, a step-ordering bug).
- **D-09:** **`act` is not used and must not be introduced.** It was considered and rejected on the grounds that it runs its own runner images (so a green `act` says nothing about `ubuntu-latest` + `libsane-dev` + `uv sync --locked`) and knows nothing about rulesets (so it cannot address merge-blocking at all). It is also not installed and would require Docker. Do not add it as a plan task.
- **D-10:** Deliberately **not** individually demonstrating each of ruff / `ty` / `pyrefly` / pytest going red. Their exit codes are their own contract, not this project's code; four runs of setup overhead to confirm four linters return non-zero is not what criterion 1 is protecting.

### Hang guard (TEST-07)

- **D-11:** `pytest-timeout` is configured in **`[tool.pytest.ini_options]` in `pyproject.toml`**, not as a CI-only `--timeout` flag, so a deadlock introduced locally fails locally exactly as it would in CI. This removes the "passes on my machine, hangs in CI" failure class.
- **D-12:** `timeout = 60`. **Measured baseline: 332 tests pass in 26.8s; slowest single test is 3.0s** (`test_upload_retry_on_network_error` and two siblings, all sleep-driven). 60s is ~20x headroom, so it will not fire spuriously even as Phases 22 and 26 add legitimately slower real-thread tests. Its job is catching an infinite hang, not policing slowness.
- **D-13:** `timeout_method` stays **`signal`** (the POSIX default). A hung test fails with a traceback, fixtures tear down, and the rest of the suite still runs — one deadlock yields one red test rather than a red run with no detail. `thread` was rejected because it ends the process with `os._exit(1)`: no teardown, no remaining tests, no report. If a future concurrency test specifically needs an all-thread stack dump, `@pytest.mark.timeout(60, method="thread")` is the documented per-test escape hatch, and it belongs in Phase 22/26 with that test — not here.
- **D-14:** **No `session_timeout`.** The per-test timeout already bounds the run, and a job-level `timeout-minutes` is the cleaner backstop because it covers apt and `uv sync` too, not just pytest.
- **D-15:** `pytest-timeout` goes into `[dependency-groups].dev` as a plain requirement. Project research described it as "optional"; it is not optional here — the gate depends on it. (Consistent with the standing preference that a dependency the system depends on is never hedged as optional.)

### Toolchain and supply chain

- **D-16:** **`ty` and `pyrefly` are bumped to current in this phase**, and whatever type errors the newer checkers surface get fixed here. `ty` 0.0.24 → ~0.0.79, `pyrefly` 0.57.1 → 1.2+ (crossing 1.0).
  - **This overrides the ROADMAP note "zero source changes in this phase."** Type-error fixes are in scope for Phase 20 by explicit user decision; the planner must treat them as planned work, not as an execution deviation.
  - Note for the planner: the "CI and local disagree" rationale from `.planning/research/STACK.md` does **not** apply here — `uv sync --locked` already makes CI and local resolve identically. The bump is being taken on its own merits (not carrying a stale pre-1.0 type checker into a milestone that gates on it), with the type-error fallout accepted.
- **D-17:** **Sequence: CI first, proven green on the locked versions, then the bump as a separate commit.** All four checks pass clean at `ty` 0.0.24 / `pyrefly` 0.57.1 / `ruff` 0.15.7 today (verified during discussion), so the workflow's first run is green and the gate is proven working before anything else moves. A red run after the bump then unambiguously implicates the bump rather than the workflow. Do not collapse these into one commit.
- **D-18:** SHA-pin the actions **`ci.yml` introduces**, each with a `# vX.Y.Z` trailing comment, and add **`.github/dependabot.yml`** with the `github-actions` ecosystem **in the same commit** — pinning without Dependabot freezes the pins forever, including the security fixes pinning exists to control (research Pitfall 19).
- **D-19:** `release.yml` and `docs.yml` pinning, and `zizmor`, are **Phase 31's**. `zizmor` was specifically declined as a CI step here: it would add a sixth check to a gate CI-01 defines as five, and a finding against Phase 31's known-broken `release.yml` would redden CI over a file this phase deliberately does not own.

### Repository publication (added 2026-09-09 after research surfaced an empty remote)

Research found — and I independently confirmed — that `github.com/kdknigga/saneless` **has never been pushed to**: `git ls-remote origin` returns no refs at all. Locally `master` is a single root commit (`a87b3dd`, no `.planning/` in its tree) and `autodev` holds 335 commits, 334 ahead of `master` and 0 behind. No branch has an upstream. Phase 20's success criteria are unreachable until this is resolved, so the following were decided with the user:

- **D-20:** Push `autodev`'s work and **open a pull request into `master`**. The gate's first run therefore happens on a real PR, which is what success criteria 1 and 2 describe ("visible on the pull request").
- **D-21: DO NOT MERGE THE PULL REQUEST.** The user does all merging, without exception. No plan task, no executor step, and no subagent may run `gh pr merge` or `git merge` into `master`. The phase's terminal state is "PR open and green (or red, for the seeded break), handed to the user." If a plan draft contains a merge step, it is wrong and must be removed. This is a standing user rule, not a phase-local preference.
- **D-22:** **`.planning/` is stripped from everything pushed.** The planning trail — roadmaps, the 2026-09-09 code review, research, discussion logs, debug write-ups — stays local. Use `/gsd-pr-branch`, which exists precisely to build a clean branch filtering out `.planning/` commits. Consequence the plan must account for: pushed history is rewritten and permanently diverges from local history, so every later push needs the same filtering. `master`'s root commit contains no `.planning/` and so survives filtering unchanged.
- **D-23:** **The repository is switched to private before the first push.** It is currently PUBLIC and empty (verified: `gh repo view` → `"visibility":"PUBLIC"`). Making it private must be the *first* outward-facing action in the phase — after a push it is too late. CI, rulesets, and Actions all work on private repos. GHCR/PyPI publishing visibility is Phase 31's problem, not this phase's.
- **D-24:** No secrets are published by this. Verified before deciding: `saneless.toml` is gitignored (`.gitignore:312`) and untracked, no token- or key-shaped strings exist in any tracked file, `site/` is not committed. `.planning/debug/paperless-token-test.md` discusses a token-validation *bug* and contains no credential — and is stripped by D-22 regardless.

- **D-25:** The filtered branch is built with **`uvx git-filter-repo --path .planning --invert-paths`**, not by cherry-picking and not by squashing. Measured: `master..autodev` is **338 commits, of which 229 touch only `.planning/`** and become empty once it is stripped, leaving **109 real code commits**. filter-repo removes the path across all history, drops the emptied commits automatically, and preserves the 109 commits' original messages, authorship, and dates — so the published repo has a real development history. Squashing to one commit was explicitly rejected: for a project being prepared for open-source release, publishing no history is a real loss. Cherry-picking was rejected as less reliable for the same result.
  - **Safety, non-negotiable: filter-repo runs on a scratch clone, NEVER on `/home/kris/git/saneless`.** It rewrites history in place and removes the `origin` remote. The working repo must be untouched by it; verify `git rev-list --count autodev` is still 335 and `git remote -v` still lists `origin` afterwards.
  - `git-filter-repo` is not installed and must not be permanently installed — `uvx` runs it ephemerally (confirmed available).
  - This supersedes Plan 20-02 Task 2's original `/gsd-pr-branch` + cherry-pick approach and its single-squashed-commit fallback.

**Ordering these impose on the plan:** repo→private → push `master` → build filtered branch from `autodev` → push it → open PR (do not merge) → CI runs on the PR → read check names back from the run → apply the ruleset → seeded-break PR → then the D-16 bump. Note that `ci.yml` must be committed *before* the filtered branch is built, or there is no workflow on the PR head for `pull_request` to trigger.

### Claude's Discretion

- `sudo apt-get update && sudo apt-get install -y libsane-dev` **before** `uv sync --locked` in every job — `python-sane` 2.9.2 is sdist-only and compiles against `sane/sane.h`.
- `uv sync --locked` (never bare `uv sync`) so a stale lockfile turns CI red instead of silently resolving around it.
- Workflow-level `permissions: { contents: read }`; no job in `ci.yml` needs more.
- `concurrency` group with `cancel-in-progress` so fast-follow pushes don't stack runs.
- uv caching via `astral-sh/setup-uv`.
- `timeout-minutes` on both jobs as the run-level backstop (see D-14).
- The exact prose and structure of `CONTRIBUTING.md`, within the content requirements in D-04.
- Cleanup of the throwaway branch used for the seeded break in D-08.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### The findings this phase resolves
- `.planning/reviews/2026-09-09-code-review.md` § M-25 (~line 688) — the finding behind CI-01: what is missing, why it matters, and a starting `ci.yml` snippet. **Its `branches: [main]` is wrong for this repo — see D-01.** Also names the `workflow_call` reuse idea that D-06 defers to Phase 31.
- `.planning/reviews/2026-09-09-code-review.md` § M-26 — the release-workflow breakage. Read to understand what Phase 31 owns and therefore what this phase must not touch.
- `.planning/reviews/2026-09-09-code-review.md` § M-33 — the finding behind TEST-07.
- `.planning/REQUIREMENTS.md` § "CI Gate" and § "Test Suite Hygiene" — CI-01 and TEST-07 verbatim; CI-02 is listed there but belongs to Phase 31.

### Research that constrains the implementation
- `.planning/research/STACK.md` § 9 "Workflow requirements the milestone must satisfy" (~lines 320–340) — `libsane-dev` before `uv sync --locked`, `uv sync --locked` over bare `uv sync`, `-m "not browser"` in the fast gate, both type checkers, per-workflow `permissions`. Also the action version/SHA table (~lines 14–24) for D-18.
- `.planning/research/STACK.md` § 10 (~line 371) — `pytest-timeout` `timeout = 60` as the CI hang guard, and the explicit rejection of freezegun and time-machine.
- `.planning/research/STACK.md` (~lines 58–66) — the `ty` / `pyrefly` / `zizmor` / `pytest-timeout` dependency table behind D-15 and D-16.
- `.planning/research/PITFALLS.md` § Pitfall 19 "permissions: blocks break OIDC, and SHA pins rot without Dependabot" (~lines 428–445) — why D-18 pairs pinning with Dependabot in one commit, and why workflow-level `permissions` must be paired with job-level escalation in the workflows that need it (relevant to Phase 31, not to `ci.yml`).
- `.planning/research/PITFALLS.md` § Pitfall 20 (~lines 447–470) — why the browser job waits for Phase 26's vendoring (D-07).
- `.planning/research/ARCHITECTURE.md` (~lines 296–320) — why CI is step 0 rather than the review's step 7.

### Phase and milestone framing
- `.planning/ROADMAP.md` § "Phase 20: CI Gate" — goal and the three success criteria. **Its "zero source changes in this phase" note is superseded by D-16.**
- `.planning/ROADMAP.md` § Overview, ordering point 1 — why CI lands first.
- `.planning/PROJECT.md` § "Current Milestone: v2.0 Prep for release" — milestone framing and the "No git tags" constraint.

### Upstream tool documentation
- pytest-timeout configuration reference (`_autodocs/configuration.md`, `_autodocs/errors.md`) — `timeout`, `timeout_method`, `session_timeout`, and the `signal`-fails-a-test vs `thread`-calls-`os._exit(1)` distinction behind D-13. Fetched via Context7 (`/pytest-dev/pytest-timeout`) during discussion.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `.github/workflows/release.yml` — its `test` job is the closest existing analog for a check job (checkout → `astral-sh/setup-uv` → `uv sync` → ruff → pytest). Useful as a shape reference, but it is missing `libsane-dev`, `--locked`, both type checkers, and the browser exclusion. **Read it; do not edit it** (D-06).
- `.github/workflows/docs.yml` — the other existing workflow. Also not to be edited here (its `pip install mkdocs-material` problem is N-31, Phase 31).
- `.pre-commit-config.yaml` lines 41–62 — already runs ruff, ruff-format, `uv run ty check`, and `uv run pyrefly check` as local hooks. The CI job should run the same four commands so local and CI agree; the difference is that CI adds pytest and cannot be skipped with `--no-verify`.
- `pyproject.toml` `[tool.pytest.ini_options]` (line 141) — already has `testpaths`, the registered `browser` marker, `--strict-markers`, `--strict-config`, `xfail_strict`, and `filterwarnings = ["error"]`. `timeout` and `timeout_method` are added into this existing block (D-11).
- `pyproject.toml` `[dependency-groups].dev` (line 152) — where `pytest-timeout` is added (D-15).

### Established Patterns
- The `browser` marker is already registered and applied to exactly 3 tests in `tests/test_browser.py`; `-m "not browser"` deselects them cleanly (verified: 332 selected, 8 deselected).
- `filterwarnings = ["error"]` is already in force, so a new dependency that emits a `DeprecationWarning` at import time will turn the suite red. Relevant to adding `pytest-timeout` and to the `ty`/`pyrefly` bump.
- Both type checkers are treated as mandatory and equal in `CLAUDE.md` — issues are fixed, never suppressed with `# type: ignore` or `# noqa`. This governs the D-16 bump fallout.

### Integration Points
- New file `.github/workflows/ci.yml` — the phase's primary artifact.
- New file `.github/dependabot.yml` — D-18.
- New file `CONTRIBUTING.md` at repo root — D-04.
- `pyproject.toml` — `[dependency-groups].dev` and `[tool.pytest.ini_options]`.
- `uv.lock` — regenerated by adding `pytest-timeout` and again by the `ty`/`pyrefly` bump.
- `src/` — touched only by D-16's type-error fixes, and only in the second commit (D-17).
- GitHub repo settings (`kdknigga/saneless`) — the `master` branch ruleset, applied via `gh api` (D-03). Outside the repo; verified by read-back.

### Verified baseline (measured during discussion, 2026-09-09)
- `uv run ruff check .` → no issues. `uv run ruff format --check .` → 37 files already formatted. `uv run ty check` → all checks passed. `uv run pyrefly check` → 0 errors.
- `uv run pytest -m "not browser"` → **332 passed, 8 deselected, 26.8s**; slowest test 3.0s.
- Locked versions: `ty` 0.0.24, `pyrefly` 0.57.1, `ruff` 0.15.7, `pytest` 9.0.2.
- `gh` authenticated as `kdknigga`; `act` **not** installed.
- The tree is clean of check failures, so the first CI run is expected green (D-17 depends on this).

</code_context>

<specifics>
## Specific Ideas

- The user pushed back on the framing of success criterion 1 mid-discussion — asking "what is it we're trying to test here?" — and the criterion was re-derived from what is genuinely uncertain (setup path, failure propagation, merge-blocking) rather than from its literal wording. D-08, D-09, and D-10 are the result. **A planner should not silently restore a per-tool seeded-break matrix.**
- The measured baseline above was gathered specifically to ground the `timeout` value rather than adopt 60s on research's say-so. It happens to agree with research; the number is now defensible from data.

</specifics>

<deferred>
## Deferred Ideas

- **A browser-test CI job** (`playwright install --with-deps chromium`, `-m browser`) — Phase 26, together with htmx/PicoCSS vendoring and the C-10 Scan-button regression test. `tests/test_browser.py`'s hard-coded port (M-34) needs fixing at the same time.
- **`workflow_call` reuse so the gate is defined once** — Phase 31, with the rest of the `release.yml` fixes (M-26).
- **SHA-pinning `release.yml` and `docs.yml`** — Phase 31.
- **`zizmor` as a workflow-audit CI step** — Phase 31, once `release.yml` is no longer known-broken. Declined here (D-19).
- **The CI-02 naming grep guard** — already assigned to Phase 31 by the roadmap; adding it here would make CI red from its first run, since the rename has not happened.
- **Bumping the rest of the toolchain** (`ruff`, `pytest`, `playwright`, `pytest-playwright`, `prek`, `mkdocs-material`, and the runtime pins in `.planning/research/STACK.md` ~lines 460–472) — not discussed; only `ty` and `pyrefly` are in scope here (D-16). Phase 32's sweep is the natural home.

</deferred>

---

*Phase: 20-CI Gate*
*Context gathered: 2026-09-09*
