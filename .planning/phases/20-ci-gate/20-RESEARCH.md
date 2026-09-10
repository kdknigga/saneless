# Phase 20: CI Gate - Research

**Researched:** 2026-09-09
**Domain:** GitHub Actions CI, GitHub repository rulesets, uv/Python toolchain in CI, pytest hang guards
**Confidence:** HIGH (most findings empirically verified this session)

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Triggers and merge gate**

- **D-01:** The canonical branch is **`master`**. There is no `main`. The review's own `ci.yml` snippet (`push: { branches: [main] }`) is wrong for this repo and must not be copied literally. `origin/HEAD` is not set locally.
- **D-02:** Triggers are `push: { branches: [master] }` and `pull_request: {}`. Direct pushes to `master` and every PR from any branch are gated. `development`, `autodev`, and the `worktree-agent-*` branches do not run the gate until they open a PR — deliberate, so in-progress agent commits neither burn Actions minutes nor produce red runs from half-finished work.
- **D-03:** "A red run blocks merge" is delivered by **applying a branch ruleset on `master` via `gh api`** during the phase, then reading it back to prove it stuck — not by documenting a click-path. `gh` is already authenticated as `kdknigga` with the canonical remote `git@github.com:kdknigga/saneless.git`, so this is executable in-phase.
- **D-04:** The contributing docs live in a **new root `CONTRIBUTING.md`** (none exists today). Root placement is deliberate: GitHub surfaces it in the PR and issue UI. It must describe the five checks, the `libsane-dev` prerequisite, `uv sync --locked`, running `uv run prek run` locally, and that `--no-verify` no longer bypasses the gate.

**Job layout and workflow scope**

- **D-05:** **Two parallel jobs**, not one and not four: a lint/types job (ruff check, `ruff format --check`, `ty`, `pyrefly`) and a tests job (`pytest -m "not browser"`). Rationale: lint and test failures surface in the same run rather than serially, and the fast job returns in well under a minute. Accepted cost: a second `libsane-dev` install and dependency resolve per run — the lint job needs the venv for `ty`/`pyrefly` regardless.
- **D-06:** **`release.yml` is not touched in this phase.** No `workflow_call` refactor, no trigger added. All of it belongs to Phase 31 (M-26). The duplicate, weaker test job in `release.yml` is harmless in the interim because it fires only on tags, and this project never tags.
- **D-07:** The three `@pytest.mark.browser` tests in `tests/test_browser.py` are **excluded via `-m "not browser"`** and no browser job is added. Phase 26 adds it.

**Verification of the success criteria**

- **D-08:** Success criterion 1 is proven by **three complementary pieces**, not by seeding a break per tool: (1) the **green run**; (2) the **`gh api` ruleset read-back**; (3) **one single one-line seeded break** on a throwaway branch.
- **D-09:** **`act` is not used and must not be introduced.**
- **D-10:** Deliberately **not** individually demonstrating each of ruff / `ty` / `pyrefly` / pytest going red.

**Hang guard (TEST-07)**

- **D-11:** `pytest-timeout` is configured in **`[tool.pytest.ini_options]` in `pyproject.toml`**, not as a CI-only `--timeout` flag.
- **D-12:** `timeout = 60`.
- **D-13:** `timeout_method` stays **`signal`** (the POSIX default).
- **D-14:** **No `session_timeout`.** Job-level `timeout-minutes` is the backstop.
- **D-15:** `pytest-timeout` goes into `[dependency-groups].dev` as a plain requirement.

**Toolchain and supply chain**

- **D-16:** **`ty` and `pyrefly` are bumped to current in this phase**, and whatever type errors the newer checkers surface get fixed here. **This overrides the ROADMAP note "zero source changes in this phase."**
- **D-17:** **Sequence: CI first, proven green on the locked versions, then the bump as a separate commit.** Do not collapse these into one commit.
- **D-18:** SHA-pin the actions **`ci.yml` introduces**, each with a `# vX.Y.Z` trailing comment, and add **`.github/dependabot.yml`** with the `github-actions` ecosystem **in the same commit**.
- **D-19:** `release.yml` and `docs.yml` pinning, and `zizmor`, are **Phase 31's**.

### Claude's Discretion

- `sudo apt-get update && sudo apt-get install -y libsane-dev` **before** `uv sync --locked` in every job.
- `uv sync --locked` (never bare `uv sync`).
- Workflow-level `permissions: { contents: read }`.
- `concurrency` group with `cancel-in-progress`.
- uv caching via `astral-sh/setup-uv`.
- `timeout-minutes` on both jobs.
- The exact prose and structure of `CONTRIBUTING.md`, within the content requirements in D-04.
- Cleanup of the throwaway branch used for the seeded break in D-08.

### Deferred Ideas (OUT OF SCOPE)

- A browser-test CI job — Phase 26.
- `workflow_call` reuse so the gate is defined once — Phase 31.
- SHA-pinning `release.yml` and `docs.yml` — Phase 31.
- `zizmor` as a workflow-audit CI step — Phase 31.
- The CI-02 naming grep guard — Phase 31.
- Bumping the rest of the toolchain (`ruff`, `pytest`, `playwright`, `pytest-playwright`, `prek`, `mkdocs-material`) — Phase 32.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| **CI-01** | Every push and pull request runs ruff check, ruff format --check, ty, pyrefly, and the non-browser pytest suite in GitHub Actions, and a red run blocks merge [M-25] | § Standard Stack (pinned action SHAs), § Code Examples (`ci.yml`), § Ruleset API (exact `gh api` calls + verified context-name semantics), § Pitfall 1 (the remote is empty — blocks the whole requirement until resolved) |
| **TEST-07** | `pytest-timeout` guards the suite so a hung thread test cannot block CI forever [M-33] | § Standard Stack (pytest-timeout 2.4.0 verified), § Code Examples (`pyproject.toml` block), § Pitfall 7 (`filterwarnings = ["error"]` interaction — empirically cleared), § Validation Architecture (timeout firing verified end-to-end) |
</phase_requirements>

---

## Summary

Almost everything CONTEXT.md decided is directly implementable, and this session verified the mechanics end-to-end rather than inferring them. `pytest-timeout` 2.4.0 was run against the real suite with `timeout = 60` / `timeout_method = "signal"` and produced **332 passed, 8 deselected in 26.78s with zero warnings**, so the `filterwarnings = ["error"]` interaction is a non-issue; a separate 3-second repro confirmed the signal method fails exactly one test with a traceback and lets the rest of the suite finish. `-m "not browser"` was re-run with Playwright's browser cache pointed at an empty directory and still passed 332/8, proving no `playwright install` step is needed. The D-16 bump was dry-run: **pyrefly 1.2.0 reports 0 errors** (the 0.x→1.x crossing is clean), and **ty 0.0.80 reports exactly 3 errors** — and those 3 sites are precisely the repo's only 3 `# type: ignore[...]` comments, because ty stopped honouring bracketed mypy codes somewhere between 0.0.24 and 0.0.80.

The highest-risk unknown named in the brief — how the required status check's *name* must be spelled — is now settled from GitHub's own documentation source: for a workflow check the name format is **`<job name>`**, and "required status checks do not take workflow, matrix, or event trigger types into account." The full ruleset REST schema was extracted from GitHub's OpenAPI description, including the non-obvious fact that `strict_required_status_checks_policy` is a **required** parameter, and that the `workflows` rule (which would sidestep job names entirely) is **GHEC/GHES-only** and therefore unavailable to this free public repo.

**One finding, however, invalidates the phase's central assumption.** `github.com/kdknigga/saneless` is an **empty repository** — zero refs, `size: 0`, and `GET /commits` returns `409 Git Repository is empty`. Locally, `master` holds a single "Initial commit" and is **333 commits behind `autodev`**, where all real work lives, and no branch has an upstream. Nothing has ever been pushed. Until a push happens there can be no Actions run, no check-run names to reference, and no PR — so success criteria 1 and 2 are unreachable and the ruleset would be created against names that have never been reported. This is not a research gap; it is a sequencing and branch-reconciliation decision that CONTEXT.md's 19 decisions do not cover, and it needs the user before planning completes.

**Primary recommendation:** Resolve the publish-and-branch-reconciliation question with the user first; then land `ci.yml` + `dependabot.yml` + `pytest-timeout` on a pushed branch, let it go green, read the **actual** check-run names back from `GET /commits/{sha}/check-runs`, create the ruleset from those exact strings with `do_not_enforce_on_create: true`, read it back, and only then take the `ty`/`pyrefly` bump as a separate commit fixing the 3 known ty diagnostics.

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Running the five checks | CI runner (GitHub-hosted `ubuntu-latest`) | — | The checks are the same commands `prek` runs locally; CI's only added value is that it cannot be skipped with `--no-verify` |
| Blocking a merge on a red run | GitHub repo settings (branch ruleset) | — | Workflows report status; only a ruleset can *enforce* it. This is server-side config, not code, and is invisible to the repo tree |
| Bounding a hung test | Test config (`pyproject.toml` → pytest-timeout) | CI runner (`timeout-minutes`) | Per-test timeout must live with the tests so it fires locally too (D-11); job `timeout-minutes` backstops apt and `uv sync`, which pytest cannot see (D-14) |
| System library provisioning (`libsane-dev`) | CI runner step (apt) | — | `python-sane` 2.9.2 is sdist-only and compiles against `sane/sane.h`; this cannot be expressed in `uv.lock` |
| Python interpreter + dependency provisioning | `astral-sh/setup-uv` + `uv sync --locked` | — | uv owns interpreter acquisition (managed CPython 3.14) and lock enforcement |
| Action version currency | Dependabot (`.github/dependabot.yml`) | — | SHA pins are frozen by construction; only an automated updater keeps them from rotting (D-18) |
| Contributor-facing description of the gate | `CONTRIBUTING.md` at repo root | — | GitHub surfaces root `CONTRIBUTING.md` in the PR/issue UI (D-04) |

---

## Standard Stack

### Core

| Library / Action | Version | Purpose | Why Standard |
|---|---|---|---|
| `actions/checkout` | **v7.0.1** — commit `3d3c42e5aac5ba805825da76410c181273ba90b1` | Clone the repo into the runner | The canonical checkout action; v7.0.1 is the current release (published 2026-07-20) `[VERIFIED: GitHub API]` |
| `astral-sh/setup-uv` | **v10.0.1** — commit `20cfd1bf945f4377ade1205e4dbc17946fc9a30d` | Install uv, register problem matchers, persist the uv cache | Astral's own action; current release published 2026-08-14 `[VERIFIED: GitHub API]` |
| `pytest-timeout` | **2.4.0** | Per-test hang guard (TEST-07) | The pytest-dev project's own plugin; 24 releases, source at `github.com/pytest-dev/pytest-timeout` `[VERIFIED: PyPI + slopcheck OK]` |
| `ty` | **0.0.80** (from 0.0.24) | Type checker #1 (D-16) | Astral's checker, already in use; 0.0.80 published 2026-09-09 `[VERIFIED: PyPI]` |
| `pyrefly` | **1.2.0** (from 0.57.1) | Type checker #2 (D-16) | Meta's checker, already in use; 1.2.0 published 2026-08-01 `[VERIFIED: PyPI]` |

**How the SHAs were resolved** (so the planner can re-verify at execution time):

```bash
# actions/checkout — v7 is a LIGHTWEIGHT tag, .object.sha IS the commit
gh api repos/actions/checkout/releases/latest --jq '.tag_name'                  # -> v7.0.1
gh api repos/actions/checkout/git/ref/tags/v7.0.1 --jq '.object.sha+" "+.object.type'
#   -> 3d3c42e5aac5ba805825da76410c181273ba90b1 commit

# astral-sh/setup-uv
gh api repos/astral-sh/setup-uv/releases/latest --jq '.tag_name'                # -> v10.0.1
gh api repos/astral-sh/setup-uv/git/ref/tags/v10.0.1 --jq '.object.sha+" "+.object.type'
#   -> 20cfd1bf945f4377ade1205e4dbc17946fc9a30d commit
```

> **Gotcha the planner must not trip over:** older `astral-sh/setup-uv` tags (e.g. `v7`) are **annotated** tags, where `.object.sha` is the *tag object* SHA, not the commit. Always check `.object.type`; if it is `tag`, dereference with `gh api repos/OWNER/REPO/git/tags/<tag-object-sha> --jq '.object.sha'`. Both SHAs recommended above resolve to `commit` directly, so no dereference is needed. `[VERIFIED: observed this session — setup-uv v7 tag object 94527f2e… derefs to commit 37802adc…]`

> **Second gotcha:** `astral-sh/setup-uv` **no longer publishes floating major tags**. `v8`, `v9`, and `v10` all return `404 Not Found` from the git-ref API; the newest floating major is `v7`. So `astral-sh/setup-uv@v10` is *not a valid ref*. Since D-18 mandates SHA pinning this is harmless, but the `# vX.Y.Z` comment must read `# v10.0.1` (a full version), not `# v10`. `[VERIFIED: gh api ref lookups + tag listing]`

### Supporting

| Item | Version | Purpose | When to Use |
|---|---|---|---|
| `libsane-dev` (apt) | 1.2.1-7build4 on Ubuntu 24.04 `main` | SANE headers for building `python-sane` 2.9.2 from sdist | Every job, before `uv sync --locked` `[VERIFIED: packages.ubuntu.com/noble/libsane-dev]` |
| GitHub Actions app | integration_id **15368** | Pin required status checks to the Actions app so a human with write access cannot fake a green status | In the ruleset's `required_status_checks[].integration_id` `[VERIFIED: gh api /apps/github-actions]` |
| Dependabot `github-actions` ecosystem | config schema `version: 2` | Keep the SHA pins from rotting (D-18) | `.github/dependabot.yml` `[CITED: docs.github.com — Keeping your actions up to date with Dependabot]` |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|---|---|---|
| `required_status_checks` rule naming individual job names | The ruleset **`workflows`** rule (require a whole workflow file to pass, by path — sidesteps job-name matching entirely) | **Not available.** The `repo-rules-required-workflows` feature is gated to `ghec: '*'` and `ghes: '>=3.12'` — GitHub Enterprise only. This is a free personal public repo, so `required_status_checks` is the only path. `[VERIFIED: github/docs data/features/repo-rules-required-workflows.yml]` |
| Repository ruleset | Classic branch protection (`PUT /repos/{o}/{r}/branches/master/protection`) | Rulesets are the current API, support `evaluate` mode and history, and are what the GitHub UI now steers to. Classic protection also currently 404s here because the `master` branch does not exist on the remote. Rulesets target *ref patterns*, so they do not require the branch to exist. |
| `astral-sh/setup-uv` + `uv sync` for the interpreter | `actions/setup-python@v5` with `python-version: "3.14"` then `uv sync --locked` | Fallback only. uv's managed CPython 3.14 (python-build-standalone) ships headers and builds C extensions fine, but if `python-sane` fails to compile against it, `actions/setup-python` is the escape hatch. See Pitfall 5. |
| Two jobs each installing `libsane-dev` | One job running all five checks | Rejected by D-05. |

**Installation (the only new Python dependency this phase adds):**

```bash
uv add --dev pytest-timeout        # then, per D-17, in a SEPARATE later commit:
uv add --dev "ty@latest" "pyrefly@latest"
```

---

## Package Legitimacy Audit

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---|---|---|---|---|---|---|
| `pytest-timeout` | PyPI | 2.4.0 published 2025-05-05; 24 releases | very high (pytest-dev ecosystem) | `github.com/pytest-dev/pytest-timeout` | `[OK]` | Approved |
| `ty` | PyPI | 0.0.80 published 2026-09-09; 121 releases | high | `github.com/astral-sh/ty/` | `[OK]` | Approved (already a project dep) |
| `pyrefly` | PyPI | 1.2.0 published 2026-08-01; 143 releases | high | not set in PyPI metadata (`facebook/pyrefly` upstream) | `[OK]` | Approved (already a project dep) |

**Packages removed due to slopcheck `[SLOP]` verdict:** none
**Packages flagged as suspicious `[SUS]`:** none

`slopcheck install pytest-timeout ty pyrefly` → `scanned 3 packages / 3 OK`. Ecosystem-correct registry verified (PyPI, not npm). `[VERIFIED: slopcheck + PyPI JSON API]`

> **Housekeeping note:** running `slopcheck install` actually performed a `pip install` into the *pyenv global* site-packages (`~/.pyenv/versions/3.14.2`). The project's `.venv` was verified unaffected afterwards (`ty 0.0.24`, `pyrefly 0.57.1`, `pytest_timeout` still absent). No action needed, but do not be surprised if `pip list` outside the venv shows these.

---

## Architecture Patterns

### System Architecture Diagram

```
   ┌──────────────────────────┐        ┌──────────────────────────┐
   │ push to refs/heads/master│        │  pull_request (any base) │
   └────────────┬─────────────┘        └────────────┬─────────────┘
                │                                   │
                └───────────────┬───────────────────┘
                                ▼
                    ┌───────────────────────┐
                    │ .github/workflows/    │
                    │      ci.yml           │
                    │ permissions: read     │
                    │ concurrency: cancel   │
                    └───────────┬───────────┘
                                │  fan out (parallel)
                ┌───────────────┴────────────────┐
                ▼                                ▼
   ┌────────────────────────┐      ┌────────────────────────┐
   │ job: lint              │      │ job: test              │
   │  checkout (SHA-pinned) │      │  checkout (SHA-pinned) │
   │  apt-get update        │      │  apt-get update        │
   │  apt install libsane-  │      │  apt install libsane-  │
   │    dev  ── C headers   │      │    dev  ── C headers   │
   │  setup-uv (cache auto) │      │  setup-uv (cache auto) │
   │  uv sync --locked      │      │  uv sync --locked      │
   │    └─ installs [dep-   │      │    └─ same env         │
   │       groups].dev      │      │                        │
   │  ruff check            │      │  pytest -m             │
   │  ruff format --check   │      │    "not browser"       │
   │  ty check              │      │    (pytest-timeout=60  │
   │  pyrefly check         │      │     from pyproject)    │
   └───────────┬────────────┘      └───────────┬────────────┘
               │                                │
               ▼  check run "lint"              ▼  check run "test"
         ┌─────────────────────────────────────────────┐
         │ GitHub Checks API  (app: github-actions,     │
         │                     integration_id 15368)    │
         └────────────────────┬────────────────────────┘
                              ▼
         ┌─────────────────────────────────────────────┐
         │ Branch ruleset on refs/heads/master          │
         │  rule: required_status_checks                │
         │    contexts MUST equal the JOB NAMES above   │
         │  → PR merge blocked unless both are green    │
         │  → direct push to master blocked outright    │
         └─────────────────────────────────────────────┘
                              ▲
                              │ created + read back via
                     ┌────────┴─────────┐
                     │  gh api (POST /  │
                     │  GET /rulesets)  │
                     └──────────────────┘

   ┌──────────────────────────────────────────────────┐
   │ .github/dependabot.yml (github-actions, weekly)   │
   │  → opens PRs bumping the pinned SHAs + comments   │
   │  → those PRs are themselves gated by ci.yml       │
   └──────────────────────────────────────────────────┘
```

### Recommended file layout

```
.github/
├── workflows/
│   ├── ci.yml          # NEW — this phase's primary artifact
│   ├── release.yml     # UNTOUCHED (D-06, Phase 31)
│   └── docs.yml        # UNTOUCHED (D-06/D-19, Phase 31)
└── dependabot.yml      # NEW — same commit as ci.yml (D-18)
CONTRIBUTING.md         # NEW — repo root (D-04)
pyproject.toml          # MODIFIED — [tool.pytest.ini_options] + [dependency-groups].dev
uv.lock                 # REGENERATED — twice (pytest-timeout, then the ty/pyrefly bump)
```

### Pattern 1: Name the jobs explicitly, then read the names back

**What:** Give every job an explicit `name:` and treat the ruleset's `context` strings as *derived from observed check-run names*, never from the YAML by inspection.
**When to use:** Always, when a ruleset must require a check.

GitHub's own docs are explicit:

> When defining status checks, the name format depends on the type of check:
> * **Workflow**: The name format is `<job name>`.
> * **Reusable workflow**: The name format is `<job name> / <reusable job name>`.
> * **Other checks**: The name format is `<check name>`.
>
> Required status checks do not take workflow, matrix, or event trigger types into account.

`[CITED: github/docs — content/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/troubleshooting-rules.md]`

If a job omits `name:`, GitHub displays the **job id** (the YAML key) instead. Both are "the job name" as far as the ruleset is concerned, which is exactly the ambiguity that makes silent misconfiguration easy. Setting `name:` explicitly removes it.

**The definitive read-back** (run after the first green run, before creating the ruleset):

```bash
SHA=$(git rev-parse HEAD)
gh api "repos/kdknigga/saneless/commits/$SHA/check-runs" \
  --jq '.check_runs[] | "\(.name)\tapp=\(.app.slug)\tid=\(.app.id)\tconclusion=\(.conclusion)"'
```

Whatever appears in the `.name` column is verbatim what goes into `context`. `app.id` should be `15368`.

### Pattern 2: Ruleset apply-then-prove

**What:** `POST` the ruleset, capture its id, `GET` it back, and assert on the read-back rather than on the POST's 201.
**When to use:** D-03's verification step.

Idempotency matters because the phase may need to re-run: there is **no `PATCH`** for rulesets, only `PUT /repos/{owner}/{repo}/rulesets/{ruleset_id}` (full replace). So the pattern is: list → if a ruleset with the chosen name exists, `PUT` it; otherwise `POST`. `[VERIFIED: GitHub OpenAPI description — /repos/{owner}/{repo}/rulesets supports get,post; /rulesets/{ruleset_id} supports get,put,delete]`

### Anti-Patterns to Avoid

- **Guessing the context string from the YAML.** A ruleset naming a check that never reports produces a PR stuck at "Expected — Waiting for status to be reported" forever. Worse for this phase: a ruleset naming a *nonexistent* check looks configured and blocks nothing useful while blocking everything else.
- **Requiring only one of the two jobs.** D-05 creates two check runs. A ruleset listing only `lint` means a red `test` merges cleanly. Both must be listed.
- **Creating the ruleset before the first green run.** You will not know the real check names, and `master` may not even exist remotely.
- **`cancel-in-progress: true` unconditionally on `master`.** Cancelling a `master` push run can leave the branch without a green status. Prefer gating cancellation to PRs (see Code Examples).
- **`apt-get install` without `apt-get update`.** GitHub runner images ship stale apt indexes; installs intermittently 404 on the package fetch.
- **Copying `branches: [main]` from the review snippet or from the existing `docs.yml`.** `docs.yml` currently triggers on `main`, which does not exist — a pre-existing bug that means `docs.yml` never runs. It is Phase 31's to fix (D-06/D-19); do not propagate the mistake into `ci.yml`.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---|---|---|---|
| Killing a hung test | A `signal.alarm`/watchdog-thread fixture in `conftest.py` | `pytest-timeout` 2.4.0 | Handles per-test scoping, `signal` vs `thread` methods, fixture-setup timeouts, and `@pytest.mark.timeout` overrides. Verified working here. |
| Enforcing merge blocking | A workflow step that inspects other jobs and posts a commit status | The ruleset `required_status_checks` rule | Only server-side enforcement is unbypassable. A self-reported status can be written by anyone with write access. |
| Keeping SHA pins current | A scheduled workflow that greps and rewrites the pins | Dependabot `github-actions` ecosystem | Dependabot understands the `# vX.Y.Z` comment convention and updates the SHA *and* the comment together. |
| Ensuring CI and local resolve identically | Pinning exact versions in CI install commands | `uv sync --locked` | The lockfile is already the contract; `--locked` makes a stale lock a hard error instead of a silent re-resolve. |
| Selecting non-browser tests | A CI-only `--ignore=tests/test_browser.py` | The already-registered `browser` marker + `-m "not browser"` | Marker-based selection survives file moves and is what the suite already declares. |

**Key insight:** every piece of this phase that looks like it wants a script is actually a declarative setting somewhere — pytest config, ruleset JSON, Dependabot YAML. The only genuinely imperative work is the one-time `gh api` apply and its read-back.

---

## Common Pitfalls

### Pitfall 1: The GitHub remote is empty — nothing has ever been pushed (**BLOCKING**)

**What goes wrong:** The plan lands `ci.yml`, applies a ruleset, and none of it does anything, because there is no code on GitHub.

**Evidence gathered this session** `[VERIFIED: gh api + git]`:

| Probe | Result |
|---|---|
| `git ls-remote origin` | exit 0, **zero refs** |
| `gh api repos/kdknigga/saneless --jq '{size,pushed_at}'` | `{"size":0,"pushed_at":"2026-03-20T19:52:10Z"}` (== `created_at`) |
| `gh api repos/kdknigga/saneless/commits` | `409 — Git Repository is empty.` |
| `gh api repos/kdknigga/saneless/branches` | `[]` |
| `gh api repos/kdknigga/saneless/rulesets` | `[]` |
| `gh api repos/kdknigga/saneless/branches/master/protection` | `404 — Branch not found` |
| `git rev-list --count master` | **1** ("Initial commit", `a87b3dd`) |
| `git rev-list --count autodev` | **334** (`951c2d0 docs(state): record phase 20 context session`) |
| `git rev-list --left-right --count master...autodev` | `0  333` — master is 333 behind autodev |
| `git for-each-ref … %(upstream:short)` | **no branch has an upstream** |

**Why it happens:** CONTEXT.md's D-01 was derived from *local* branches ("`origin/HEAD` is not set locally") — a correct observation that happens to also be what an empty remote looks like. The remote's `default_branch: "master"` field is just the name the *first* push will adopt; it does not imply the branch exists.

**Consequences for the phase as written:**
1. Success criterion 2 ("a clean push produces a green run … visible on the pull request") cannot occur until a push happens.
2. D-03's ruleset would be created against check names that have never been reported.
3. A naive `git push origin master` publishes the 1-commit "Initial commit" tree — CI would run on a tree with no `ci.yml`, no `pyproject.toml` changes, and nothing to check.
4. `required_status_checks` **blocks direct pushes** once active (see Pitfall 2), so the ruleset must be the *last* step, after the repository is published.

**How to avoid:** Treat "publish the repository" as an explicit, user-decided task sequenced before everything else. See Open Question 1 — the branch-reconciliation choice (fast-forward `master` to `autodev`? force-push? make `autodev` the default?) is a user decision, not a planner one.

**Warning signs:** any plan task that assumes `origin/master` exists, or that references a PR number, without a preceding publish step.

---

### Pitfall 2: `required_status_checks` blocks direct pushes, not just merges

**What goes wrong:** After the ruleset goes active, `git push origin master` is rejected — including by the author, who is the repo admin.

**Why it happens:** GitHub's own schema description for the rule is unambiguous:

> "Choose which status checks must pass before the ref is updated. **When enabled, commits must first be pushed to another ref where the checks pass.**"

`[VERIFIED: GitHub OpenAPI description, rules[].oneOf title "required_status_checks"]`

and the prose docs say the rule ensures checks pass "before collaborators can **make changes to** a branch or tag targeted by your ruleset." `[CITED: docs.github.com — Available rules for rulesets § Require status checks to pass before merging]`

Repo admins are **not** automatically exempt from rulesets. Exemption requires an explicit `bypass_actors` entry (`actor_type: "RepositoryRole"`, `bypass_mode: "always" | "pull_request" | "exempt"`). `[VERIFIED: OpenAPI bypass_actors schema]`

**How to avoid:** Sequence the ruleset last. Decide deliberately whether to add a `bypass_actors` entry for the repository-admin role — omitting it is the stronger gate and matches the spirit of D-03/D-04 ("`--no-verify` no longer bypasses the gate"), but it means all future `master` changes must go through a PR. Note this consequence explicitly in `CONTRIBUTING.md`.

**Warning signs:** `push declined due to repository rule violations` after the ruleset lands; a plan that still contains a `git push origin master` task after the ruleset task.

---

### Pitfall 3: `do_not_enforce_on_create` and the not-yet-existing branch

**What goes wrong:** The ruleset targets `refs/heads/master`, which does not exist on the remote yet; creating the branch is itself a ref update that a status-check rule can prohibit.

**How to avoid:** Set `parameters.do_not_enforce_on_create: true` — "Allow repositories and branches to be created if a check would otherwise prohibit it." `[VERIFIED: OpenAPI schema]` Combined with Pitfall 1's fix (publish first), this is belt-and-braces, but costs nothing.

---

### Pitfall 4: `strict_required_status_checks_policy` is **required**, not optional

**What goes wrong:** A `POST` that omits it returns a 422, and it is easy to omit because in the web UI it looks like a checkbox with a default.

**Why it happens:** The rule's `parameters` object declares `required: ["required_status_checks", "strict_required_status_checks_policy"]`. `[VERIFIED: OpenAPI schema]`

**How to avoid:** Always send it. Recommend **`false`** for this repo: `true` ("require branches to be up to date before merging") forces a rebase-and-rerun cycle every time `master` moves, which is friction with no safety benefit for a single-maintainer project. Flag it to the user if they want the stricter behaviour.

---

### Pitfall 5: `python-sane` must compile, on a uv-managed interpreter

**What goes wrong:** `uv sync --locked` fails building `python-sane` 2.9.2 (sdist-only, per `uv.lock`) because `sane/sane.h` is missing, or — less likely — because the interpreter has no dev headers.

**Why it happens:** The runner has neither the SANE headers nor CPython 3.14 preinstalled. `requires-python = ">=3.14"`, so uv will download a managed python-build-standalone CPython 3.14.

**How to avoid:**
- `sudo apt-get update && sudo apt-get install -y --no-install-recommends libsane-dev` **before** `uv sync --locked`. Confirmed present on Ubuntu 24.04 (`ubuntu-latest`) in the **`main`** component at 1.2.1-7build4, amd64 included — no `universe` enablement or PPA needed. `[VERIFIED: packages.ubuntu.com/noble/libsane-dev]`
- The `apt-get update` is not optional: runner images do not refresh apt indexes on boot and installs 404 intermittently without it.
- python-build-standalone distributions ship `Include/` headers and build C extensions routinely, so the managed interpreter should be fine. `[ASSUMED — not exercised on a runner this session]`. If it is not, the fallback is `actions/setup-python@v5` with `python-version: "3.14"` before `setup-uv`.

**Warning signs:** `fatal error: sane/sane.h: No such file or directory`, or `Python.h: No such file or directory`, in the `uv sync --locked` step.

---

### Pitfall 6: Assuming `uv sync --locked` skips dev dependencies

**What goes wrong:** Someone "helpfully" adds `--dev` or, worse, the job silently lacks `ty`/`pyrefly`/`pytest`.

**Resolution:** uv **special-cases the `dev` group and syncs it by default**. From uv's docs: "uv special-cases the `dev` group, which is synced by default and can be toggled using dedicated flags like `--dev`, `--only-dev`, and `--no-dev`." And `--locked` "prevents uv from updating the lockfile, raising an error if it is out of sync with project metadata." `[CITED: astral-sh/uv docs — concepts/projects/dependencies.md, concepts/projects/sync.md, via Context7 /astral-sh/uv]`

So a bare `uv sync --locked` installs everything in `[dependency-groups].dev`: `ruff`, `ty`, `pyrefly`, `pytest`, `pytest-timeout` (once added), plus `playwright`, `pytest-playwright`, `mkdocs-material`, `prek`, `httpx`. No flags needed for either job.

**Caveat to watch for:** the CI environment must not set `UV_NO_DEV=1` (uv's docs show that pattern for Docker images). It is not set here.

---

### Pitfall 7: `filterwarnings = ["error"]` turning a new plugin red — **cleared empirically**

**What was feared:** `pytest-timeout` emitting a `DeprecationWarning` at import/collection that `filterwarnings = ["error"]` escalates into a failure.

**What actually happens** `[VERIFIED: run this session]`:

```
$ uv run --with pytest-timeout pytest -m "not browser" -o timeout=60 -o timeout_method=signal -q
332 passed, 8 deselected in 26.78s
```

Identical count and runtime to the CONTEXT.md baseline (332/8/26.8s). No warnings summary, no errors. `pytest-timeout` 2.4.0 is clean against `pytest` 9.0.2 on Python 3.14.2 with this project's exact pytest config.

---

### Pitfall 8: The `ty` bump breaks precisely the repo's `# type: ignore[...]` comments

**What goes wrong:** D-16's bump reddens CI, and the cause is non-obvious because the suppressions *look* correct.

**Empirical result of the dry-run** `[VERIFIED: uvx --from 'ty==0.0.80' ty check --python .venv]` — **3 diagnostics**:

| File:line | Rule | Code |
|---|---|---|
| `src/saneless/config.py:168` | `unknown-argument` | `return Settings(_toml_file=toml_file)  # type: ignore[call-arg]` |
| `tests/test_scanner.py:706` | `invalid-assignment` | `mock_dev.get_options = lambda: [  # type: ignore[assignment]` |
| `tests/test_scanner.py:723` | `invalid-assignment` | `mock_dev.get_options = lambda: [  # type: ignore[assignment]` |

Those are the repo's **only** three `# type: ignore` comments (`grep -rn "type: ignore" src tests` → 3 hits in 2 files). The correlation is exact.

**Root cause, isolated with a minimal repro** `[VERIFIED: 5-line file checked under both ty versions]`:

| Suppression form | ty 0.0.24 | ty 0.0.80 |
|---|---|---|
| `# type: ignore` (bare) | suppresses | **suppresses** |
| `# type: ignore[assignment]` (mypy code) | suppresses | **does NOT suppress** |
| `# type: ignore[invalid-assignment]` (ty rule name in the mypy namespace) | suppresses | **does NOT suppress** |
| `# ty: ignore[invalid-assignment]` | suppresses | **suppresses** |
| no comment | error | error |

ty 0.0.80 no longer honours `# type: ignore` when it carries **any** bracketed code; only the bare form or ty's own `# ty: ignore[rule]` namespace applies.

**How to avoid / fix:** CLAUDE.md forbids suppressing type-checker findings, so the correct fix is a real one at each of the three sites (a typed wrapper or `cast()` for the `BaseSettings` dynamic kwarg; a properly-typed callable or `object.__setattr__` for the two `get_options` reassignments — both patterns already exist in this codebase per STATE.md Phase 09 decisions). Converting to `# ty: ignore[...]` would work mechanically but trades one suppression for another and should only be proposed to the user explicitly.

---

### Pitfall 9: The `pyrefly` 0.x → 1.x crossing — **de-risked, it is clean**

**What was feared:** D-16 predicted fallout from crossing 1.0.

**What actually happens** `[VERIFIED: uvx --from 'pyrefly==1.2.0' pyrefly check --python-interpreter-path .venv/bin/python`]:

```
 INFO Checking project configured at `/home/kris/git/saneless/pyproject.toml`
 INFO 0 errors
```

Zero errors. The existing `[tool.pyrefly]` block in `pyproject.toml` is picked up unchanged.

**One CLI migration note found:** `--python-interpreter` was renamed to `--python-interpreter-path` in pyrefly 1.x. This project invokes `uv run pyrefly check` with no flags (both in `.pre-commit-config.yaml` and in the proposed `ci.yml`), so it is not affected — but it is a real 0.x→1.x breaking change worth knowing if any tooling ever passes that flag.

---

### Pitfall 10: Assuming the browser exclusion needs a Playwright install

**What was feared:** `-m "not browser"` still collects `tests/test_browser.py`, and something in the import path or the `pytest-playwright` plugin needs a downloaded Chromium.

**What actually happens** `[VERIFIED: re-ran the suite with PLAYWRIGHT_BROWSERS_PATH pointed at an empty directory]`:

```
$ PLAYWRIGHT_BROWSERS_PATH=<empty-dir> uv run --with pytest-timeout pytest -m "not browser" -o timeout=60 -q
332 passed, 8 deselected in 26.80s
```

So **no `playwright install` step is needed in `ci.yml`**, even though `uv sync --locked` installs the `playwright` and `pytest-playwright` wheels. D-07 holds.

*(Minor note: `tests/test_browser.py` carries 3 `@pytest.mark.browser` decorators but 8 tests are deselected — parametrization. Harmless; just do not be surprised that the numbers differ.)*

---

### Pitfall 11: `concurrency` cancelling a `master` run out from under the ruleset

**What goes wrong:** Two commits land on `master` in quick succession; `cancel-in-progress: true` cancels the first run, leaving that commit with a cancelled (non-success) check.

**How to avoid:** Gate cancellation to pull requests:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.head_ref || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

Using `github.head_ref || github.ref` also keeps a PR's runs in their own group rather than the synthetic `refs/pull/N/merge` ref. `[CITED: docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax]`

---

### Pitfall 12: Token scope for creating the ruleset

**What goes wrong:** `POST /rulesets` returns 403 mid-plan.

**What is known:** `gh` is authenticated as `kdknigga` with an OAuth token (`gho_…`) carrying scopes `gist, read:org, repo`, and `gh api repos/kdknigga/saneless --jq .permissions` reports `{"admin":true,…}`. Admin-only *read* endpoints succeed with this token — `GET /actions/permissions` → `{"enabled":true,"allowed_actions":"all","sha_pinning_required":false}`, `GET /actions/permissions/workflow` → `{"default_workflow_permissions":"read",…}`, `GET /hooks` → `[]`, `GET /rulesets` → `[]` (not 403). `[VERIFIED: this session]`

That is strong evidence the token can also write, but the write path was **not** exercised (research does not mutate repo settings). Confidence: MEDIUM-HIGH.

**Fallback if `POST` 403s:** re-authenticate with a fine-grained PAT carrying **Administration: write** on the repo (`gh auth login --with-token`), or apply the ruleset once via Settings → Rules and then use `gh api` only for read-back — the read-back is the part D-03 actually depends on.

---

### Pitfall 13: Dependabot PRs and the ruleset

**What goes wrong:** Dependabot opens SHA-bump PRs and they cannot merge because Dependabot cannot bypass the ruleset.

**Why it happens/does not:** Dependabot PRs are ordinary PRs; `ci.yml` triggers on `pull_request` and produces the two required checks, so they gate and merge normally. No action needed — but note that Dependabot PRs will need a human merge click, which is the intended behaviour, not a bug.

---

## Code Examples

### `.github/workflows/ci.yml`

```yaml
# SHAs resolved 2026-09-09 via gh api; comments must carry the full version.
name: CI

on:
  push:
    branches: [master]
  pull_request:

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.head_ref || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  lint:
    name: lint
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Install SANE development headers
        run: |
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends libsane-dev
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
      - run: uv sync --locked
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run ty check
      - run: uv run pyrefly check

  test:
    name: test
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Install SANE development headers
        run: |
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends libsane-dev
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
      - run: uv sync --locked
      - run: uv run pytest -m "not browser"
```

Notes:
- The job names `lint` and `test` become the required-check contexts. **Confirm by read-back before writing them into the ruleset** (Pattern 1).
- `setup-uv` needs **no cache configuration**: `enable-cache` defaults to `"auto"` (enabled on GitHub-hosted runners except for release, tag push, `pull_request_target` and `workflow_run` events — our `push`/`pull_request` triggers qualify), and the default `cache-dependency-glob` already includes `**/pyproject.toml` and `**/uv.lock`. `[VERIFIED: setup-uv v10.0.1 README]`
- `setup-uv`'s README itself uses the `uses: astral-sh/setup-uv@<sha> # v10.0.0` convention, so D-18's style matches upstream.
- No `python-version` input is passed; uv resolves `requires-python = ">=3.14"` from `pyproject.toml`/`uv.lock` and installs a managed CPython 3.14. If C-extension builds prove troublesome, add `python-version: "3.14"` to `setup-uv` or fall back to `actions/setup-python` (Pitfall 5).

### `.github/dependabot.yml`

```yaml
# Set update schedule for GitHub Actions
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
```

`directory: "/"` is mandatory and means "`.github/workflows`". `package-ecosystem`, `directory` and `schedule.interval` are the three required keys. `[CITED: github/docs — content/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/auto-update-actions.md]`

> Consequence the planner should note: Dependabot will also start opening PRs for `release.yml` and `docs.yml` actions, which Phase 31 owns. That is fine (a PR is not a change) but is worth a sentence in `CONTRIBUTING.md` so it is not mistaken for scope creep.

### `pyproject.toml` — pytest-timeout (D-11 … D-15)

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "browser: Playwright browser tests (requires chromium)",
]
addopts = ["-ra", "--strict-markers", "--strict-config"]
strict_markers = true
strict_config = true
xfail_strict = true
filterwarnings = ["error"]
timeout = 60                  # D-12 — ~20x the 3.0s slowest test
timeout_method = "signal"     # D-13 — fail one test with a traceback; suite continues

[dependency-groups]
dev = [
    "httpx>=0.28.1",
    "playwright>=1.58.0",
    "pytest-playwright>=0.7.0",
    "pytest-timeout>=2.4.0",   # D-15 — plain requirement, not optional
    "prek>=0.3.5",
    "pyrefly>=0.55.0",
    "pytest>=9.0.2",
    "ruff>=0.15.5",
    "ty>=0.0.21",
    "mkdocs-material>=9.7.6",
]
```

> `--strict-config` is already on, which means an unknown ini key is a hard error. `timeout` and `timeout_method` are only known keys once `pytest-timeout` is installed — so **the `[dependency-groups]` addition and the `[tool.pytest.ini_options]` addition must land in the same commit**, or the intermediate state fails on every pytest invocation including `prek`.

### Creating the ruleset (D-03)

```bash
OWNER_REPO=kdknigga/saneless

# 1. Read the ACTUAL check names off the green run's head commit.
SHA=$(git rev-parse HEAD)
gh api "repos/$OWNER_REPO/commits/$SHA/check-runs" \
  --jq '.check_runs[] | "\(.name)\tapp=\(.app.slug)(\(.app.id))\t\(.conclusion)"'
# expect e.g.:  lint  app=github-actions(15368)  success
#               test  app=github-actions(15368)  success

# 2. Create (no ruleset exists today — GET /rulesets returns [])
gh api --method POST "repos/$OWNER_REPO/rulesets" --input - <<'JSON'
{
  "name": "master gate",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {
    "ref_name": { "include": ["refs/heads/master"], "exclude": [] }
  },
  "rules": [
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": true,
        "required_status_checks": [
          { "context": "lint", "integration_id": 15368 },
          { "context": "test", "integration_id": 15368 }
        ]
      }
    },
    { "type": "deletion" },
    { "type": "non_fast_forward" }
  ]
}
JSON

# 3. Read back — this is the proof D-03 asks for.
RID=$(gh api "repos/$OWNER_REPO/rulesets" --jq '.[] | select(.name=="master gate") | .id')
gh api "repos/$OWNER_REPO/rulesets/$RID" \
  --jq '{name, target, enforcement,
         refs: .conditions.ref_name.include,
         checks: (.rules[] | select(.type=="required_status_checks")
                  | .parameters.required_status_checks
                  | map(.context + "@" + (.integration_id|tostring))),
         other: [.rules[].type]}'
```

**Re-apply / update** (there is no `PATCH`; `PUT` is a full replace):

```bash
gh api --method PUT "repos/$OWNER_REPO/rulesets/$RID" --input ruleset.json
```

**Schema facts behind the above** `[VERIFIED: GitHub OpenAPI dereferenced description, `POST /repos/{owner}/{repo}/rulesets`]`:
- Top-level `required`: `["name", "enforcement"]`; `target` ∈ `branch|tag|push`; `enforcement` ∈ `disabled|active|evaluate`.
- `conditions.ref_name.include` accepts explicit refs, fnmatch patterns, `~DEFAULT_BRANCH`, or `~ALL`. `refs/heads/master` is used above rather than `~DEFAULT_BRANCH` because it is explicit and survives a default-branch change (choose deliberately — `~DEFAULT_BRANCH` is the alternative).
- `required_status_checks.parameters` `required`: `["required_status_checks", "strict_required_status_checks_policy"]`.
- Each `required_status_checks[]` item requires `context`; `integration_id` is optional but pins the status to the GitHub Actions app (id **15368**).
- `deletion` and `non_fast_forward` take no parameters. Other available rule types: `creation`, `update`, `required_linear_history`, `merge_queue`, `required_deployments`, `required_signatures`, `pull_request`, `commit_message_pattern`, `commit_author_email_pattern`, `committer_email_pattern`, `branch_name_pattern`, `tag_name_pattern`, `workflows` (GHEC/GHES only), `code_scanning`, `copilot_code_review`, `license_compliance_scanning`, `file_path_restriction`, `max_file_path_length`, `file_extension_restriction`, `max_file_size`.
- `bypass_actors[]` requires `actor_type` ∈ `Integration|OrganizationAdmin|RepositoryRole|Team|DeployKey|User`, with `bypass_mode` ∈ `always|pull_request|exempt` (default `always`). `OrganizationAdmin` is not applicable to personal repositories.

> **Optional, worth surfacing to the user:** adding `{ "type": "pull_request", "parameters": { "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": false, "require_code_owner_review": false, "require_last_push_approval": false, "required_review_thread_resolution": false } }` would additionally *require* a PR (rather than merely blocking direct pushes as a side effect of the status-check rule). All five parameters are required if the rule is used. Not decided in CONTEXT.md — flag rather than assume.

### Verifying pytest-timeout actually fires (TEST-07 evidence)

Reproduced this session against this project's environment:

```
$ pytest test_hang.py -o timeout=3 -o timeout_method=signal
timeout: 3.0s
timeout method: signal
timeout func_only: False
collected 2 items
test_hang.py F.                                                          [100%]
=================================== FAILURES ===================================
__________________________________ test_hangs __________________________________
    def test_hangs():
>       time.sleep(30)
E       Failed: Timeout (>3.0s) from pytest-timeout.
=========================== short test summary info ============================
FAILED test_hang.py::test_hangs - Failed: Timeout (>3.0s) from pytest-timeout.
========================= 1 failed, 1 passed in 3.03s ==========================
```

Exactly the D-13 behaviour: one red test with a traceback, the following test still runs, the process exits normally.

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|---|---|---|---|
| Classic branch protection (`/branches/{b}/protection`) | Repository **rulesets** (`/rulesets`) | Rulesets GA'd for public repos on Free; now the default in the GitHub UI | D-03's `gh api` path should use `/rulesets`, not `/branches/master/protection` (which additionally 404s here) |
| `astral-sh/setup-uv@v7` (as in `release.yml`) | `astral-sh/setup-uv@<sha> # v10.0.1` | v10.0.1 published 2026-08-14 | Three major versions behind. `release.yml` stays on v7 (D-06); `ci.yml` starts current |
| `actions/checkout@v5` (as in `release.yml`) | `actions/checkout@<sha> # v7.0.1` | v7.0.1 published 2026-07-20 | Same — `ci.yml` starts current |
| Floating major tags for `setup-uv` (`@v7`) | Full-version tags only (`@v10.0.1`); `v8`/`v9`/`v10` do not exist | after v7.6 | `@v10` is an invalid ref. SHA pinning avoids the issue entirely |
| `ty` honouring `# type: ignore[<mypy-code>]` | Only bare `# type: ignore` or `# ty: ignore[<ty-rule>]` | between ty 0.0.24 and 0.0.80 | The entire D-16 ty fallout (3 diagnostics) |
| `pyrefly --python-interpreter` | `pyrefly --python-interpreter-path` | pyrefly 1.x | Not used by this project; noted for completeness |

**Deprecated/outdated:**
- `.github/workflows/docs.yml` triggers on `branches: [main]` — a branch that does not and will not exist. It has never run and never will until Phase 31 fixes it. Do not touch it here (D-06), but do not let it serve as a pattern.
- `release.yml`'s `uv sync` (no `--locked`), missing `libsane-dev`, missing both type checkers, and unfiltered `pytest` — Phase 31 (M-26).

---

## Project Constraints (from CLAUDE.md)

| Directive | Implication for this phase |
|---|---|
| Python 3.14; `uv` for package/env management (not pip/poetry/conda) | `ci.yml` uses `uv sync --locked` + `uv run …`; never `pip install` |
| `prek` (not `pre-commit`) for hooks | `CONTRIBUTING.md` must say `uv run prek run` (D-04) |
| Ruff lint + Ruff format + `ty` + `pyrefly` must all pass with **zero** errors/warnings | All four are separate `ci.yml` steps so failures are individually attributable |
| **No mypy or pyright** | Nothing added; note that the 3 existing `# type: ignore[call-arg]`/`[assignment]` comments are *mypy* codes, which is why ty 0.0.80 rejects them (Pitfall 8) |
| **Do not suppress with `# type: ignore`, `# noqa`, or by disabling rules** | The D-16 ty fallout must be fixed with real typing changes, not by converting to `# ty: ignore[...]`. Also: this phase is an opportunity to remove all 3 existing suppressions, which currently violate this rule |
| Prefer external packages over reimplementing solved problems; favour libraries in Context7 | `pytest-timeout` over a bespoke watchdog fixture |
| Use Serena / Context7 / Tavily / Playwright MCP during development | Context7 was used for uv docs; Playwright is not needed in `ci.yml` (Pitfall 10) |
| **All browser-based validation must go through Playwright MCP — never "manual-only"** | Not applicable to this phase: `ci.yml` has no browser surface and D-07 defers the browser job to Phase 26. No verification step in this phase should be marked human-only on browser grounds |
| `ty` and `pyrefly` may report different issues for the same code — both must pass clean | Confirmed empirically: at the bumped versions ty finds 3, pyrefly finds 0 |
| Ruff `D` rules require docstrings on all public modules/classes/functions | Relevant if the D-16 fixes introduce helper functions in `src/` |

---

## Runtime State Inventory

*(Rename/refactor/migration category — this phase creates state **outside** the repository tree, so the inventory is included in the "what lives outside git" sense.)*

| Category | Items Found | Action Required |
|---|---|---|
| Stored data | **None** — this phase adds no persistence and touches no database. Verified: no `src/` change except D-16 type fixes | none |
| Live service config | **`kdknigga/saneless` branch ruleset** — lives in GitHub repo settings, **not** in git. Currently `GET /rulesets` → `[]`. Also: `default_workflow_permissions: "read"`, `allowed_actions: "all"`, `sha_pinning_required: false` (repo Actions settings, also not in git) | Ruleset applied via `gh api` and verified by read-back (D-03). Actions settings left as-is |
| OS-registered state | **None** — no scheduled tasks, no services, no daemons involved | none |
| Secrets / env vars | **None new.** `ci.yml` needs no secrets; the default `GITHUB_TOKEN` with `permissions: contents: read` suffices. Repo default workflow permission is already `read` | none |
| Build artifacts / installed packages | **`.venv` must be re-synced** after `uv add --dev pytest-timeout` and again after the `ty`/`pyrefly` bump, or local `prek` runs the old versions. Also: `slopcheck install` during research installed `ty 0.0.80` / `pyrefly 1.2.0` / `pytest-timeout 2.4.0` into the **pyenv global** site-packages (project `.venv` verified unaffected) | `uv sync` after each dependency change; no action for the pyenv global install |
| **Remote git state (the critical one)** | `github.com/kdknigga/saneless` has **zero refs** — nothing has ever been pushed. Local `master` = 1 commit; `autodev` = 334 commits; no upstream on any branch | **Publish the repository.** See Open Question 1 |

**The canonical question — after every file in the repo is updated, what runtime systems still have stale/absent state?** Answer: the GitHub remote itself (empty) and the branch ruleset (absent). Both are addressed by explicit plan tasks; neither is visible to a `git status` or a grep.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|---|---|---|---|---|
| `gh` CLI, authenticated | D-03 ruleset apply + read-back | ✓ | `/usr/bin/gh`, account `kdknigga`, scopes `gist, read:org, repo`, repo `admin: true` | Web UI for apply; `gh api` still needed for read-back |
| `uv` | everything | ✓ | 0.10.3 | — |
| Python 3.14 | project | ✓ | 3.14.2 (pyenv-built, `.venv` home `~/.pyenv/versions/3.14.2`) | — |
| `ty` (local) | baseline + bump | ✓ | 0.0.24 in `.venv`; 0.0.80 available on PyPI | — |
| `pyrefly` (local) | baseline + bump | ✓ | 0.57.1 in `.venv`; 1.2.0 available on PyPI | — |
| `pytest-timeout` | TEST-07 | ✗ (not in `.venv`) | 2.4.0 on PyPI, verified working via `uv run --with` | — (`uv add --dev` installs it) |
| `libsane-dev` on `ubuntu-latest` | `python-sane` build in CI | ✓ | 1.2.1-7build4, Ubuntu 24.04 **main** | — |
| Playwright browsers in CI | *not required* | n/a | — | Verified unnecessary (Pitfall 10) |
| `act` | — | ✗ | — | **Explicitly rejected by D-09.** Not researched, not proposed |
| **Remote repo with content** | success criteria 1 & 2 | **✗ EMPTY** | zero refs | **No fallback — see Open Question 1** |

**Missing dependencies with no fallback:**
- A published GitHub repository. Without it there is no Actions run, no check-run names, and no PR. This blocks CI-01's verification entirely.

**Missing dependencies with fallback:**
- `pytest-timeout` — trivially resolved by `uv add --dev pytest-timeout`.

---

## Validation Architecture

### Test Framework

| Property | Value |
|---|---|
| Framework | `pytest` 9.0.2 (9.1.1 available; bumping is Phase 32) |
| Config file | `pyproject.toml` → `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest -m "not browser" -q` |
| Full suite command | `uv run pytest -m "not browser"` (browser tests are Phase 26's) |

Measured this session: **332 passed, 8 deselected, 26.8s** — matches the CONTEXT.md baseline exactly, both with and without `pytest-timeout` loaded, and with and without Playwright browsers present.

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|---|---|---|---|---|
| TEST-07 | `pytest-timeout` is installed and its ini keys are accepted under `--strict-config` | smoke | `uv run pytest -m "not browser" -q` (a bad ini key is a hard error under `--strict-config`) | ✅ existing suite |
| TEST-07 | Config values are actually `timeout=60`, `method=signal` | smoke | `uv run pytest --collect-only -q 2>&1 \| head -1` after adding `-o timeout=…`, or `grep -A2 'timeout' pyproject.toml` | ✅ config assertion |
| TEST-07 | A hung test is killed with a traceback and the suite continues | integration | throwaway file + `uv run pytest <file> -o timeout=3` → expect `1 failed, 1 passed` | ❌ transient, not committed (see note) |
| CI-01 | All five checks pass locally | smoke | `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pyrefly check && uv run pytest -m "not browser"` | ✅ |
| CI-01 | The workflow triggers and completes green on GitHub | e2e (out-of-repo) | `gh run list --workflow=ci.yml --limit 1 --json conclusion,headBranch,event` → `conclusion == "success"` | ❌ requires published repo |
| CI-01 | Both check runs are reported by the GitHub Actions app | e2e (out-of-repo) | `gh api repos/kdknigga/saneless/commits/$SHA/check-runs --jq '[.check_runs[].name]'` | ❌ requires published repo |
| CI-01 | A seeded one-line break turns the run red (D-08.3) | e2e (out-of-repo) | push throwaway branch, open PR, `gh run watch` → `conclusion == "failure"` | ❌ requires published repo |
| CI-01 | The ruleset exists, is `active`, targets `refs/heads/master`, and requires **both** contexts | e2e (out-of-repo) | the `gh api …/rulesets/$RID --jq` read-back in § Code Examples | ❌ requires published repo |
| CI-01 (D-16) | Bumped checkers pass clean | smoke | `uv run ty check && uv run pyrefly check` | ✅ (pyrefly already 0; ty needs the 3 fixes) |

> **Note on the hang test:** committing a permanently-hanging test to the suite would add 60s to every run. The D-08-style approach is right here too — demonstrate it once with a throwaway file and delete it, capturing the output as evidence. Do **not** add a `@pytest.mark.timeout(1)`-plus-`sleep(5)` test to `tests/` as a "regression test"; it is a test of pytest-timeout, not of this project.

### Sampling Rate

- **Per task commit:** `uv run prek run` (already runs ruff, ruff-format, ty, pyrefly) + `uv run pytest -m "not browser" -q`
- **Per wave merge:** the full five-check sequence above
- **Phase gate:** green GitHub Actions run + ruleset read-back before `/gsd-verify-work`

### Wave 0 Gaps

- [ ] None for the Python suite — `tests/` and `pyproject.toml` pytest config already exist and are green.
- [ ] `uv add --dev pytest-timeout` must precede any run that relies on the new ini keys (`--strict-config` makes the ordering load-bearing).
- [ ] The out-of-repo e2e rows above are blocked on Open Question 1, not on missing test files.

---

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---|---|---|
| V2 Authentication | no | No auth surface in a CI workflow |
| V3 Session Management | no | — |
| V4 Access Control | **yes** | `permissions: { contents: read }` at workflow level (least privilege for `GITHUB_TOKEN`); ruleset `bypass_actors: []` (no silent exemptions); repo default already `default_workflow_permissions: "read"` |
| V5 Input Validation | marginal | No untrusted input reaches the workflow. The `pull_request` trigger (not `pull_request_target`) means fork PRs run with a read-only token and no secrets — correct by construction |
| V6 Cryptography | **yes** | SHA-pinning actions is integrity pinning: a mutable tag can be repointed by a compromised maintainer; a commit SHA cannot (D-18) |
| V14 Configuration | **yes** | `uv sync --locked` (supply-chain integrity of Python deps); Dependabot (pin currency); no secrets in `ci.yml` |

### Known Threat Patterns for GitHub Actions + uv

| Pattern | STRIDE | Standard Mitigation |
|---|---|---|
| Mutable action tag repointed to malicious code | Tampering | SHA-pin `actions/checkout` and `astral-sh/setup-uv` (D-18) |
| Frozen SHA pins never receive security fixes | Denial of Service / Tampering | Dependabot `github-actions` ecosystem in the same commit (D-18, PITFALLS.md Pitfall 19) |
| Over-privileged `GITHUB_TOKEN` abused by a compromised dependency | Elevation of Privilege | Workflow-level `permissions: { contents: read }`; no job escalates |
| `pull_request_target` exposing secrets to fork code | Information Disclosure | Use plain `pull_request` (D-02 already does) |
| Lockfile drift letting CI resolve different (possibly malicious) versions than local | Tampering | `uv sync --locked` — a stale lock is a hard failure |
| Human with write access posting a fake green commit status | Spoofing | `integration_id: 15368` on each required check pins the status source to the GitHub Actions app |
| Force-push or deletion of `master` erasing history | Tampering / DoS | `non_fast_forward` and `deletion` rules in the ruleset |
| Slopsquatted package in the dev group | Tampering | `slopcheck` run on all three packages → 3 OK; ecosystem-correct registry (PyPI) verified |

Note: the repo currently has `sha_pinning_required: false` in its Actions settings — GitHub now offers a repo/org-level toggle that *enforces* SHA pinning for all workflows. Enabling it would break `release.yml` and `docs.yml`, which are Phase 31's; mention as a Phase 31 candidate, do not enable here.

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|---|---|---|
| A1 | uv's managed CPython 3.14 (python-build-standalone) ships headers sufficient to compile `python-sane` 2.9.2 on the runner | Pitfall 5, Code Examples | CI red at `uv sync --locked` with a `Python.h` error; mitigated by the documented `actions/setup-python` fallback |
| A2 | The OAuth token's `repo` scope permits `POST /rulesets` (write), inferred from `admin: true` and successful admin-only *reads* — the write path was not exercised | Pitfall 12 | 403 mid-plan; mitigated by the fine-grained-PAT / web-UI fallback |
| A3 | A ruleset can be created targeting `refs/heads/master` before that ref exists on the remote (rulesets are pattern-based) | Code Examples, Pitfall 3 | 422 on create; mitigated by publishing the repo first, which the plan does anyway |
| A4 | Job names `lint` / `test` will appear verbatim as the check-run names | Pattern 1 | The whole gate silently blocks nothing; **fully mitigated** by mandating the `check-runs` read-back before writing contexts |
| A5 | `pytest-timeout` also behaves cleanly against `pytest` 9.1.1, should Phase 32 bump pytest | Standard Stack | Out of scope for this phase; verified only against the locked 9.0.2 |
| A6 | Dependabot will preserve and update the `# vX.Y.Z` trailing comments alongside the SHAs | Don't Hand-Roll | Comments go stale; cosmetic, not functional |

---

## Open Questions (ALL RESOLVED)

> Resolved 2026-09-09 during planning. Each answer is threaded into CONTEXT.md and/or a specific plan task; the original analysis is kept below for traceability.

### 1. The repository has never been pushed — what becomes `master`? **(RESOLVED → D-20..D-25)**

> **RESOLVED:** option (b). Push `autodev`'s work and open a PR into `master` — **which the user merges, never Claude (D-21)**. Additionally: `.planning/` is stripped via `uvx git-filter-repo` (D-22, D-25), and the repo is switched to private before the first push (D-23). Implemented by Plans 20-02 and 20-03.

- **What we know:** `github.com/kdknigga/saneless` is empty (zero refs, `409 Git Repository is empty`). Locally, `master` = 1 commit ("Initial commit"), `autodev` = 334 commits and holds all real work, `development` = 1 commit, plus two `worktree-agent-*` branches. No branch has an upstream. `[VERIFIED]`
- **What's unclear:** the branch-reconciliation strategy. Options include: (a) fast-forward/reset local `master` to `autodev` and push `master` as the single published branch; (b) push `autodev` first, open a PR into `master`, and let the CI gate itself validate the merge (elegant, but requires `master` to exist remotely first and requires `ci.yml` to be on `autodev`); (c) push all branches and keep working on `autodev` with PRs into `master`. `.planning/config.json` has `git.branching_strategy: "none"` and `use_worktrees: true`, which does not settle it.
- **Recommendation:** put this to the user before planning finalises. It changes the task sequence materially, and it interacts with Pitfall 2 (once the ruleset is active, direct pushes to `master` are rejected — including the very push that would publish the work).
- **Hard ordering constraint regardless of choice:** publish → `ci.yml` on a branch → green run → read back check names → create ruleset. The ruleset must be last.

### 2. Should the ruleset also add a `pull_request` rule? **(RESOLVED — no)**

> **RESOLVED:** do not add it; the `required_status_checks` rule already achieves D-03. Plan 20-04 Task 1 explicitly excludes it and surfaces it at the Task 3 checkpoint for the user to revisit.

- **What we know:** `required_status_checks` already blocks direct pushes as a side effect ("commits must first be pushed to another ref where the checks pass"). A separate `pull_request` rule would make the PR requirement explicit and enable review/thread settings. All five of its parameters are required.
- **What's unclear:** CONTEXT.md D-03 says only "a red run blocks merge"; a PR requirement is adjacent but not decided.
- **Recommendation:** default to **not** adding it (the status-check rule already achieves D-03's stated goal with fewer moving parts), and surface it to the user as a one-line question.

### 3. `strict_required_status_checks_policy` — `true` or `false`? **(RESOLVED — false)**

> **RESOLVED:** `false`, per the recommendation below. Set explicitly in Plan 20-04 Task 1's POST body.

- **What we know:** `true` means "topic branch must be up to date with base before merging"; it is the GitHub UI default but must be sent explicitly via the API.
- **Recommendation:** `false` for a single-maintainer repo — `true` forces a rebase-and-rerun every time `master` moves. Cheap to flip later via `PUT`.

### 4. Should `bypass_actors` include the repository-admin role? **(RESOLVED — `[]`, user-confirmed)**

> **RESOLVED:** `[]` by default, but the consequence is disclosed to the user at Plan 20-02's blocking checkpoint and echoed again immediately before the write in Plan 20-04 Task 1. The executor may not decide it unilaterally in either direction.

- **What we know:** repo admins are **not** exempt by default. With `bypass_actors: []`, the maintainer must use PRs for all future `master` changes.
- **Recommendation:** `[]` (no bypass) — it matches D-04's "`--no-verify` no longer bypasses the gate" spirit. But it is a real workflow change and belongs in `CONTRIBUTING.md`, and the user should be told rather than surprised.

### 5. Do the three `# type: ignore[...]` sites get real fixes or `# ty: ignore[...]`? **(RESOLVED — real fixes)**

> **RESOLVED:** real fixes, per CLAUDE.md's no-suppressions rule (D-16). PATTERNS.md found an in-repo precedent for each site, so no suppression swap is needed. Implemented by Plan 20-05.

- **What we know:** CLAUDE.md forbids suppressions; the three existing ones already violate that rule; the exact sites and ty rule names are known (Pitfall 8). pyrefly is clean at 1.2.0, so there is no cross-checker conflict to navigate.
- **Recommendation:** real fixes, and treat removing the three suppressions as a small bonus win of D-16. If a real fix proves infeasible at `src/saneless/config.py:168` (pydantic-settings' dynamic `__init__` kwargs are genuinely hard to type), raise it rather than silently converting the syntax.

---

## Sources

### Primary (HIGH confidence)

- **Direct measurement on this machine, 2026-09-09** — suite runs with/without `pytest-timeout` and with/without Playwright browsers; `ty` 0.0.24 vs 0.0.80 suppression repro; `pyrefly` 1.2.0 dry-run; `git` topology; `gh api` probes against `kdknigga/saneless`.
- **GitHub REST OpenAPI description** — `github/rest-api-description` `descriptions/api.github.com/dereferenced/api.github.com.deref.json` — full ruleset POST schema, all rule type enums, `bypass_actors` schema, required parameters.
- **github/docs source** — `content/repositories/.../managing-rulesets/troubleshooting-rules.md` (check-name format), `.../available-rules-for-rulesets.md` (status-check rule semantics, strict vs loose), `content/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/auto-update-actions.md` (dependabot.yml), `data/features/repo-rules-required-workflows.yml` (GHEC/GHES gating).
- **Context7 `/astral-sh/uv`** — `dev` group synced by default; `--locked` semantics; `default-groups`; `UV_NO_DEV`.
- **`astral-sh/setup-uv` v10.0.1 README** (raw.githubusercontent.com) — all inputs, `enable-cache: "auto"` semantics, default `cache-dependency-glob`, SHA-pin-with-comment convention.
- **GitHub API tag/release lookups** — `actions/checkout` v7.0.1, `astral-sh/setup-uv` v10.0.1, absence of floating `v8`/`v9`/`v10`, annotated-vs-lightweight tag types.
- **PyPI JSON API** — `ty` 0.0.80, `pyrefly` 1.2.0, `pytest-timeout` 2.4.0, `ruff` 0.16.6, `pytest` 9.1.1 with publish dates.
- **`slopcheck install pytest-timeout ty pyrefly`** — 3/3 OK.

### Secondary (MEDIUM confidence)

- `packages.ubuntu.com/noble/libsane-dev` (via WebFetch) — `libsane-dev` 1.2.1-7build4 in Ubuntu 24.04 `main`, amd64 present.
- `actions/runner-images` README — `ubuntu-latest` → Ubuntu 24.04.
- `docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax` (URL confirmed 200) — `concurrency`, `permissions`, `timeout-minutes` syntax.

### Tertiary (LOW confidence — corroborating only, not relied upon)

- GitHub community discussions #130684 and #170628 on required-status-check naming — consistent with the authoritative docs finding above, cited only as corroboration.

---

## Metadata

**Confidence breakdown:**

- **Standard stack:** HIGH — every version and SHA resolved live via the GitHub and PyPI APIs this session, with the resolution commands recorded for re-verification.
- **Ruleset API and check-name semantics:** HIGH — schema from GitHub's own OpenAPI description, naming rule from GitHub's own docs source. The one residual gap (write-scope authorization) is MEDIUM and has a documented fallback.
- **`pytest-timeout` / TEST-07:** HIGH — run end-to-end against this project, including a real timeout firing.
- **D-16 bump fallout:** HIGH — both checkers dry-run against the real tree; the ty behaviour change isolated with a minimal repro.
- **CI runner setup path (`libsane-dev`, uv-managed 3.14, C-extension build):** MEDIUM — `libsane-dev` availability verified; the actual runner build was not exercised (that is precisely what D-08's green run is for), and A1 records the residual assumption.
- **Repository publication state:** HIGH — verified four independent ways.
- **Pitfalls:** HIGH for the empirically cleared ones (7, 8, 9, 10), MEDIUM for those relying on documented behaviour not exercised here (2, 3, 12).

**Research date:** 2026-09-09
**Valid until:** 2026-10-09 for the ruleset/docs findings (stable APIs); **2026-09-16** for the action SHAs and `ty`/`pyrefly` versions — `ty` 0.0.80 shipped the same day as this research and Astral releases frequently. Re-run the resolution commands in § Standard Stack at execution time.
