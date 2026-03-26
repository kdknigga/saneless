---
phase: 19
slug: write-user-facing-docs-including-a-full-getting-started-section-that-walks-a-new-user-through-setup-and-first-scan-using-the-di-taxis-model
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-26
---

# Phase 19 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | MkDocs Material (mkdocs build --strict) |
| **Config file** | mkdocs.yml |
| **Quick run command** | `uv run mkdocs build --strict 2>&1 | tail -20` |
| **Full suite command** | `uv run mkdocs build --strict && echo "BUILD OK"` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run mkdocs build --strict 2>&1 | tail -20`
- **After every plan wave:** Run `uv run mkdocs build --strict && echo "BUILD OK"`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 19-01-01 | 01 | 1 | Getting Started nav | build | `uv run mkdocs build --strict` | ❌ W0 | ⬜ pending |
| 19-01-02 | 01 | 1 | Quick Start page | build + grep | `grep -l "Quick Start" docs/getting-started/quick-start.md` | ❌ W0 | ⬜ pending |
| 19-01-03 | 01 | 1 | CLI Scan page | build + grep | `grep -l "First CLI Scan" docs/getting-started/first-cli-scan.md` | ❌ W0 | ⬜ pending |
| 19-01-04 | 01 | 1 | Web UI Scan page | build + grep | `grep -l "First Web UI Scan" docs/getting-started/first-web-ui-scan.md` | ❌ W0 | ⬜ pending |
| 19-02-01 | 02 | 1 | auto_source_mode docs | grep | `grep -l "auto_source_mode" docs/how-to/configure-scan-profiles.md` | ✅ | ⬜ pending |
| 19-02-02 | 02 | 1 | paper_size docs | grep | `grep -l "paper_size" docs/how-to/configure-scan-profiles.md` | ✅ | ⬜ pending |
| 19-02-03 | 02 | 1 | ADF auto_source_mode | grep | `grep -l "auto_source_mode" docs/how-to/set-up-adf-duplex.md` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `docs/getting-started/` directory created
- [ ] `docs/getting-started/quick-start.md` — stub
- [ ] `docs/getting-started/first-web-ui-scan.md` — stub

*Existing infrastructure (MkDocs, mkdocs.yml) covers build verification.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Docs render correctly in browser | Visual QA | Layout/styling nuance | `uv run mkdocs serve` → check localhost:8000 |

*All other behaviors verified via `mkdocs build --strict` (catches broken links, missing pages, nav errors).*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
