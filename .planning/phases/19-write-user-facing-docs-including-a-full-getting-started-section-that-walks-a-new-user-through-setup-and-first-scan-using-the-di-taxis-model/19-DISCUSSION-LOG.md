# Phase 19: User-Facing Docs — Getting Started & Feature Updates - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-26
**Phase:** 19-write-user-facing-docs
**Areas discussed:** Getting Started scope, Feature documentation gaps, Nav structure, Web UI docs, Quick Start path, Tutorial relocation, Page count

---

## Getting Started Scope

| Option | Description | Selected |
|--------|-------------|----------|
| Expand into multi-page guide | Split into separate pages: Install, Configure, First CLI Scan, First Web UI Scan, Next Steps | |
| Update the existing tutorial | Keep single-page format, refresh for Phases 16-18 features, fix Python version | |
| Add a Quick Start + keep tutorial | Add short Quick Start page as entry point, keep existing tutorial as detailed walkthrough | ✓ |

**User's choice:** Add a Quick Start + keep tutorial
**Notes:** User wants a fast 5-minute entry point plus the existing detailed walkthrough.

---

## Feature Documentation Gaps

| Option | Description | Selected |
|--------|-------------|----------|
| Weave into existing guides | Add auto_source_mode to profile/ADF guides, paper_size to profile guide. No new pages. | ✓ |
| New dedicated how-to pages | Create separate how-to guides for paper size and auto source mode | |
| Config reference is enough | Both fields already documented in config reference, don't add to how-to guides | |

**User's choice:** Weave into existing guides
**Notes:** No new pages needed — extend existing how-to guides with the new fields.

---

## Nav Structure

| Option | Description | Selected |
|--------|-------------|----------|
| New top-level section | Getting Started as first nav item above Tutorials | ✓ |
| Keep under Tutorials | Expand within existing Tutorials section, keep pure Diataxis 4-quadrant nav | |
| Replace Tutorials with Getting Started | Rename Tutorials to Getting Started since it's the only tutorial | |

**User's choice:** New top-level section
**Notes:** Getting Started becomes the first nav item, ahead of Tutorials.

---

## Web UI Documentation

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, as a getting started page | Add "Your First Web UI Scan" as part of the getting started flow | ✓ |
| Yes, as a separate how-to | Create standalone how-to guide for the web UI | |
| No, skip for now | Web UI is self-explanatory, focus on CLI/config docs | |

**User's choice:** Yes, as a getting started page
**Notes:** Web UI scan becomes the third page in Getting Started, after Quick Start and First CLI Scan.

---

## Quick Start Path

| Option | Description | Selected |
|--------|-------------|----------|
| Docker + Web UI | Fastest for self-hosters: docker run, open browser, click Scan | ✓ |
| Bare metal + CLI | pip install, saneless scan. More hands-on. | |
| Both equally | Docker and bare metal side-by-side with tabs | |

**User's choice:** Docker + Web UI
**Notes:** Docker is primary path, bare metal as tab alternative.

---

## Tutorial Relocation

| Option | Description | Selected |
|--------|-------------|----------|
| Move to Getting Started | Relocate and refresh existing tutorial as detailed CLI walkthrough in Getting Started | ✓ |
| Keep in Tutorials, fix version | Leave in place, just fix Python 3.12 → 3.14 | |

**User's choice:** Move to Getting Started
**Notes:** Tutorials section becomes empty/placeholder for future content.

---

## Getting Started Page Count

| Option | Description | Selected |
|--------|-------------|----------|
| 3 pages | Quick Start, First CLI Scan, First Web UI Scan | ✓ |
| 4 pages | Quick Start, Install, First CLI Scan, First Web UI Scan | |
| 2 pages | Quick Start + Web UI Scan only | |

**User's choice:** 3 pages
**Notes:** Tight and focused. Install details live in the how-to guides.

---

## Claude's Discretion

- Exact wording and flow of new pages
- Whether to keep empty Tutorials section or remove from nav
- Admonition placement in new/updated pages
- Depth of auto_source_mode/paper_size coverage in how-to guides

## Deferred Ideas

- API auto-generation (OpenAPI) — future enhancement
- Man pages — out of scope
- Video tutorials — out of scope
- Troubleshooting guide — potential future how-to
