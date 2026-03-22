---
phase: 15
slug: create-user-facing-documentation-using-the-di-taxis-approach
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-22
---

# Phase 15 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | mkdocs build validation + link checker |
| **Config file** | `mkdocs.yml` (created in Wave 1) |
| **Quick run command** | `uv run mkdocs build --strict 2>&1 | tail -5` |
| **Full suite command** | `uv run mkdocs build --strict && echo "BUILD OK"` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run mkdocs build --strict 2>&1 | tail -5`
- **After every plan wave:** Run `uv run mkdocs build --strict && echo "BUILD OK"`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 15-01-01 | 01 | 1 | D-03/D-04 | build | `uv run mkdocs build --strict` | ❌ W0 | ⬜ pending |
| 15-02-01 | 02 | 2 | D-05/D-06 | build | `uv run mkdocs build --strict` | ❌ W0 | ⬜ pending |
| 15-03-01 | 03 | 2 | D-07 | build | `uv run mkdocs build --strict` | ❌ W0 | ⬜ pending |
| 15-04-01 | 04 | 2 | D-08 | build | `uv run mkdocs build --strict` | ❌ W0 | ⬜ pending |
| 15-05-01 | 05 | 2 | D-09 | build | `uv run mkdocs build --strict` | ❌ W0 | ⬜ pending |
| 15-06-01 | 06 | 3 | D-17/D-18 | build | `uv run mkdocs build --strict` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `mkdocs.yml` — MkDocs configuration with Material theme
- [ ] `docs/index.md` — Landing page stub
- [ ] `uv add --dev mkdocs-material` — Install dependency

*Wave 0 is embedded in Plan 01 (infrastructure setup).*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| GitHub Pages deployment | D-02 | Requires GitHub Actions + gh-pages branch | Push to main, verify site at GitHub Pages URL |

*All other behaviors have automated verification via `mkdocs build --strict`.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
