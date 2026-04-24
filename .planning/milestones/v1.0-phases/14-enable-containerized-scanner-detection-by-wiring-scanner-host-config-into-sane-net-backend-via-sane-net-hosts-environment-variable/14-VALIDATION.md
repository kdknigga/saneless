---
phase: 14
slug: enable-containerized-scanner-detection-by-wiring-scanner-host-config-into-sane-net-backend-via-sane-net-hosts-environment-variable
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-22
---

# Phase 14 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `uv run pytest tests/test_sane_backend.py -x -q` |
| **Full suite command** | `uv run pytest tests/ -x -q` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_sane_backend.py -x -q`
- **After every plan wave:** Run `uv run pytest tests/ -x -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 14-01-01 | 01 | 1 | env-var-injection | unit | `uv run pytest tests/test_sane_backend.py -k "sane_net_hosts" -x -q` | ❌ W0 | ⬜ pending |
| 14-01-02 | 01 | 1 | no-override | unit | `uv run pytest tests/test_sane_backend.py -k "no_override" -x -q` | ❌ W0 | ⬜ pending |
| 14-01-03 | 01 | 1 | empty-host | unit | `uv run pytest tests/test_sane_backend.py -k "empty_host" -x -q` | ❌ W0 | ⬜ pending |
| 14-01-04 | 01 | 1 | cli-passthrough | unit | `uv run pytest tests/test_cli.py -k "scanner_host" -x -q` | ❌ W0 | ⬜ pending |
| 14-01-05 | 01 | 1 | docker-compose | grep | `grep -q "SANELESS_SCANNER__HOST" docker-compose.yml` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_sane_backend.py` — add test cases for SANE_NET_HOSTS env var injection, no-override, and empty-host scenarios
- [ ] `tests/test_cli.py` — add test cases for scanner host passthrough from settings to SaneBackend

*Existing test infrastructure covers framework and fixtures.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Scanner discovered via network | end-to-end | Requires physical/network scanner | Set `scanner.host`, run `saneless devices`, verify scanner listed |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
