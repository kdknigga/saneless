---
phase: 31
slug: delivery-identity-and-documentation-accuracy
status: approved
nyquist_compliant: true
wave_0_complete: false
created: 2026-09-18
updated: 2026-09-18
---

# Phase 31 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `31-RESEARCH.md` § Validation Architecture (measured, not estimated).
> Task IDs filled in after planning (10 plans, 8 waves, commit `0f4ad70`) and confirmed
> against the plans by `gsd-plan-checker` (0 blockers).

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 (+ pytest-timeout, pytest-playwright) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest -m "not browser and not sane_hardware" -x -q` |
| **Full suite command** | `uv run pytest -m "not browser and not sane_hardware" && uv run pytest -m sane_hardware && uv run pytest -m browser` |
| **Estimated runtime** | ~27 s quick (3066 tests collected, measured) |
| **Doc-truth harness** | `tests/test_deployment_config.py` (1085 lines) — the file every new guard and audit test joins |
| **Tool-as-test** | `uv run zizmor .` — for DLVR-03 the tool *is* the test (baseline today: 23 findings, exit 14; target: exit 0) |

---

## Sampling Rate

- **After every task commit:** `uv run pytest -m "not browser and not sane_hardware" -x -q`
- **After every plan wave:** full suite, plus `uv run prek run --all-files` and
  `uv run prek run --stage pre-push --all-files`
- **Before `/gsd-verify-work`:** full suite green **and** `uv run zizmor .` exit 0,
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
  `uv run pyrefly check src tests`
- **Max feedback latency:** 30 seconds for `unit` tasks. Three tasks are declared exceptions
  (see *Latency Exceptions* below) because what they verify is a real image build or a real
  clean-machine install, not a unit-test loop.

---

## Per-Task Verification Map

Tasks are positional within each plan (`31-NN` task M). `Threat Ref` cites the plan's own
`<threat_model>` STRIDE register (`T-31-01`..`T-31-37`, plus `T-31-SC`).

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 31-01-01 | 01 | 1 | DLVR-03, DLVR-08 | T-31-SC | Dev dependency is the audited `zizmor`; pyproject declares SPDX license with no legacy classifier | unit | `uv run pytest -k pep639 -q` | ❌ W0 | ⬜ pending |
| 31-01-02 | 01 | 1 | DLVR-10 | — | `--version` reports the installed version, exit 0 | unit | `uv run pytest tests/test_cli.py -k version -q` | ❌ W0 | ⬜ pending |
| 31-02-01 | 02 | 2 | CI-02, DLVR-01 | T-31-ID | No shipped file names the wrong account; guard, rename and the three Phase-30 deletions in ONE commit | unit | `uv run pytest tests/test_deployment_config.py -k naming -q` | ❌ W0 | ⬜ pending |
| 31-02-02 | 02 | 2 | DOCS-02 | — | README examples run as written; deep links resolve (rows 16, 17, 18) | unit | `uv run pytest tests/test_deployment_config.py -k readme -q` | ❌ W0 | ⬜ pending |
| 31-03-01 | 03 | 2 | DLVR-04 | — | `configure_logging(stream=True)` attaches a stderr handler and no file handler | unit | `uv run pytest tests/test_logging.py -q` | ✅ (16 existing tests must stay green unmodified) | ⬜ pending |
| 31-03-02 | 03 | 2 | DLVR-04 | — | `serve` streams and sets no `log_file`; one-shot commands unchanged; serve renders tracebacks, CLI does not without `-v` | unit | `uv run pytest tests/test_cli.py -k "serve or logging" -q` | ❌ W0 | ⬜ pending |
| 31-03-03 | 03 | 2 | DLVR-04 | — | The two reference pages state which mode each log key governs | unit | `uv run pytest tests/test_deployment_config.py -q` | ✅ (harness exists) | ⬜ pending |
| 31-04-01 | 04 | 2 | CI-02, DLVR-03 | T-31-SUPPLY | All actions SHA-pinned, per-job `permissions:`, no persisted credentials; zizmor in CI and prek | tool | `uv run zizmor .` (exit 0) | ❌ W0 | ⬜ pending |
| 31-04-02 | 04 | 2 | DLVR-02, DLVR-03 | T-31-PUB | Release gate reuses `ci.yml`; pre-release routes to TestPyPI; attestations and provenance on | tool | `uv run zizmor .` (exit 0) + `gh workflow view release.yml` | ❌ W0 | ⬜ pending |
| 31-04-03 | 04 | 2 | DLVR-03 | T-31-SUPPLY | Docs publish via the Pages artifact flow; Dependabot covers the docker ecosystem | tool + build | `uv run zizmor .` + `uv run mkdocs build --strict` | ❌ W0 | ⬜ pending |
| 31-05-01 | 05 | 3 | DLVR-06, DLVR-09 | T-31-CTX | Allow-list starts with `*`, re-includes nothing sensitive; no `*.png` glob remains | unit | `uv run pytest tests/test_deployment_config.py -k "dockerignore or gitignore" -q` | ❌ W0 | ⬜ pending |
| 31-05-02 | 05 | 3 | DLVR-07 | T-31-PRIV | Runs as UID 1000, bases digest-pinned, `WORKDIR` set, explicit `COPY` | static + **build** | `uv run pytest tests/test_deployment_config.py -k dockerfile -q` then `docker build -t saneless-local-verify . && docker run --rm --entrypoint id saneless-local-verify` | ❌ W0 | ⬜ pending |
| 31-05-03 | 05 | 3 | DLVR-05 | — | Example config, `EXPOSE`, `HEALTHCHECK` and every doc page agree on 8080 | unit | `uv run pytest tests/test_deployment_config.py -k port -q` | ❌ W0 | ⬜ pending |
| 31-06-01 | 06 | 4 | DOCS-04 | — | "Which setup do I have?" exists and leads Getting Started in the nav | unit + build | `uv run pytest tests/test_deployment_config.py -q` + `uv run mkdocs build --strict` | ❌ W0 | ⬜ pending |
| 31-06-02 | 06 | 4 | DOCS-04 | — | One USB rule across all four statements; the passthrough section is gone (row 29) | unit | `uv run pytest tests/test_deployment_config.py -k usb -q` | ❌ W0 | ⬜ pending |
| 31-06-03 | 06 | 4 | DOCS-05 | — | No-login / all-interfaces sentence on quick-start and the Docker page, each link resolving | unit | `uv run pytest tests/test_deployment_config.py -k trust -q` | ❌ W0 | ⬜ pending |
| 31-07-01 | 07 | 5 | DOCS-01 | — | Rows 30 and 31: UUID4 job-id example; every `docker run -v` host path uses `$(pwd)` | unit | `uv run pytest tests/test_deployment_config.py -q` | ✅ (harness exists) | ⬜ pending |
| 31-07-02 | 07 | 5 | DOCS-06 | — | Row 32: the partial Paperless service is replaced by a pointer | unit | `uv run pytest tests/test_deployment_config.py -k paperless -q` | ❌ W0 | ⬜ pending |
| 31-07-03 | 07 | 5 | DOCS-03 | — | Row 34: `docs/PRD.md` no longer publishes | unit + build | `git ls-files docs/ \| grep -c PRD` → 0, `uv run mkdocs build --strict` | ❌ W0 | ⬜ pending |
| 31-08-01 | 08 | 6 | DOCS-01 | — | All 34 rows have a disposition, evidence and a Defence classification | recorded read + unit | `uv run pytest tests/test_deployment_config.py -q` + `31-AUDIT.md` review | ❌ W0 | ⬜ pending |
| 31-08-02 | 08 | 6 | DOCS-01, DOCS-02 | — | The ~20 "checked and correct" claims are re-verified against today's code (D-44) | recorded read | `31-AUDIT.md` second table complete | ❌ W0 | ⬜ pending |
| 31-09-01 | 09 | 7 | DLVR-02 | T-31-PUB | Pending publishers, the two publish environments with an approval rule on `pypi`, Pages source | **manual — blockers 1,2,3,5** | n/a — `checkpoint:human-action` | ❌ blocked | ⬜ pending |
| 31-09-02 | 09 | 7 | DLVR-02 | — | RC version set; CI green at the commit the tag will point at | unit + tool | `uv run pytest …` + `gh run list` | ❌ W0 | ⬜ pending |
| 31-09-03 | 09 | 7 | DLVR-02 | T-31-PUB | RC tag pushed **by the user**; `git tag -l` unchanged by Claude | **manual — blocker 6** | n/a — `checkpoint:human-action` | ❌ blocked | ⬜ pending |
| 31-10-01 | 10 | 8 | DLVR-02 | T-31-PUB | GHCR package public (one-way; impossible before first push) | **manual — blocker 4** | n/a — `checkpoint:human-action` | ❌ blocked | ⬜ pending |
| 31-10-02 | 10 | 8 | DLVR-02, DOCS-01 | T-31-PUB | Published wheel and image install and run from clean hosts | **manual-gated build** | `docker run --rm python:3.14-slim …` + `docker pull` (see Manual-Only below) | ❌ blocked | ⬜ pending |
| 31-10-03 | 10 | 8 | DLVR-02 | — | 0.2.0 restored; final release commands handed over, not run | unit | `uv run pytest -k pep639 -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Latency Exceptions

Three tasks exceed the 30 s budget by nature, not by neglect. Each is bucketed as `build` or
`manual`, never `unit`:

| Task | Why it exceeds 30 s |
|------|---------------------|
| 31-05-02 | Chains a real multi-stage `docker build` after pytest — the only way to prove UID 1000 rather than read it out of a Dockerfile |
| 31-10-02 | Runs `apt-get install libsane-dev && pip install` inside a throwaway container — that *is* the clean-machine proof criterion 2 demands |
| 31-04-03 | `mkdocs build --strict` over the whole site |

The fast unit loop is unaffected: no plan makes a `unit`-bucketed task wait on a build.

---

## Wave 0 Requirements

- [ ] `uv add --dev zizmor` and regenerate `uv.lock` (31-01-01) — **nothing in DLVR-03 is verifiable until this lands**
- [ ] `uv sync` after the version bump to 0.2.0 (31-01-01), or every `--version` assertion reads `0.1.0`
- [ ] New sections in `tests/test_deployment_config.py`: naming guard, `.dockerignore`, Dockerfile, `.gitignore`, pyproject PEP 639 shape
- [ ] New serve-logging tests in `tests/test_cli.py`
- [ ] `31-AUDIT.md` skeleton with all 34 rows plus the ~20 re-checked "checked and correct" claims

---

## Manual-Only Verifications

Manual **only** because they need a human credential or a one-way account setting — not because
they are hard to automate.

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| A pre-release tag publishes to TestPyPI and the wheel installs | DLVR-02, criterion 2 | Pending trusted publishers (blockers 1-2), the two publish environments (blocker 3), a user-pushed tag (blocker 6, D-20) | User pushes `v0.2.0-rc.1`; then `docker run --rm python:3.14-slim sh -c 'apt-get update && apt-get install -y libsane-dev && pip install -i https://test.pypi.org/simple/ saneless==0.2.0rc1 && saneless --version'` |
| The published image pulls and runs anonymously | DLVR-02, criterion 2 | GHCR package visibility (blocker 4) — one-way, and impossible before the first push | `docker pull ghcr.io/kdknigga/saneless:latest && docker run --rm ghcr.io/kdknigga/saneless:latest --version`. **This host is podman/rootless/SELinux** — bind-mount checks need `--userns=keep-id:uid=1000,gid=1000` and `:Z` |
| The docs site serves and all five README deep links resolve | DOCS-01, criterion 5 | Settings → Pages → Source: **GitHub Actions** (blocker 5, amended from the gh-pages branch) | After the Pages-artifact workflow runs green, fetch each README docs link and assert HTTP 200 |
| Rows resolved by reading a paragraph (e.g. row 26's responsiveness claim) | DOCS-01 | Prose judgement; a keyword ban would be brittle and get weakened later (D-41) | Recorded verified read with `file:line` in `31-AUDIT.md`, marked point-in-time rather than permanently defended |

**Blocked is not passed.** 31-09 and 31-10 instruct the executor to record any ungranted
verification in `31-AUDIT.md` as *blocked, with the reason* — never as passed, never as skipped.

*Everything else in this phase has automated verification.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies — the only tasks without one are
      the three `checkpoint:human-action` tasks (31-09-01, 31-09-03, 31-10-01), which are
      inherently manual and correctly typed
- [x] Sampling continuity: no 3 consecutive tasks without automated verify (verified across all
      27 tasks)
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 30s for every `unit` task; three `build`/`manual` exceptions declared above
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-09-18 (post plan-check, 0 blockers)
