---
title: "data-theme=\"auto\" never engages PicoCSS v2's dark palette — the app renders light for everyone"
status: pending
priority: P2
source: "discovered during Phase 23 plan 23-05 execution"
created: 2026-09-11
theme: web-ui
---

## Goal

Make the web UI honour the operator's OS dark-mode preference, which it has never done.

## Context

Found while adding the FALLBACK status rendering (Phase 23, plan 23-05). Not fixed there —
correctly deferred, because it flips the entire application's appearance and carries its own
contrast-audit obligations across every component. Full analysis, including the measurement the
fix needs, is in
`.planning/phases/23-honest-outcomes-and-never-lose-a-scan/deferred-items.md`.

`src/saneless/web/templates/base.html:2` sets `<html lang="en" data-theme="auto">`. PicoCSS v2
scopes its automatic dark rule to `:root:not([data-theme])` — the attribute must be **absent**.
`"auto"` was the Pico v1 spelling. The CDN build the page links contains exactly one dark media
block, selector `:host(:not([data-theme])), :root:not([data-theme])`.

**Consequence: every user has seen the light palette regardless of OS preference since Phase 12.**
`.planning/UI-SPEC.md` asserted the opposite in three places; plan 23-05 corrected the written
claims but deliberately left the attribute alone.

Related, smaller: `pico.min.css` ships semantic tokens only. The `--pico-color-*` palette lives in
a separate `pico.colors.css` that `base.html` does not link, so `.status-fallback`'s
`var(--pico-color-amber-600, #a16207)` falls through to the literal hex. Linking the palette would
make the token resolve with no CSS change.

## Acceptance Criteria

- [ ] `data-theme="auto"` removed from `base.html`, and a browser test asserts the dark palette
      actually applies under `prefers-color-scheme: dark`
- [ ] Contrast re-audited for `.status-done`, `.status-error` and `.status-fallback` against the
      dark palette. Note: `.status-fallback`'s amber-600 (`#a16207`) measures ~3.6:1 on Pico's dark
      surface — below the 4.5:1 floor — so it needs a lighter amber under a dark scheme
- [ ] That change introduces the first dark-scheme-specific override in `app.css`; record the
      convention in `.planning/UI-SPEC.md`, which currently records its absence
- [ ] Decide separately whether to link `pico.colors.css`

## Notes

Candidate home: Phase 26 (Worker and Web Robustness) already vendors htmx and PicoCSS for the
no-egress CI sandbox (ROBU-09), which is the natural moment to settle which Pico files ship.
