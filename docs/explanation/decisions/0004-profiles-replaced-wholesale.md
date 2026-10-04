# 0004. The profiles mapping is replaced wholesale, never mutated

## Status

Accepted, 2026-10-03.

## Context

The scan worker's configured profiles change at most once while the server runs, when start-up profile generation swaps in a generated set. Request threads and the worker thread read them concurrently, and some readers, such as the pipeline on the worker thread, take the mapping once and use it without holding the lock.

## Decision

`saneless.worker` rebinds the profiles in exactly one place, `ScanWorker._set_profiles`, and start-up profile generation is its only caller. It copies the new set into a fresh dict, refuses a set without a `default` profile, and assigns the new dict under the profiles lock.

The invariant: the profiles mapping is replaced wholesale under the lock and never mutated in place, so a reader outside the lock sees either the old or the new mapping.

## Consequences

- A reader that took the old mapping keeps a consistent view of it for as long as it needs, and no locked reader observes a mapping part-way through an update.
- Any new code that edits a profile must build a new mapping and go through the one rebinding path. An in-place `update`, `pop` or item assignment would break unlocked readers silently.
- A set with no `default` is logged and refused, not raised, because the swap runs on the worker thread, where an exception would stop the loop that takes jobs.

**Alternatives rejected:** mutating the existing dict in place under the lock, which unlocked readers could observe half-updated; requiring every reader to take the lock.
