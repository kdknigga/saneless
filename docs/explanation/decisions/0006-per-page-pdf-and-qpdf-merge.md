# 0006. The PDF is built one page at a time and merged with qpdf

## Status

Accepted, 2026-10-03.

## Context

Each scanned page is spooled to disk as a PNG, and that PNG is embedded losslessly as the PDF's page content. img2pdf's `convert` reads every input fully into memory and finalises the whole document before writing a byte. Measured on 48 synthetic A4 300 DPI colour pages, one `convert` whose bytes were then written out peaked at 1395 MB, and the same call with `outputstream=` at 787 MB, both still linear in page count.

## Decision

`saneless.pdf` converts each page with its own `convert` call into its own single-page PDF in a scratch directory, and merges those with qpdf through pikepdf, copying each page's streams lazily. The first single-page PDF carries the document metadata and is qpdf's primary input.

The invariant: no step of assembly holds more than one page's image data at a time, so memory stays flat as page count grows.

## Consequences

- Measured flat at 131 MB for both 12 and 48 pages, with byte-identical image streams, the same page boxes and the same total wall clock.
- The merge holds the GIL for roughly 12 ms per page, so a 500-page job stalls its thread for about 6 s during assembly. That thread is the worker's, already busy for the whole scan, so the visible effect is one briefly frozen status poll.
- The shape is not a tuning knob: it is what keeps the memory bound true end to end.

**Alternatives rejected:** one `convert` call over every page; one `convert` call with `outputstream=`, which still finalises the whole document in memory.
