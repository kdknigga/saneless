---
phase: 12
slug: ui-polish-humanize-enum-labels-add-accessible-button-labels-fix-cli-table-truncation-replace-inline-htmx-scripts-normalize-spacing-to-design-token-grid
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-22
---

# Phase 12 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2+ with pytest-playwright 0.7.0+ |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_web.py tests/test_cli.py -x -q` |
| **Full suite command** | `uv run pytest -x -q` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_web.py tests/test_cli.py -x -q`
- **After every plan wave:** Run `uv run pytest -x -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 12-01-01 | 01 | 1 | P12-01 | unit + browser | `uv run pytest tests/test_web.py -x -q -k history` | Partial | ⬜ pending |
| 12-02-01 | 02 | 1 | P12-02 | browser | `uv run pytest tests/test_browser.py -x -q -k refresh` | Partial | ⬜ pending |
| 12-03-01 | 03 | 1 | P12-03 | unit | `uv run pytest tests/test_cli.py -x -q -k devices` | Partial | ⬜ pending |
| 12-04-01 | 04 | 1 | P12-04 | unit | `uv run pytest tests/test_web.py -x -q -k script` | ❌ W0 | ⬜ pending |
| 12-05-01 | 05 | 1 | P12-05 | browser | `uv run pytest tests/test_browser.py -x -q -k spacing` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_web.py` — add test for humanize_state filter output in history response
- [ ] `tests/test_web.py` — add test for no `<script>` tags in status partial response
- [ ] `tests/test_cli.py` — add test for CLI truncation with long device names
- [ ] `tests/test_browser.py` — add Playwright test for aria-label on refresh buttons

*Existing infrastructure partially covers phase requirements — Wave 0 fills gaps.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Scanner hardware interaction | N/A | Requires physical scanner | Connect scanner and verify scan workflow |

*All UI behaviors have automated verification via Playwright.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
