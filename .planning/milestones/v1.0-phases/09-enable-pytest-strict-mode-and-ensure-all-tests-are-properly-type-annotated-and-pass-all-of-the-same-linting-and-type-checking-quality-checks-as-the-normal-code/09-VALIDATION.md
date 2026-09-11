---
phase: 9
slug: enable-pytest-strict-mode-and-ensure-all-tests-are-properly-type-annotated-and-pass-all-of-the-same-linting-and-type-checking-quality-checks-as-the-normal-code
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-21
---

# Phase 9 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (via uv run pytest) |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest -x -q` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest -x -q`
- **After every plan wave:** Run `uv run pytest && uv run ruff check . && uv run ty check && uv run pyrefly check src tests`
- **Before `/gsd:verify-work`:** Full suite + all linters must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 09-01-01 | 01 | 1 | Type checker parity | integration | `uv run ty check && uv run pyrefly check src tests` | ✅ | ⬜ pending |
| 09-01-02 | 01 | 1 | Stub class fixes | unit | `uv run pytest tests/test_browser.py tests/test_scanner.py -x` | ✅ | ⬜ pending |
| 09-01-03 | 01 | 1 | None narrowing | unit | `uv run pytest tests/test_job.py -x` | ✅ | ⬜ pending |
| 09-02-01 | 02 | 2 | ANN compliance | lint | `uv run ruff check tests/ --select ANN` | ✅ | ⬜ pending |
| 09-02-02 | 02 | 2 | Docstring compliance | lint | `uv run ruff check tests/ --select D` | ✅ | ⬜ pending |
| 09-02-03 | 02 | 2 | Lazy import cleanup | lint | `uv run ruff check tests/ --select PLC0415` | ✅ | ⬜ pending |
| 09-03-01 | 03 | 3 | Pytest strict mode | integration | `uv run pytest --strict-markers --strict-config -W error` | ✅ | ⬜ pending |
| 09-03-02 | 03 | 3 | Full quality parity | integration | `uv run prek run` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. No new test files or frameworks needed — this phase modifies existing test files and configuration to meet higher quality standards.

---

## Manual-Only Verifications

All phase behaviors have automated verification.

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
