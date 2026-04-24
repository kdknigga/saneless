---
phase: 7
slug: tech-debt-cleanup
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-21
---

# Phase 7 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/ -x -q` |
| **Full suite command** | `uv run pytest tests/ -v` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x -q`
- **After every plan wave:** Run `uv run pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 07-01-01 | 01 | 1 | PKG-01 | unit | `uv run pytest tests/test_config.py -x -q` | ✅ | ⬜ pending |
| 07-01-02 | 01 | 1 | UI-02 | unit | `uv run pytest tests/test_worker.py -x -q` | ✅ | ⬜ pending |
| 07-01-03 | 01 | 1 | ARCH-02 | unit | `uv run pytest tests/test_worker.py -x -q` | ✅ | ⬜ pending |
| 07-02-01 | 02 | 2 | UI-01, UI-02, UI-03 | e2e | `uv run pytest tests/test_browser.py -x -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_browser.py` — Playwright browser test stubs for PicoCSS rendering, HTMX polling, flip prompt UI
- [ ] `pytest-playwright` dev dependency installed

*Existing infrastructure covers unit/integration test requirements. Browser tests are new.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Physical scanner discovery | (deferred) | Requires real SANE hardware | Not in phase scope |

*All in-scope phase behaviors have automated verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
