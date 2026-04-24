---
phase: 17
slug: fix-paperless-upload-error-datetime-format-and-title-type-mismatch-in-api-payload
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-22
---

# Phase 17 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (strict mode, strict markers) |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_paperless.py tests/test_pipeline.py -x` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_paperless.py tests/test_pipeline.py -x`
- **After every plan wave:** Run `uv run pytest`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 17-01-01 | 01 | 1 | D-01/D-02 | unit | `uv run pytest tests/test_pipeline.py -x -k "created or upload"` | Partial | ⬜ pending |
| 17-01-02 | 01 | 1 | D-03/D-04 | unit | `uv run pytest tests/test_paperless.py -x -k "upload"` | ❌ W0 | ⬜ pending |
| 17-01-03 | 01 | 1 | D-05 | unit | `uv run pytest tests/test_paperless.py::TestUploadDocument::test_upload_with_created -x` | Needs update | ⬜ pending |
| 17-01-04 | 01 | 1 | D-06 | unit | `uv run pytest tests/test_paperless.py -x -k "form_fields"` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_paperless.py` — new test for data=/files= separation (D-06)
- [ ] `tests/test_paperless.py` — update test_upload_with_created to assert date-only format (D-05)

*Existing infrastructure covers all fixture needs.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real Paperless-ngx upload succeeds | D-01/D-03 | Requires running Paperless-ngx instance | Upload a scan with Auto source, verify document appears in Paperless |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
