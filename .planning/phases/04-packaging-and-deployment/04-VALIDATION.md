---
phase: 4
slug: packaging-and-deployment
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-20
---

# Phase 4 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest >=9.0.2 |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_cli.py tests/test_paperless.py -x` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~10 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_cli.py tests/test_paperless.py -x`
- **After every plan wave:** Run `uv run pytest && uv run ruff check . && uv run ruff format --check .`
- **Before `/gsd:verify-work`:** Full suite must be green + `uv build` succeeds
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 04-01-01 | 01 | 1 | CLI-03 | unit | `uv run pytest tests/test_cli.py -x -k jobs` | No -- Wave 0 | ⬜ pending |
| 04-01-02 | 01 | 1 | PLSS-06 | unit | `uv run pytest tests/test_paperless.py -x -k consume` | Partial | ⬜ pending |
| 04-02-01 | 02 | 2 | PKG-01 | smoke | `uv build && uv run --with dist/*.whl --no-project -- python -c "import saneless"` | No -- Wave 0 | ⬜ pending |
| 04-02-02 | 02 | 2 | PKG-02 | smoke | `docker build -t saneless-test .` | No -- Wave 0 | ⬜ pending |
| 04-02-03 | 02 | 2 | PKG-03 | smoke | `docker run --rm saneless-test --help` | No -- Wave 0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_cli.py::TestJobsCommand` — stubs for CLI-03 (jobs table, --json, --limit)
- [ ] `tests/test_paperless.py::TestConsumeDir` — stubs for PLSS-06 startup validation
- [ ] Dockerfile build smoke test (CI workflow, not pytest)
- [ ] `uv build` smoke test (CI workflow, not pytest)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| GHCR image published | PKG-02 | Requires GitHub Actions + GHCR credentials | Push a version tag, verify image appears at ghcr.io |
| PyPI package published | PKG-01 | Requires PyPI trusted publisher setup | Push a version tag, verify package at pypi.org/project/saneless |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
