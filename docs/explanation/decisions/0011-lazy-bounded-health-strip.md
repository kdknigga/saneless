# 0011. The health strip is filled lazily and its polling is bounded

## Status

Accepted, 2026-10-03.

## Context

The health strip reports on the scanner, paperless-ngx and the local storage. A check against an unplugged scanner host can take minutes, because name resolution has no timeout of its own and a TCP connect hangs until the operating system gives up. A server that waited for the checks would not finish starting, and a strip that ran checks per request would turn every open tab into scanner and paperless-ngx traffic.

## Decision

The checks run on one background refresher thread and the strip only reads its cache. The server never probes on the way up: the refresher starts last, the first render says the checks are running, and the strip polls for itself only while something in flight will change the answer. Every chain of polls is bounded by one of two attempt caps, a short one with nothing in flight and a longer one while a probe is running. A manual refresh collapses into a probe already in flight and is honoured at most once every couple of seconds.

The invariant: no page render and no server start waits on a check, and no tab polls without a bound.

## Consequences

- The server starts and answers at once, even with the scanner host switched off.
- Watching the page generates no scanner or paperless-ngx traffic beyond what the cache allows, however many tabs are open.
- A tab whose chain ran out shows the last answer and points at the Check again button, which is then the one way forward.

**Alternatives rejected:** probing during start-up; running the checks per request; a steady-state poll; ending the poll with an HTTP 286 answer, which would need a change to the tuned htmx configuration.
