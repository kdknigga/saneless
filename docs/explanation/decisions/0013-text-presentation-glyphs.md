# 0013. Status glyphs use text-presentation characters only

## Status

Accepted, 2026-10-03.

## Context

The web UI's health strip marks each check's state with a single character. Many Unicode symbols render with emoji presentation on some platforms: a coloured picture at a different size and baseline from the surrounding text, which breaks the strip's alignment and its colour coding. The job status already uses U+2713 and U+2717 for done and error.

## Decision

`saneless.checks` marks a passing check with U+2713, a failing one with U+2717, and a warning with a plain ASCII `!`. Every glyph has text presentation everywhere and needs no variation selector.

The invariant: a status glyph is a character with text presentation on every shipping platform.

## Consequences

- The strip borrows the job status's visual language rather than inventing a second one.
- The warning mark is less distinctive than a warning sign, so the row's colour and its label carry more of the meaning.

**Alternatives rejected:** U+26A0 and U+2757 for the warning, both of which render with emoji presentation on at least one shipping platform.
