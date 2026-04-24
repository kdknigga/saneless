---
phase: 16
slug: when-a-scanner-advertised-auto-mode-it-should-be-configurable-by-the-user-if-that-means-flatbed-mode-or-adf-mode
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-22
---

# Phase 16 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (strict mode, strict markers) |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_scanner.py tests/test_auto_profiles.py tests/test_config.py -x` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_scanner.py tests/test_auto_profiles.py tests/test_config.py -x`
- **After every plan wave:** Run `uv run pytest`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 16-01-01 | 01 | 0 | D-01/D-02 | unit | `uv run pytest tests/test_config.py -x -k auto_source` | ❌ W0 | ⬜ pending |
| 16-01-02 | 01 | 0 | D-06 | unit | `uv run pytest tests/test_scanner.py -x -k scan_settings` | ❌ W0 | ⬜ pending |
| 16-01-03 | 01 | 0 | D-04 | unit | `uv run pytest tests/test_scanner.py -x -k auto_source` | ❌ W0 | ⬜ pending |
| 16-01-04 | 01 | 0 | D-08 | unit | `uv run pytest tests/test_auto_profiles.py -x -k auto` | ❌ W0 | ⬜ pending |
| 16-01-05 | 01 | 0 | D-07 | unit | `uv run pytest tests/test_auto_profiles.py -x -k auto` | ❌ W0 | ⬜ pending |
| 16-01-06 | 01 | 1 | D-01 | unit | `uv run pytest tests/test_config.py -x -k auto_source` | ❌ W0 | ⬜ pending |
| 16-01-07 | 01 | 1 | D-06 | unit | `uv run pytest tests/test_scanner.py -x -k scan_settings` | ❌ W0 | ⬜ pending |
| 16-01-08 | 01 | 1 | D-04 | unit | `uv run pytest tests/test_scanner.py -x -k auto_source` | ❌ W0 | ⬜ pending |
| 16-01-09 | 01 | 1 | D-08/D-07 | unit | `uv run pytest tests/test_auto_profiles.py -x -k auto` | ❌ W0 | ⬜ pending |
| 16-01-10 | 01 | 2 | D-04 | unit | `uv run pytest tests/test_scanner.py -x -k auto_source` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_config.py` — stubs for auto_source_mode Literal validation, default, invalid rejection
- [ ] `tests/test_scanner.py` — stubs for ScanSettings auto_source_mode field, scan_pages Auto routing
- [ ] `tests/test_auto_profiles.py` — stubs for source_to_slug("Auto"), generate_profiles Auto source

*Existing infrastructure covers all fixture needs (MockSaneDev, MockSaneModule, sane_backend fixtures).*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real scanner with Auto source routes correctly | D-04 | Requires physical scanner hardware | Configure auto_source_mode="adf", scan via Auto source, verify ADF path used |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
