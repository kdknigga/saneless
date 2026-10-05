# 0001. A source saneless does not recognise scans one page

## Status

Accepted, 2026-10-03.

## Context

saneless classifies each SANE source name as a flatbed, a feeder, a duplex feeder, Auto, or unknown, by matching the name against a short list of known words. A feeder source is scanned until the feeder runs dry; a flatbed source yields one page. Scanners name their sources freely, so some name will eventually match none of the known words. Something has to decide what such a source does.

## Decision

An unknown source keeps single-page routing: it is treated as not using the feeder, and a scan from it produces one page. A new feeder name is supported by adding one word to the list of feeder words.

The invariant: an unrecognised source never enters the multi-page feeder loop on the strength of being unrecognised.

## Consequences

- A genuinely new feeder name yields a one-page PDF until someone adds its word. That failure is cheap: the operator sees it at once, and the fix is one entry in a tuple.
- The opposite failure, treating a flatbed as a feeder, rescans the platen until something stops it. It is the expensive one, and it stays impossible for unknown names. Where a source is sent through the feeder anyway, a per-pass page cap bounds it (see [0007](0007-auto-feeder-page-cap.md)).
- This is a settled answer, not an unmade decision.

**Alternatives rejected:** treating every source that is not the flatbed entry as multi-page, which bets on scanners nobody has seen and trades a cheap, visible failure for an expensive one.
