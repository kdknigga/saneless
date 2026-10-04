# 0007. A feeder pass stops at a fixed page cap

## Status

Accepted, 2026-10-03.

## Context

A feeder pass ends when the scanner reports that its feed is empty. A source sent through the feeder that does not classify as a feeder -- in practice Auto in feeder mode -- may be a platen being rescanned as a feeder. Measured on the SANE test backend, a flatbed source driven through the feeder loop never reports the end of its feed, so the only thing that ends the pass is a cap.

## Decision

Every feeder pass is bounded by a page count per pass, not per job. A source named as a feeder is capped at 500 pages, sized to any plausible hopper. A source sent through the feeder that does not classify as a feeder is capped at 50 pages, which bounds a rescanned platen to minutes while still covering any plausible stack fed through Auto. Both are module constants, not settings, and the scan child applies them to each pass it runs. The free-space check on each page, not the cap, is what protects the disk.

The invariant: no feeder pass can ask the scanner for pages forever.

## Consequences

- A misrouted flatbed costs minutes of rescanning one sheet, not hours.
- A real stack of more than 50 sheets fed through Auto is cut short; naming the feeder source lifts the limit to the larger cap.
- The cap counts pages, so a stack of identical forms scans normally.

**Alternatives rejected:** stopping after a run of identical pages, which needs fuzzy image comparison and would trip on a stack of identical forms; making the cap a configuration key.
