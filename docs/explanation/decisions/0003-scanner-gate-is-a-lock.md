# 0003. The scanner gate is a lock, not an advisory check

## Status

Accepted, 2026-10-03.

## Context

The SANE backend provides no mutual exclusion of its own. On the net backend, a status-strip probe that lands in the middle of a scan is a second request on the control connection the scan is using, which loses a sheet rather than slowing a page. Two things want the scanner outside a scan job: the health checks' scanner probe and the worker's start-up capability read.

## Decision

`saneless.worker` owns one lock, the scanner gate. It is held for the whole of a job's pipeline call and for the whole of the start-up capability read. The scanner check in `saneless.checks` takes it with a non-blocking acquire around the device listing only, and reports the scanner as busy when the acquire fails, instead of waiting.

The check refresher also skips the scanner check, before the gate is tried, while the worker records a job in flight, so the strip says the scanner is not checked during a scan. That skip is only a first layer: a job can start just after it is read, and the gate is what keeps such a probe out of the scan.

The invariant: anything that enters libsane outside a scan holds the gate for as long as it is inside, and a health probe never blocks on it.

## Consequences

- A probe cannot collide with a scan, whatever the timing.
- A probe that loses the race reports that the scanner was busy, without checking it. The row stays green, because "we did not look" is a fact about the probe, not a verdict about the appliance.
- Blocking on the gate would queue a probe behind a scan that may run for minutes and let it enter SANE at some arbitrary later moment, so the only permitted move on failure is to report it.

**Alternatives rejected:** relying only on skipping the scanner check while a job is recorded as running, without the gate, which is racy because a reader can see no job, start listing, and have a job start a microsecond later; a blocking acquire in the health check.
