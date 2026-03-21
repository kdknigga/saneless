---
phase: 08
slug: audit-lint-and-type-checker-ignores-and-noqas-and-fix-them
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-21
---

# Phase 08 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.x + ruff 0.15.x + ty 0.0.21 + pyrefly 0.55.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run ruff check . && uv run ty check && uv run pyrefly check` |
| **Full suite command** | `uv run ruff check . && uv run ty check && uv run pyrefly check && uv run pytest tests/ -q` |
| **Estimated runtime** | ~30 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run ruff check . && uv run ty check && uv run pyrefly check`
- **After every plan wave:** Run `uv run ruff check . && uv run ty check && uv run pyrefly check && uv run pytest tests/ -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 08-01-01 | 01 | 1 | CODE-QUALITY | lint+type | `uv run ruff check src/ && uv run ty check` | ✅ | ⬜ pending |
| 08-01-02 | 01 | 1 | CODE-QUALITY | lint+type | `uv run ruff check . && uv run ty check && uv run pyrefly check` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. No new test files needed — validation is via linter/type checker clean output.

---

## Manual-Only Verifications

All phase behaviors have automated verification. Lint and type checker output is fully machine-checkable.

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
