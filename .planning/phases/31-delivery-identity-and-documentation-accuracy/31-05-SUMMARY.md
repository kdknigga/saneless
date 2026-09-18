---
phase: 31-delivery-identity-and-documentation-accuracy
plan: 05
subsystem: infra
tags: [docker, dockerignore, non-root, digest-pinning, dependabot, supply-chain, gitignore, doc-truth]

# Dependency graph
requires:
  - phase: 31-02
    provides: "the identity guard, so every URL touched here already names the current owner"
  - phase: 31-04
    provides: "`package-ecosystem: docker` in dependabot.yml, and the measured fact that its parser sees `FROM` lines only"
provides:
  - "`.dockerignore`: an allow-list whose first meaningful line is `*`, verified sufficient by a real `uv build --wheel`"
  - "A Dockerfile whose three base references are all `image:tag@sha256:...`, with uv on a named `FROM ... AS uv` stage Dependabot can maintain"
  - "An image that starts as UID/GID 1000 with `WORKDIR /var/lib/saneless`, proven by running it"
  - "One container port, 8080, agreed on by the image, the compose file, the example config and every doc page"
  - "`.gitignore` with no blanket `*.png`; `/test-results/` named instead"
  - "Ten new static contracts in the doc-truth harness covering all of the above"
affects: [31-09, 31-10]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Allow-list `.dockerignore` (`*` first, then `!` re-includes) as one of two independent build-context gates, the other being explicit `COPY` lines"
    - "Tool images enter through a named `FROM ... AS <stage>` so a digest pin is Dependabot-visible"
    - "Port expectations derived from `OutputConfig.model_fields[...].default`, and each mapping attributed to its owning image before it is judged"

key-files:
  created:
    - .dockerignore
  modified:
    - Dockerfile
    - .gitignore
    - docker-compose.yml
    - saneless.toml.example
    - docs/reference/docker.md
    - docs/how-to/deploy-docker-compose.md
    - docs/reference/cli-commands.md
    - tests/test_deployment_config.py

key-decisions:
  - "Pinned the uv image to 0.10.3, the series pyproject's `requires = [\"uv_build>=0.10.3,<0.11.0\"]` admits, rather than the 0.12.17 released the same day"
  - "Corrected the VOLUME-ordering rationale against Docker's own reference: the legacy builder discards post-VOLUME changes and BuildKit keeps them, the reverse of what RESEARCH assumed. The required ordering is unchanged"
  - "Scoped the port contract to mappings owned by this project's image, after paperless-ngx's `8000:8000` and a `user: \"1000:1000\"` each surfaced as false positives"
  - "Reworded three Dockerfile comments so the plan's literal grep counts hold, following the precedent Plan 04 set"

patterns-established:
  - "A build-context change is verified by building a probe image that enumerates what the daemon received, not by reading the ignore file"
  - "A contract that is already green on arrival is mutation-tested in the same commit that introduces it, and the mutation result is recorded in the commit message"

requirements-completed: [DLVR-05, DLVR-06, DLVR-07, DLVR-09]

# Metrics
duration: 50min
completed: 2026-09-18
---

# Phase 31 Plan 05: Container Hardening and Build-Context Control Summary

**The image stopped being root-from-mutable-tags-with-the-whole-working-tree-in-it: three digest pins with uv promoted to a Dependabot-visible `FROM` stage, an allow-list build context proven by a probe build to deliver exactly five paths, and a UID 1000 runtime anchored at `WORKDIR /var/lib/saneless` — proven by running the image, not by reading it.**

## Performance

- **Duration:** ~50 min
- **Tasks:** 3, committed as 6 (RED/GREEN per task)
- **Files created:** 1 · **modified:** 8

## Task Commits

| Task | Gate | Commit | What |
|------|------|--------|------|
| 1 | RED | `28c6bc7` | 5 failing contracts for the build context and the PNG glob |
| 1 | GREEN | `a0c0be7` | `.dockerignore` created; `.gitignore` swaps `*.png` for `/test-results/` |
| 2 | RED | `69af882` | 6 failing contracts for the image itself |
| 2 | GREEN | `f7db29a` | Dockerfile restructured; two falsified doc claims corrected |
| 3 | RED | `b8fb2f9` | 4 contracts for the port and the UID escape hatch (3 red, 1 mutation-tested) |
| 3 | GREEN | `0c98070` | Example config, compose file and both Docker pages |

## Accomplishments

### The build context is an allow-list, and that was measured rather than asserted

`.dockerignore` did not exist; the builder ran `COPY . .`. A probe image was built with the new file in place, copying the whole context and listing it. What the daemon delivered:

```
/ctx/LICENSE  /ctx/README.md  /ctx/pyproject.toml  /ctx/src  /ctx/uv.lock
```

A decoy `saneless.toml` and a decoy `config/config.toml` — both holding a token-shaped string — were placed in the worktree first, specifically so their absence from that listing would mean something. Also absent: `tests/`, `.planning/`, `.github/`, `docs/`, `site/`, `.venv/`.

The allow-list was then proven **sufficient**, not merely tight, by copying exactly those five paths into a clean directory and running a real `uv build --wheel`. It succeeded, and the wheel carried `web/templates/`, `web/static/vendor/` and `dist-info/licenses/LICENSE`. Both documented traps were reproduced rather than taken on faith:

| Removed | Result |
|---------|--------|
| `LICENSE` | `project.license-files` glob `LICENSE` did not match any files |
| `README.md` | failed to open file `README.md`: No such file or directory |

### The image runs as UID 1000 — demonstrated, not documented

| Check | Result |
|-------|--------|
| `docker run --rm --entrypoint id <img>` | `uid=1000(saneless) gid=1000(saneless) groups=1000(saneless)` |
| `docker run --rm <img> --version` | `saneless, version 0.2.0` |
| `docker run --rm --entrypoint pwd <img>` | `/var/lib/saneless` |
| `ls -ldn /var/lib/saneless` | `drwxr-xr-x 2 1000 1000` |
| same, on a **fresh named volume** | `drwxr-xr-x 2 1000 1000` — the inheritance D-28 depends on |
| `--entrypoint touch <img> ./relative-write-probe` | exit 0 — a relative write lands in the volume |

The final numbers come from a `--no-cache` rebuild of the committed tree, with `.dockerignore` active.

### All three base references are pinned, and all three are maintainable

Digests were re-resolved at implementation time against the Docker Hub and GHCR v2 APIs rather than copied from RESEARCH. Both matched the recorded values:

| Image | Tag | Digest |
|-------|-----|--------|
| `python:3.14-slim` (x2) | `3.14-slim` | `sha256:cad9a2c8…525ef6` |
| `ghcr.io/astral-sh/uv` | `0.10.3` | `sha256:7a88d4c4…c9d93a3` |

uv moved from `COPY --from=ghcr.io/astral-sh/uv:latest` — a mutable tag pulling an executable into the build, on a line Dependabot's parser skips outright — to `FROM ghcr.io/astral-sh/uv:0.10.3@sha256:… AS uv` plus `COPY --from=uv`.

### The ports all agree, and the operator who is not 1000 has somewhere to look

`saneless.toml.example`'s live `web_port = 8081` was the sharpest edge here: it was a *shipped* setting that disagreed with `EXPOSE` and the healthcheck, so an operator copying the example into a mounted `config.toml` moved the server off the port the probe watches and the container went unhealthy with nothing on screen explaining it. It is now commented, with the bare-metal-only guidance.

## Decisions Made

**uv pinned to 0.10.3, not the 0.12.17 released hours earlier.** `pyproject.toml` declares `requires = ["uv_build>=0.10.3,<0.11.0"]`. A 0.12 image ships a `uv_build` outside that range, and the local toolchain that produced `uv.lock` is 0.10.3. Dependabot will propose the bump; whoever takes it must widen the constraint in the same pull request. That coupling is recorded in a comment above the stage — surfacing it in a reviewable PR is better than it breaking quietly later.

**The `VOLUME` ordering rationale was wrong and is now right.** RESEARCH marked "BuildKit discards post-`VOLUME` changes" as ASSUMED and advised not test-driving it. Docker's own reference says the opposite: *"If any build steps change the data within the volume after it has been declared, those changes will be discarded when using the legacy builder. When using Buildkit, the changes will instead be kept."* The **required ordering does not change** — `mkdir` + `chown` before `VOLUME` is the one arrangement correct under both builders — but the reason had to be stated accurately, in the Dockerfile comment and in the test docstring, before it shipped. The static assertion remains the right check precisely because the answer depends on which builder ran.

**The port contract judges only this project's own mappings.** Written as specified, it flagged paperless-ngx's `"8000:8000"` in the side-by-side compose example, which is correct and none of this contract's business. Tightened once to attribute each mapping to its owning image (via the enclosing compose service's `image:`, or the shell command the `-p` flag sits in with continuations joined) — then a second time, when the new `user: "1000:1000"` line proved to have the exact shape of a port mapping. A quoted `number:number` now counts only under a `ports:` key. Both tightenings were driven by a real false positive, never to make a test pass.

**Three comments were reworded so the plan's grep counts hold literally** — the same issue Plan 04 hit. Comments explaining why `COPY . .`, `COPY --from=ghcr.io/…` and `:latest` were removed each *contained* the string being counted, pushing `grep -c` to 1 where the criterion says 0. Reworded to describe without quoting; every literal count in the plan now holds exactly as written.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Bug] `WORKDIR` falsified two documented claims, in a way the harness could not catch**

- **Found during:** Task 2
- **Issue:** Two pages stated that `saneless auto-profiles` writes `/saneless.toml` in the image *"because the image sets no working directory"* — `docs/how-to/deploy-docker-compose.md` and `docs/reference/cli-commands.md`, each pinned by its own doc-truth test. Adding `WORKDIR /var/lib/saneless` made both false. The tests still passed, because the docs and the tests agreed with each other and both had stopped agreeing with the image. This is exactly the failure mode the harness exists to prevent, and it does not announce itself.
- **Fix:** Corrected both pages to `/var/lib/saneless/saneless.toml`, and said what actually changes: the stray file now lives in the data volume, so it **survives** container recreation and goes on shadowing a `config.toml` added later until deleted. That makes the existing "create `config/config.toml` first" advice matter more, not less. The two tests moved to a shared `AUTO_PROFILES_CONTAINER_PATH` constant so the path is stated once.
- **Not a weakening:** each test asserts the same property against the new truth. Confirmed by construction — the old assertion looked for the substring `` `/saneless.toml` ``, which the new path does not contain.
- **Committed in:** `f7db29a`

**2. [Rule 1 — Bug] The VOLUME-ordering rationale contradicted Docker's reference**

- **Found during:** Task 2, checking Dockerfile semantics through Context7 before writing the file
- **Issue:** A docstring written from RESEARCH claimed BuildKit discards post-`VOLUME` changes. Upstream says the legacy builder discards them and BuildKit keeps them. RESEARCH had flagged this as ASSUMED.
- **Fix:** Rewrote the docstring and the Dockerfile comment to state the real rule and why the required ordering is unchanged. No behaviour change.
- **Committed in:** `f7db29a`

**3. [Rule 3 — Blocking] A revert during mutation-testing took real work with it**

- **Found during:** Task 3
- **Issue:** A mutation test needed `docker-compose.yml` reverted, but the file also held that task's new commented `user:` block. The revert removed both.
- **Fix:** Caught immediately by `grep -c 'user:'` returning 0, and the block was re-applied. `git diff --stat` and a residue grep for `9090|9091|9092` across the tree confirmed the final state carries the work and none of the mutation. Nothing was committed in between.
- **Committed in:** `0c98070`

---

**Total deviations:** 3 auto-fixed (2 bugs, 1 blocking). No architectural decision was needed; no locked decision was revisited.

## Issues Encountered

**The plan's Task 3 spec produced a contract that passes on arrival.** `test_every_documented_container_port_matches_the_model_default` was green against the untouched tree — the live `web_port = 8081` it was written near lives in the example config, which is not an `EXPOSE`, a healthcheck or a `-p` mapping, and is covered by the sibling test instead. Rather than let a green test into a RED commit unexamined, it was mutation-tested: `"8080:9090"` in the compose file, `"8080:9091"` in the how-to and `-p 8080:9092` in the README each produced an offender with `file:line`, while paperless-ngx's `"8000:8000"` stayed unflagged. The result is recorded in `b8fb2f9`'s message. It is a regression guard, and it is not vacuous.

**Podman's OCI format drops `HEALTHCHECK`.** Every local build warned `HEALTHCHECK is not supported for OCI image format and will be ignored`. This is the verification host, not the image: CI builds with `docker/build-push-action`. The healthcheck is therefore asserted statically, and the container's health status was not something this host could demonstrate.

**`uv build` outside the repo needs an explicit interpreter.** The allow-list verification builds from a scratch directory, where `pyenv`'s `python3.14` shim resolves to nothing and uv fails at interpreter discovery. Passing `--python` at the real 3.14.2 path fixes it. A property of the scratch location, not of the allow-list.

**The `rtk` shell hook confirms Plan 04's note, and extends it.** `/usr/bin/git` by absolute path is required, as recorded. Additionally, the isolation guard refuses `docker run --entrypoint sh` and `--entrypoint /bin/sh`. Single-purpose entrypoints (`id`, `pwd`, `ls`, `touch`) pass and were enough for every check here.

## Verification Results

| Check | Result |
|-------|--------|
| `uv run pytest tests/test_deployment_config.py -q` | **70 passed** (60 before, 10 new) |
| `uv run pytest -m "not browser and not sane_hardware" -q` | **3105 passed** |
| `docker build --no-cache` + `--entrypoint id` | `uid=1000(saneless) gid=1000(saneless)` |
| `docker run --rm <img> --version` | `saneless, version 0.2.0` |
| Probe build enumerating the context | exactly `LICENSE README.md pyproject.toml src uv.lock` |
| Real `uv build --wheel` from the allow-list alone | wheel built, templates + vendor + licenses present |
| `uv run mkdocs build --strict` | exit 0 |
| `uv run ruff check .` / `ruff format --check .` | clean, 63 files formatted |
| `uv run ty check` / `uv run pyrefly check src tests` | all checks passed, 0 errors |
| `uv run zizmor .` | `No findings to report.` |
| `uv run prek run --all-files` | all passed |
| `uv run prek run --stage pre-push --all-files` | all passed, doc-truth harness included |
| Suppressions added | **none** — no `# noqa`, `# type: ignore` or `# zizmor: ignore` anywhere |

### Plan acceptance criteria, measured

`.dockerignore`: `^!` lines 5 · first meaningful line `*` · no `!` names tests/config/saneless.toml/.planning
`.gitignore`: `*.png` 0 · `/test-results/` 1 · `check-ignore` names `.gitignore:309` for `test-results/foo.png` · `site/` and `.playwright-mcp/` still ignored by their own entries · `git status --porcelain` clean
`Dockerfile`: `COPY . .` 0 · `COPY --from=ghcr.io/` 0 · `AS uv` 1 · `:latest` 0 · `WORKDIR /var/lib/saneless` 1 · `USER saneless` 1 · `The container runs as root` 0 · `EXPOSE 8080` 1 · `chown` at line 80 < `VOLUME` at line 93
`saneless.toml.example`: `^web_port` 0 · `# web_port = 8080` 1 · `8081` 0
`docker-compose.yml`: `# user:` 1 · live `user:` 0
Docs: `chown -R 1000:1000` present on both Docker pages · the `SANELESS_OUTPUT__WEB_PORT` row carries both `8080` and `-p 8888:8080`
Plan 02's naming guard: still green (it scans every tracked file, including the new `.dockerignore`)

## Known Stubs

None.

## Threat Flags

None. Every change in this plan narrows surface. The three registered mitigations are all in place and each was verified by execution rather than inspection: T-31-07 (two independent gates on the build context — probe build), T-31-02 (three digest pins, all Dependabot-visible — APIs re-queried), T-31-08 (non-root — `--entrypoint id`), T-31-23 (`WORKDIR` — `--entrypoint pwd`), T-31-24 (ownership before `VOLUME` — fresh named volume shows `1000 1000`). T-31-53 is unchanged and remains accepted under D-53.

## User Setup Required

Nothing new. One consequence worth knowing, already covered by D-51: the decoy `saneless.toml` used during verification was deleted, and no real one exists in this worktree. If a real config with a live token still sits at the root of the main checkout, the `.dockerignore` now keeps it out of any build context — but moving it to `~/.config/saneless/config.toml` remains the structural fix, and it is still the user's action.

## Next Phase Readiness

- **`release.yml`'s `publish-docker` job builds the hardened image unchanged** — no workflow edit is needed for this plan's changes to reach a published image.
- **Watch item for Plan 09/10:** CI builds with BuildKit, which this host cannot exercise. The `chown`/`VOLUME` ordering and the `HEALTHCHECK` instruction are both correct by construction and pinned by tests, but the first real BuildKit build is the first time either is exercised by the builder that will ship them.
- **Dependabot will now open PRs against three digest pins.** A uv bump past 0.11 also needs `pyproject.toml`'s `uv_build` constraint widened in the same PR; the Dockerfile says so above the stage.
- DLVR-04 needed no Dockerfile change, exactly as D-24 predicted: `CMD ["serve"]` already selects Plan 03's streaming logging mode.

## Self-Check: PASSED

All nine files exist on disk (`.dockerignore` created; eight modified). All six commit hashes resolve in this worktree's history — `28c6bc7`, `a0c0be7`, `69af882`, `f7db29a`, `b8fb2f9`, `0c98070` — each a descendant of the required base `c570a3b`. `STATE.md` and `ROADMAP.md` were not touched; the orchestrator owns those writes.

---
*Phase: 31-delivery-identity-and-documentation-accuracy*
*Completed: 2026-09-18*
