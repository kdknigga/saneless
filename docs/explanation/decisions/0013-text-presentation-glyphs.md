# 0013. Health-strip glyphs use text-presentation characters only

## Status

Accepted, 2026-10-03.

## Context

The web UI's health strip marks each check's state with a single character. Many Unicode symbols render with emoji presentation on some platforms: a coloured picture at a different size and baseline from the surrounding text, which breaks the strip's alignment and its colour coding. The job status already uses U+2713 and U+2717 for done and error.

## Decision

`saneless.checks` marks a passing check with U+2713, a failing one with U+2717, and a warning with a plain ASCII `!`. Every glyph has text presentation everywhere and needs no variation selector.

The invariant: a health-strip state glyph is a character with text presentation on every shipping platform. The decision covers the strip only.

## Consequences

- The strip borrows the job status's visual language rather than inventing a second one.
- The warning mark is less distinctive than a warning sign, so the row's colour and its label carry more of the meaning.
- The rest of the UI still uses U+26A0, with no variation selector: the job status marks a done-with-warning scan with it (`saneless.vocabulary`), and the amber fallback lines in the templates and `saneless.web.status_view` start with it. Those are outside this decision, and a strip glyph is never copied from them.

**Alternatives rejected:** U+26A0 and U+2757 for the warning, both of which render with emoji presentation on at least one shipping platform.
