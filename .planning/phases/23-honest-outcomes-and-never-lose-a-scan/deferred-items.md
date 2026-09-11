# Deferred Items — Phase 23

Out-of-scope discoveries logged during execution. These are **not** fixed in the
plan that found them.

## From plan 23-05

### `data-theme="auto"` never engages Pico v2's dark palette

`src/saneless/web/templates/base.html:2` sets `<html lang="en" data-theme="auto">`.
PicoCSS v2 scopes its automatic dark rule to `:root:not([data-theme])` — the
attribute has to be **absent**, not `"auto"` (that spelling was Pico v1). The
CDN build the page links (`@picocss/pico@2/css/pico.min.css`) contains exactly
one dark media block and its selector is
`@media only screen and (prefers-color-scheme:dark){:host(:not([data-theme])),:root:not([data-theme])`.

Consequence: the app renders in the light palette for every user, regardless of
OS preference, and has since Phase 12. `.planning/UI-SPEC.md` claimed in three
places that the preference was honoured; plan 23-05 corrected the written
claims but deliberately did **not** change the attribute.

Why deferred rather than auto-fixed: removing the attribute flips the entire
application's appearance for every user with a dark OS preference. That is a
visible product change with its own contrast-audit obligations across every
component, not a side effect of rendering one new job state. It also touches
`base.html`, which plan 23-05 does not own.

Fix when taken up: delete `data-theme="auto"` from `base.html`, then re-audit
contrast for `.status-done`, `.status-error` and `.status-fallback` against the
dark palette. `.status-fallback`'s amber-600 (`#a16207`) measures ~3.6:1 on
Pico's dark surface, below the 4.5:1 floor, so it would need a lighter amber
under a dark scheme — which in turn means the first dark-mode-specific override
in `app.css`, a discipline `.planning/UI-SPEC.md` currently records as absent.

### PicoCSS colour palette is not linked

`pico.min.css` ships the semantic tokens only. The `--pico-color-*` palette
lives in a separate `pico.colors.css` that `base.html` does not link, so
`.status-fallback`'s `var(--pico-color-amber-600, #a16207)` falls through to
the literal hex. Linking the palette file would make the token resolve with no
CSS change. Not done here: adding a second CDN stylesheet to every page load to
resolve one variable that already has a correct fallback is not a trade worth
making inside this plan.
