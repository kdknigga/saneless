---
phase: 1
slug: core-pipeline
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-20
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 |
| **Config file** | `pyproject.toml` [tool.pytest.ini_options] |
| **Quick run command** | `uv run pytest tests/ -x -q` |
| **Full suite command** | `uv run pytest tests/ -v` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x -q`
- **After every plan wave:** Run `uv run pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 01-01-01 | 01 | 1 | CONF-01/02/03 | unit | `uv run pytest tests/test_config.py -v` | ❌ W0 | ⬜ pending |
| 01-01-02 | 01 | 1 | PROF-01/02 | unit | `uv run pytest tests/test_config.py -v` | ❌ W0 | ⬜ pending |
| 01-02-01 | 02 | 1 | ARCH-01 | unit | `uv run pytest tests/test_scanner.py -v` | ❌ W0 | ⬜ pending |
| 01-02-02 | 02 | 1 | SCAN-01/02/03 | unit | `uv run pytest tests/test_scanner.py -v` | ❌ W0 | ⬜ pending |
| 01-02-03 | 02 | 1 | PDF-01/02 | unit | `uv run pytest tests/test_pdf.py -v` | ❌ W0 | ⬜ pending |
| 01-03-01 | 03 | 2 | PLSS-01/02/03 | unit | `uv run pytest tests/test_paperless.py -v` | ❌ W0 | ⬜ pending |
| 01-03-02 | 03 | 2 | ARCH-02/03 | unit | `uv run pytest tests/test_worker.py -v` | ❌ W0 | ⬜ pending |
| 01-03-03 | 03 | 2 | CLI-01/02 | unit | `uv run pytest tests/test_cli.py -v` | ❌ W0 | ⬜ pending |
| 01-03-04 | 03 | 2 | LOG-01/02/04 | unit | `uv run pytest tests/test_logging.py -v` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/conftest.py` — shared fixtures (mock scanner backend, temp directories, mock paperless server)
- [ ] `tests/test_config.py` — stubs for CONF-01/02/03, PROF-01/02
- [ ] `tests/test_scanner.py` — stubs for ARCH-01, SCAN-01/02/03
- [ ] `tests/test_pdf.py` — stubs for PDF-01/02
- [ ] `tests/test_paperless.py` — stubs for PLSS-01/02/03
- [ ] `tests/test_worker.py` — stubs for ARCH-02/03
- [ ] `tests/test_cli.py` — stubs for CLI-01/02
- [ ] `tests/test_logging.py` — stubs for LOG-01/02/04

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Scan produces actual image from hardware | SCAN-03 | Requires physical scanner | Connect scanner, run `saneless scan --profile default --title "Test"`, verify PDF created |
| PDF appears in paperless-ngx | PLSS-01 | Requires running paperless-ngx instance | Run scan with valid paperless config, check paperless UI for document |
| saned network connectivity | SCAN-01 | Requires network scanner setup | Run `saneless devices` with saned on remote host |

*All other behaviors have automated verification via mock backends.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
