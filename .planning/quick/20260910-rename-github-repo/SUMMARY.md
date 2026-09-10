---
status: complete
slug: rename-github-repo
date: 2026-09-10
---

# Rename GitHub repository scanless -> saneless

## What

The GitHub repository was created as `kdknigga/scanless`, a typo. Everything else
in the project -- the working directory, the Python distribution name, the package
under `src/`, the console script -- is `saneless`. Renamed the repository to match.

## Actions

- `gh repo rename saneless --repo kdknigga/scanless --yes`
- `git remote set-url origin git@github.com:kdknigga/saneless.git`
- Updated 21 `.planning/` files from `kdknigga/scanless` to `kdknigga/saneless`.
- Updated two forward-looking planning references the rename invalidated:
  - `.planning/research/PITFALLS.md` -- the PyPI trusted publisher must be registered
    as (`kdknigga`, `saneless`, `release.yml`). Registering the old name would fail the
    first release with `invalid-publisher` after all tests passed.
  - `.planning/reviews/2026-09-09-code-review.md` -- the prescribed target URLs for the
    docs-link findings now read `kdknigga.github.io/saneless/`.

## Verified after rename

- Repo: `kdknigga/saneless`, PUBLIC, default branch `master`.
- Ruleset `master gate` (22777879): still `active`, still requires `lint` + `test`,
  admin bypass actor intact, `deletion` and `non_fast_forward` intact.
- PR #1: still `OPEN`, `mergedAt: null`, `mergeStateStatus: CLEAN`.
  Now at https://github.com/kdknigga/saneless/pull/1
- `git fetch origin` succeeds against the new URL.
- GitHub redirects the old name: `gh api repos/kdknigga/scanless` resolves to
  `kdknigga/saneless`.

## Blast radius

Zero tracked source files referenced `scanless` -- confirmed by `git grep -i scanless`
against both the working tree and the published `autodev-filtered` branch. No commit,
no push, and no PR update was needed for the rename itself.

## Found but NOT fixed (pre-existing, out of scope)

`README.md:72-77` and `mkdocs.yml:3-4` point at **`kris-knigga`**, a wrong owner slug
(actual: `kdknigga`). This predates the rename and was already recorded in
`.planning/reviews/2026-09-09-code-review.md` rows 739-745 as a docs-link defect for
DOCS-01. The rename changed what the correct value is, so the review rows were updated
to prescribe `kdknigga.github.io/saneless/`, but the tracked files themselves were left
alone -- they are published files whose fix belongs in a PR, not in a quick task.
