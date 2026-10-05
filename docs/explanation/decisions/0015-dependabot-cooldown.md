# 0015. Every dependency update waits seven days

## Status

Accepted, 2026-10-03.

## Context

Every dependency this repository uses is pinned to an immutable reference: SHA-pinned workflow actions, digest-pinned base images, locked Python dependencies and frozen hook repositories. Dependabot moves those pins deliberately. Without a settling period, a release published from a compromised upstream account today becomes a merge-ready pull request today.

## Decision

Every Dependabot update entry carries a cooldown of at least seven days, for every ecosystem. A test enforces the seven-day floor on every entry, so a new entry added without the block fails. zizmor's audit also reports a missing or shorter cooldown, but its threshold is the tool's default and can change with an upgrade, so the test pins the floor in the repository.

The invariant: no ecosystem's update proposals arrive less than seven days after the upstream release.

## Consequences

- A compromised release has a week to be noticed and pulled upstream before it is proposed here.
- Security fixes also arrive a week later, and an urgent one has to be bumped by hand.

**Alternatives rejected:** no cooldown; relying on zizmor's default threshold alone.
