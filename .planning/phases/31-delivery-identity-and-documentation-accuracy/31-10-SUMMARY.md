---
phase: 31
plan: 10
status: complete
completed: 2026-09-18
requirements: [DLVR-02, DOCS-01]
---

# Plan 31-10 Summary — clean-machine verification and version restore

## Criterion 2 is satisfied by evidence

Release run https://github.com/kdknigga/saneless/actions/runs/35403846735 — **all five jobs green**:
`ci / lint`, `ci / test`, `ci / browser`, `publish-pypi`, `publish-docker`.

Full command output is recorded in `31-AUDIT.md` § "Release rehearsal evidence". Headlines:

- The **published wheel** installs and runs on a clean `python:3.14-slim`:
  `saneless, version 0.2.0rc2`, 12 `Requires-Dist`, `License-Expression: MIT`, and
  `saneless-0.2.0rc2.dist-info/licenses/LICENSE` present — **DLVR-08 proven in the published
  artifact**, not a local build.
- The **published image** pulls anonymously and runs as `uid=1000(saneless)` with
  `WORKDIR /var/lib/saneless` — DLVR-07 and D-28 confirmed in the artifact, not the Dockerfile.
- Real PyPI returns **404**: the pre-release routing kept the RC off it.
- GHCR carries **only** `0.2.0-rc.2` — no `latest`. D-23's deletion of the unconditional
  `type=raw,value=latest` works; `latest=auto` skips semver pre-releases on its own.

## The bug the rehearsal found

`v0.2.0-rc.1` failed at `publish-docker` with
`ERROR: failed to build: Attestation is not supported for the docker driver.` Plan 31-04 had added
`provenance: true` per D-19 but not `docker/setup-buildx-action`, so the build ran on the default
docker driver, which cannot emit attestations. Fixed in `f2adf5b`.

This is what a rehearsal is for, and it also demonstrated T-31-35 concretely: `publish-pypi` had
already succeeded when `publish-docker` failed, so `0.2.0rc1` was permanently claimed on TestPyPI
and the retry had to be `rc.2`.

## Deviations

1. **Blocker 4 needed no user action.** The GHCR package was already public — verified by a
   credential-free `ghcr.io/token` request returning the tag list with no login. The plan's
   `checkpoint:human-action` for it was therefore satisfied on inspection rather than by asking.
2. **The `pip install` check was decomposed.** Resolving dependencies across both indexes failed
   with `Failed to build 'fastapi'` — TestPyPI hosts a squatted `fastapi 1.0` that outranks real
   PyPI's `0.141.1`, and `--extra-index-url` takes the highest across indexes. That hazard is
   TestPyPI's and cannot occur at the real release. Dependencies were installed from real PyPI and
   the published wheel `--no-deps` from TestPyPI, testing what was genuinely uncertain.
3. **`HEALTHCHECK` was not exercised.** The verification host runs podman, whose OCI output ignores
   it. It remains statically asserted only; CI's `docker/build-push-action` is where it ships.

## Version restored

`pyproject.toml` is back at `0.2.0`; `uv run saneless --version` → `saneless, version 0.2.0`.
The version test passed unmodified throughout.

## The final release is not a phase task

Criterion 2 asks for a pre-release proven end to end, and D-22 keeps the RC as that evidence.
Cutting `v0.2.0` to real PyPI remains the user's to do, when they choose. The `pypi` environment's
required-reviewer gate will fire on that tag; it has not fired yet, and was correctly observed
*not* to gate the hyphenated ones.
