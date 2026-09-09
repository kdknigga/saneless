---
phase: 20
slug: ci-gate
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-09-09
---

# Phase 20 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 (9.1.1 available; bumping is Phase 32) |
| **Config file** | `pyproject.toml` → `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest -m "not browser" -q` |
| **Full suite command** | `uv run pytest -m "not browser"` |
| **Estimated runtime** | ~27 seconds (measured: 332 passed, 8 deselected, 26.8s) |

Measured twice independently (discussion + research), with and without `pytest-timeout` loaded, and with and without Playwright browsers present. No `playwright install` step is needed in CI — `-m "not browser"` passes with an empty browser cache.

---

## Sampling Rate

- **After every task commit:** `uv run prek run` (runs ruff, ruff-format, ty, pyrefly) + `uv run pytest -m "not browser" -q`
- **After every plan wave:** the full five-check sequence
- **Before `/gsd-verify-work`:** green GitHub Actions run + ruleset read-back
- **Max feedback latency:** ~30 seconds locally; ~2–3 minutes for a CI round trip

---

## Per-Task Verification Map

| Requirement | Behavior | Test Type | Automated Command | File Exists | Status |
|---|---|---|---|---|---|
| TEST-07 | `pytest-timeout` installed; its ini keys accepted under `--strict-config` | smoke | `uv run pytest -m "not browser" -q` (a bad ini key is a hard error under `--strict-config`) | ✅ existing suite | ⬜ pending |
| TEST-07 | Config values really are `timeout = 60`, `timeout_method = "signal"` | smoke | `uv run pytest --collect-only -q` after the edit; assert config via `grep -A2 timeout pyproject.toml` | ✅ config assertion | ⬜ pending |
| TEST-07 | A hung test is killed with a per-test traceback and the suite continues | integration | throwaway file + `uv run pytest <file> -o timeout=3` → expect `1 failed, 1 passed` | ❌ transient, not committed (see note) | ⬜ pending |
| TEST-07 | No new warning under `filterwarnings = ["error"]` | smoke | full suite green after adding the plugin | ✅ | ⬜ pending |
| CI-01 | All five checks pass locally | smoke | `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pyrefly check && uv run pytest -m "not browser"` | ✅ | ⬜ pending |
| CI-01 (D-23) | Repo is private before any push | e2e (out-of-repo) | `gh repo view kdknigga/scanless --json isPrivate --jq .isPrivate` → `true` | ✅ gh | ⬜ pending |
| CI-01 (D-22) | Pushed branch contains no `.planning/` | e2e (out-of-repo) | `git ls-tree -r --name-only <pushed-ref> \| grep -c '^\.planning/'` → `0` | ✅ git | ⬜ pending |
| CI-01 | The workflow triggers and completes green on the PR | e2e (out-of-repo) | `gh run list --workflow=ci.yml --limit 1 --json conclusion,headBranch,event` → `conclusion == "success"`, `event == "pull_request"` | ✅ gh | ⬜ pending |
| CI-01 (D-05) | **Both** check runs are reported by the GitHub Actions app | e2e (out-of-repo) | `gh api repos/kdknigga/scanless/commits/$SHA/check-runs --jq '[.check_runs[].name]'` → both job names present | ✅ gh | ⬜ pending |
| CI-01 (D-08.3) | A seeded one-line break turns the run red | e2e (out-of-repo) | throwaway branch → PR → `gh run watch` → `conclusion == "failure"` | ✅ gh | ⬜ pending |
| CI-01 (D-03) | Ruleset exists, is `active`, targets `refs/heads/master`, requires **both** contexts | e2e (out-of-repo) | the `gh api …/rulesets/$RID --jq` read-back in RESEARCH.md § Code Examples | ✅ gh | ⬜ pending |
| CI-01 (D-16) | Bumped checkers pass clean | smoke | `uv run ty check && uv run pyrefly check` | ✅ (pyrefly already 0; ty needs 3 fixes) | ⬜ pending |
| CI-01 (D-21) | The PR is **not** merged | e2e (out-of-repo) | `gh pr view <n> --json state --jq .state` → `OPEN` | ✅ gh | ⬜ pending |

> **Note on the hang test:** committing a permanently-hanging test would add 60s to every run. Demonstrate it once with a throwaway file, capture the output as evidence, delete it. Do **not** add a `@pytest.mark.timeout(1)`-plus-`sleep(5)` test to `tests/` as a "regression test" — that tests pytest-timeout, not this project. (Same reasoning as D-10.)

---

## Wave 0 Requirements

- [ ] `uv add --dev pytest-timeout` must precede any run relying on the new ini keys — `--strict-config` makes the ordering load-bearing.

Otherwise: existing infrastructure covers all phase requirements. `tests/` and the pytest config already exist and are green.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|---|---|---|---|
| Merging the pull request | — | **Not manual-because-hard — manual by standing user rule (D-21).** The user does all merging. No automation may perform it. | User merges when and if they choose. The phase completes with the PR open. |

All other phase behaviors have automated verification. The out-of-repo rows are automated via the `gh` CLI (authenticated as `kdknigga`), not deferred to a human — they were blocked on the empty-remote question, which D-20..D-24 resolved.

---

## Validation Sign-Off

- [x] All tasks have automated verify or a Wave 0 dependency
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers the one MISSING reference (`pytest-timeout` install ordering)
- [x] No watch-mode flags
- [x] Feedback latency < 30s locally
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
