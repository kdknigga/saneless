# 0005. The backend hands each page to a declared page sink

## Status

Accepted, 2026-10-03.

## Context

A scan backend acquires pages one at a time, and a job can run to hundreds of pages. A backend that accumulated decoded pages put peak memory at 1395 MB for 48 pages. The backend also measures two facts the pipeline needs: the resolution the device settled on, and how many fed sheets failed their integrity checks. Where a page lands and what is measured about it are pipeline concerns, not backend ones.

## Decision

The backend's scan call takes a page sink, an abstract base class the pipeline implements, and returns a completed batch. For each accepted page the backend calls the sink exactly once, after the page passed its checks and was cropped, and retains nothing afterwards. The batch carries the records the sink returned, the resolution the device used, and the count of rejected sheets.

The invariant: the backend never holds more than one decoded page, and it never decides where a page is stored.

## Consequences

- Peak memory is a small constant number of decoded pages, independent of page count.
- Both type checkers see the sink's contract, so a wrong argument or return value is caught statically.
- The sink is an abstract base class, not a protocol, because it is a seam this project implements rather than a shape it describes.

**Alternatives rejected:** going back to a generator, which can only hand back images and loses the resolution and rejected-sheet count to the `list()` every caller wraps it in; a bare callback with no declared contract, which neither type checker can verify.
