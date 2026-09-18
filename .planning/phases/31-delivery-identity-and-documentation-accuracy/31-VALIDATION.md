---
phase: 31
slug: delivery-identity-and-documentation-accuracy
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-09-18
---

# Phase 31 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `31-RESEARCH.md` § Validation Architecture (measured, not estimated).

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
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

> Task IDs are assigned by the planner. This table is the **requirement → command** contract
> the planner must satisfy; it is completed with task IDs once plans exist.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | TBD | 0 | CI-02, DLVR-01 | T-31-ID | No shipped file names the wrong account | unit | `uv run pytest tests/test_deployment_config.py -k naming_guard -q` | ❌ W0 | ⬜ pending |
| TBD | TBD | 0 | DLVR-03 | T-31-SUPPLY | Workflows audit clean; no unpinned action, no persisted credential, scoped permissions | tool | `uv run zizmor .` | ❌ W0 | ⬜ pending |
| TBD | TBD | 1 | DLVR-04 | — | `serve` streams to stderr and attaches no file handler | unit | `uv run pytest tests/test_cli.py -k serve_logging -q` | ❌ W0 | ⬜ pending |
| TBD | TBD | 1 | DLVR-04 (D-34) | — | One-shot commands still attach the file handler and set `ctx.obj["log_file"]` | unit | `uv run pytest tests/test_cli.py -k cli_logging_unchanged -q` | ✅ (exists, must stay green) | ⬜ pending |
| TBD | TBD | 1 | DLVR-04 (D-36 amended) | — | Stream renders tracebacks in serve mode; CLI stays traceback-free without `-v` | unit | `uv run pytest tests/test_cli.py -k traceback -q` | ❌ W0 | ⬜ pending |
| TBD | TBD | 1 | DLVR-05 | — | Example config, `EXPOSE`, `HEALTHCHECK` and every doc page agree on 8080 | unit | `uv run pytest tests/test_deployment_config.py -k web_port -q` | ❌ W0 | ⬜ pending |
| TBD | TBD | 1 | DLVR-06 | T-31-CTX | Allow-list starts with `*`; no config, secret, `.planning/` or tests path re-included | unit | `uv run pytest tests/test_deployment_config.py -k dockerignore -q` | ❌ W0 | ⬜ pending |
| TBD | TBD | 1 | DLVR-07 | T-31-PRIV | Non-root UID 1000, digest-pinned bases, `WORKDIR` set | static | `uv run pytest tests/test_deployment_config.py -k dockerfile -q` + `docker run --rm <img> id` | ❌ W0 | ⬜ pending |
| TBD | TBD | 1 | DLVR-08 | — | pyproject carries SPDX `license` + `license-files`, **no** `License ::` classifier | unit | `uv run pytest -k pep639 -q` | ❌ W0 | ⬜ pending |
| TBD | TBD | 1 | DLVR-08 | — | Built wheel contains `saneless-*.dist-info/licenses/LICENSE` | build | `uv build --wheel && unzip -l dist/*.whl` | ❌ W0 | ⬜ pending |
| TBD | TBD | 1 | DLVR-09 | — | No `*.png` glob remains; `test-results/` ignored instead | unit | `uv run pytest tests/test_deployment_config.py -k gitignore -q` | ❌ W0 | ⬜ pending |
| TBD | TBD | 1 | DLVR-10 | — | `--version` prints the installed version, exit 0 | unit | `uv run pytest tests/test_cli.py -k version_option -q` | ❌ W0 | ⬜ pending |
| TBD | TBD | 2 | DOCS-01..DOCS-06 | — | Every statically checkable row of review § 8 is pinned; the rest recorded with `file:line` | unit + recorded read | `uv run pytest tests/test_deployment_config.py -q` + the D-42 audit artifact | partly ✅ (harness exists) | ⬜ pending |
| TBD | TBD | 3 | DLVR-02 | T-31-PUB | Published wheel and image install and run from a clean host | **manual — gated on blockers 1-4, 6** | `pip install` + `docker pull` in throwaway containers (D-21) | ❌ blocked | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `uv add --dev zizmor` and regenerate `uv.lock` — **nothing in DLVR-03 is verifiable until this lands**
- [ ] `uv sync` after the version bump to 0.2.0, or every `--version` assertion reads `0.1.0`
- [ ] New sections in `tests/test_deployment_config.py`: the naming guard, the `.dockerignore`
      contract, the Dockerfile contract, the `.gitignore` contract, the pyproject PEP 639 shape
- [ ] New `TestServeLogging` class in `tests/test_cli.py`
- [ ] The D-42 audit artifact skeleton in this phase directory, with all 34 rows plus the
      ~20 re-checked "checked and correct" claims

---

## Manual-Only Verifications

These are manual because they depend on **the user's accounts**, not because they are hard to
automate. Per the project rule, a check is only "manual" when it needs a human credential,
physical hardware, or an external service that cannot be stubbed — all four below qualify.

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| A pre-release tag publishes to TestPyPI and the wheel installs | DLVR-02, criterion 2 | Needs pending trusted publishers (blockers 1-2), the two publish environments (blocker 3), and a user-pushed tag (blocker 6, D-20) | Push `v0.2.0-rc.1`; watch the run; then `docker run --rm python:3.14-slim sh -c 'apt-get update && apt-get install -y libsane-dev && pip install -i https://test.pypi.org/simple/ saneless==0.2.0rc1 && saneless --version'` |
| The published image pulls and runs anonymously | DLVR-02, criterion 2 | Needs GHCR package visibility set to public (blocker 4) | `docker pull ghcr.io/kdknigga/saneless:latest && docker run --rm ghcr.io/kdknigga/saneless:latest --version` — note this host is **podman/rootless/SELinux**, so bind-mount checks need `--userns=keep-id:uid=1000,gid=1000` and `:Z` |
| The docs site serves at `kdknigga.github.io/saneless` and all five README deep links resolve | DOCS-01, criterion 5 | Needs Settings → Pages → Source: **GitHub Actions** (blocker 5, amended) | After the Pages-artifact workflow runs green, fetch each README docs link and assert HTTP 200 |
| Rows resolved by a human reading a paragraph (e.g. row 26's responsiveness claim) | DOCS-01 | Prose judgement; forcing it into a test yields a brittle keyword ban that gets weakened later (D-41) | Recorded verified read with `file:line` in the D-42 audit artifact |

*Everything else in this phase has automated verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
