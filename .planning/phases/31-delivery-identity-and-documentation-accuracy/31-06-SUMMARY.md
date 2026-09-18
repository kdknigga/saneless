---
phase: 31-delivery-identity-and-documentation-accuracy
plan: 06
subsystem: docs
tags: [documentation, usb, deployment-shapes, trust-model, doc-truth, mkdocs]

# Dependency graph
requires:
  - phase: 31-02
    provides: "the naming guard, so every image reference on the new page names the current owner or the suite fails"
  - phase: 31-05
    provides: "the container facts the new page's compose and run lines depend on -- UID 1000, WORKDIR, the fixed 8080, the read-write config directory mount"
provides:
  - "`docs/getting-started/which-setup.md`: three named deployment shapes with a how-to-tell sentence and copy-paste lines each, first under Getting Started"
  - "One USB rule across all six statement sites: only bare metal enumerates a local scanner; a container always reaches one over the SANE network protocol"
  - "`docs/reference/docker.md` with no device mapping anywhere on it"
  - "The no-login/all-interfaces sentence on both entry surfaces, each linked to material that resolves"
  - "Four new doc-truth contracts, each verified red before it was made green"
affects: [31-09, 31-10]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "A documentation link contract resolves its target against the linking page's own directory, the way mkdocs does, so the 'and it exists' half comes off the filesystem rather than a hard-coded list"
    - "A link anchor is verified against the built site's `id=` attribute, not against the source heading it is assumed to slugify to"

key-files:
  created:
    - docs/getting-started/which-setup.md
  modified:
    - mkdocs.yml
    - docs/getting-started/quick-start.md
    - docs/getting-started/first-cli-scan.md
    - docs/reference/configuration.md
    - docs/reference/docker.md
    - tests/test_deployment_config.py

key-decisions:
  - "Renamed docker.md's surviving `## Network Scanner Access` to `## Scanner Access` and had it say why no device mapping appears on the page, rather than leaving a heading whose qualifier implies an unwritten non-network counterpart"
  - "Kept `install-bare-metal.md:9` and `scanner-host-discovery.md:13` untouched, confirmed by an empty `git diff --stat`, because both were already the one rule"
  - "Asserted the scanner-host caveat on the presence of two words in the table row rather than on a sentence, so a later rewording cannot fail it for a reason unrelated to the contract"

patterns-established:
  - "A new page that reproduces an operator-facing example inherits every contract the corrected examples are already under -- owner, host path form, mount shape, port -- and those are checked as greps before the page is committed"

requirements-completed: [DOCS-04, DOCS-05]

# Metrics
duration: 35min
completed: 2026-09-18
---

# Phase 31 Plan 06: The Setup Chooser and the One USB Rule Summary

**Six statements across five pages that disagreed about whether a container can see a USB scanner now say one thing, the device-mapping section that contradicted the project's own constraint is deleted rather than corrected, and a reader who does not know which of the three deployment shapes they are in finds out on the first page under Getting Started instead of an hour into the wrong one.**

## Performance

- **Duration:** ~35 min
- **Tasks:** 3, committed as 5 (RED/GREEN for the two TDD tasks)
- **Files created:** 1 · **modified:** 6

## Task Commits

| Task | Gate | Commit | What |
|------|------|--------|------|
| 1 | — | `f40bda7` | `which-setup.md` created, first under Getting Started in the nav |
| 2 | RED | `e94ecbc` | 2 failing contracts: the USB-passthrough ban and the scanner-host caveat |
| 2 | GREEN | `3a3198c` | Four statements reconciled; the device-mapping section deleted |
| 3 | RED | `5a2f9e4` | 2 failing contracts: the chooser link and the no-auth note |
| 3 | GREEN | `0e2204c` | Both sentences added, each linked to material that resolves |

## Accomplishments

### The reader chooses the shape before the install, not after

`docs/getting-started/which-setup.md` is 120 lines and is a decision list, not a guide. It opens with the one USB rule, then names three shapes, each with a one-sentence how-to-tell and the exact lines:

| Shape | How to tell | Needs `saned`? |
|-------|-------------|----------------|
| 1 — bare metal, scanner on this machine | `scanimage -L` here lists it, and you are not using Docker | no; leave `scanner.host` empty |
| 2 — container, scanner on the container's own host | the cable goes into the box that runs Docker | yes, on that box; `SANELESS_SCANNER__HOST=host.docker.internal` |
| 3 — container, scanner elsewhere | another machine runs `saned`, or the scanner speaks SANE itself | yes, there; `SANELESS_SCANNER__HOST=192.168.1.50` |

Shapes 2 and 3 each carry both a compose and a `docker run` form, in the tabbed style `quick-start.md` already uses. Because the page is new, every corrected defect from earlier plans in this phase would otherwise have been free to reappear on it; each was checked as a grep before the commit:

| Contract | Source | Measured on the new page |
|----------|--------|--------------------------|
| current owner in every image reference | plan 02's naming guard | `kris-knigga` 0 · `ghcr.io/kdknigga/saneless` 4 |
| `$(pwd)` host paths in `docker run -v` | row 31 | `-v ./` 0 |
| config **directory** mount, never the file | Phase 27 D-09 | `config.toml:/etc/saneless/config.toml` 0 |
| no device mapping | D-48 | `/dev/bus/usb` 0 |

The container facts on that page were taken from what plan 31-05 actually shipped, not from the neighbouring documentation: the port is stated as fixed at 8080 with the host half being the changeable one, the image is stated to run as UID 1000 with the `chown -R 1000:1000 ./config` escape hatch, and the data volume is called part of the minimum.

### One USB rule, and the fourth deployment shape is gone

All six live statements now agree. Two were already correct and were left alone, verified by an empty `git diff --stat`:

| Site | Disposition | Result |
|------|-------------|--------|
| `first-cli-scan.md:9` | rewritten | names the two ways a scanner is reached, and sends an unsure reader to the chooser |
| `first-cli-scan.md:67` | rewritten, container-scoped | says bare metal needs no SANE daemon at all, and that a container needs one even for a scanner on its own host |
| `install-bare-metal.md:9` | untouched | already the bare-metal rule |
| `configuration.md:38` | caveat added | keeps "Empty = local USB", now qualified "on a bare-metal install only" |
| `docker.md`'s device-mapping section | **deleted** | `USB Scanner Access` 0 |
| `scanner-host-discovery.md:13` | untouched | canonical |

`command grep -rn '/dev/bus/usb' docs/ docker-compose.yml Dockerfile` returns nothing, and a contract now holds that true across every page and the compose file.

### Two sentences, and no new page

D-50's rejection of a dedicated trust-model page was honoured: `ls docs/explanation/` still shows three pages, the same three as before. The quick-start prerequisites and the Docker reference image table each gained one sentence naming the two facts — no login, binds `0.0.0.0` — and a link to the reverse-proxy section that already existed. The Docker reference also links to the Web API notes where the posture is written out in full.

Both anchors were verified against the **built site**, not against the source heading they were assumed to slugify to: `site/how-to/deploy-docker-compose/index.html` carries `id="running-behind-a-reverse-proxy"`, and both linking pages emit an `href` ending in exactly that fragment.

## Decisions Made

**`## Network Scanner Access` became `## Scanner Access`.** Deleting the sibling section left a heading whose qualifier implied an unwritten non-network counterpart — precisely the fourth-shape ambiguity D-48 exists to remove. The section now opens by saying that the container never reaches a scanner directly, not even one on its own host, and that this is why no device mapping and no `--privileged` flag appear on the page. Nothing in the repository linked to either old anchor; that was checked across `docs/`, `README.md` and the compose file before the rename.

**The scanner-host caveat is asserted as two words, not a sentence.** `test_scanner_host_documentation_carries_the_container_caveat` requires the `| \`host\` |` row to contain `empty`, `container` and `saned`. A verbatim match would fail on the next rewording for a reason that has nothing to do with the contract, which is the failure mode that teaches people to weaken tests.

**Link contracts resolve against the linking page's directory.** Both DOCS-04/DOCS-05 tests take the "and the target exists" half off the filesystem, resolving `../how-to/deploy-docker-compose.md#running-behind-a-reverse-proxy` the way mkdocs does and stripping the anchor. A page renamed in a later phase therefore fails here rather than becoming a 404, and no hard-coded list of expected pages goes stale.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 — Missing critical functionality] The surviving Docker heading still implied a deleted shape**

- **Found during:** Task 2
- **Issue:** The plan says to check that the page's surrounding headings still read sensibly after the removal. `## Network Scanner Access` did not: with its USB sibling gone, the qualifier "Network" is only meaningful against a non-network alternative that no longer exists and must never come back. A reader would reasonably go looking for the other one.
- **Fix:** Renamed to `## Scanner Access` and had the section state the rule positively — the container reaches every scanner over the network protocol, which is why no device mapping appears. Confirmed nothing links to `#usb-scanner-access` or `#network-scanner-access` anywhere in `docs/`, `README.md` or `docker-compose.yml` before renaming.
- **Files modified:** `docs/reference/docker.md`
- **Commit:** `3a3198c`

**2. [Rule 3 — Blocking] `PIE810` on the section-extraction helper**

- **Found during:** Task 3, at the RED gate
- **Issue:** `line.startswith("## ") or line.startswith("# ")` is a ruff finding, and suppressions are forbidden.
- **Fix:** Collapsed to a single tuple call. No behaviour change; the RED gate was re-confirmed failing afterwards, before the commit.
- **Files modified:** `tests/test_deployment_config.py`
- **Commit:** `5a2f9e4`

---

**Total deviations:** 2 auto-fixed. No architectural decision was needed; no locked decision was revisited; no pre-existing test was weakened.

## Issues Encountered

**An unused constant nearly shipped.** `WHICH_SETUP` was written as a module constant for the chooser test and then not used, because resolving the link target off the filesystem is strictly better than comparing against a hard-coded path — the hard-coded version would pass while the actual link dangled. Ruff does not flag unused module-level constants, so this was caught by reading rather than by a tool. It was removed.

**The environment note about self-inflating greps did not fire this time, but the shape was present.** The USB ban's test constant holds the literal device path, and the test file is not among the files the acceptance grep scans (`docs/` and the compose file), so no rewording was needed. Worth recording that the check was made deliberately rather than the outcome being luck.

## Verification Results

| Check | Result |
|-------|--------|
| `uv run pytest tests/test_deployment_config.py -q` | **74 passed** (70 before, 4 new) |
| `uv run pytest -m "not browser and not sane_hardware" -q` | **3109 passed** |
| `uv run mkdocs build --strict` | exit 0; the only not-in-nav page is the pre-existing `PRD.md` |
| Built-site anchor check | `id="running-behind-a-reverse-proxy"` present; both `href`s match |
| `command grep -rn '/dev/bus/usb' docs/ docker-compose.yml Dockerfile` | no output |
| `uv run ruff check .` / `ruff format --check .` | clean, 63 files formatted |
| `uv run ty check` / `uv run pyrefly check src tests` | all checks passed, 0 errors |
| `uv run zizmor .` | `No findings to report.` |
| `uv run prek run --all-files` | all passed |
| `uv run prek run --stage pre-push --all-files` | all passed, doc-truth harness included |
| Suppressions added | **none** — no `# noqa`, `# type: ignore` or `# zizmor: ignore` in the diff |
| `git diff --name-only 77ffcd2..HEAD` | 7 files, none of them `STATE.md` or `ROADMAP.md` |

### Plan acceptance criteria, measured

`which-setup.md`: 120 lines · `kris-knigga` 0 · `ghcr.io/kdknigga/saneless` 4 · `-v ./` 0 · `/dev/bus/usb` 0 · `config.toml:/etc/saneless/config.toml` 0
`mkdocs.yml`: `which-setup.md` at line 41, `quick-start.md` at line 42 — immediately after
`docker.md`: `USB Scanner Access` 0
`first-cli-scan.md`: `must be connected to the machine running` 0
`configuration.md`: the `| \`host\` |` row carries `Empty`, `container` and `saned`
`install-bare-metal.md` / `scanner-host-discovery.md`: `git diff --stat` empty; lines 9 and 13 re-read and unchanged
`quick-start.md`: `which-setup` 1, inside the Prerequisites section
`docs/explanation/`: three pages, unchanged — no trust-model or security-posture page

Every one of the four new contracts was observed failing against the tree before the change that makes it pass, and each failure named the real offender: the device mapping in `docker.md`, the uncaveated row in `configuration.md`, the missing chooser link, and the missing no-login sentence.

## Known Stubs

None.

## Threat Flags

None. All three registered mitigations are in place:

- **T-31-25** (elevation of privilege) — the device-mapping section is deleted, and `test_no_doc_page_documents_usb_passthrough_into_a_container` holds it deleted across every page and the compose file. Verified red first.
- **T-31-26** (information disclosure) — both entry surfaces now state the posture and link to the remedy; `test_the_no_auth_note_appears_on_both_entry_surfaces` holds both halves, with the link target read off the filesystem.
- **T-31-27** (spoofing via copy-paste blocks) — the new page's blocks were grepped against all three existing bans before the commit, and the full suite including plan 02's naming guard is green.

## User Setup Required

None. All changes are documentation and tests.

## Next Phase Readiness

- **The chooser is a natural landing target for later pages.** `first-cli-scan.md` and `quick-start.md` both link to it now; anything in Phase 32 that has to explain a deployment difference can point there instead of restating it.
- **Watch item:** `mkdocs build --strict` does not validate anchor fragments, only pages. The two reverse-proxy links were verified against the built HTML by hand here. If more anchor links accumulate, a contract that parses `id=` attributes out of `site/` would be cheap; it is not needed for two.
- **`docs/PRD.md` is still built-but-not-in-nav**, unchanged by this plan and owned by D-46 elsewhere in the phase.

## Self-Check: PASSED

All seven files exist on disk — `docs/getting-started/which-setup.md` created, six modified. All five commit hashes resolve in this worktree's history: `f40bda7`, `e94ecbc`, `3a3198c`, `5a2f9e4`, `0e2204c`, each a descendant of the required base `77ffcd2`. `STATE.md` and `ROADMAP.md` were not touched; the orchestrator owns those writes.

---
*Phase: 31-delivery-identity-and-documentation-accuracy*
*Completed: 2026-09-18*
