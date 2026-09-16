---
phase: 30-appliance-layer
plan: 18
subsystem: docs
tags: [compose-template, doc-truth, ui-spec, appl-07, appl-11, appl-12, d-17]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "plan 30-01's is_placeholder_token, the predicate the shipped examples are now tested against"
  - phase: 30-appliance-layer
    provides: "plan 30-02's [web] WebConfig fields, which the config and env references now document"
  - phase: 30-appliance-layer
    provides: "plans 30-11..30-16's routes and RequestRejection.TOKEN_UNSET, which web-api.md now documents"
  - phase: 30-appliance-layer
    provides: "plan 30-08's doctor docs, not duplicated or contradicted here"
provides:
  - "docker-compose.yml with its paperless connection commented out, a consume mount and a live TZ"
  - "the upgrade instruction that makes D-17 land on an existing deployment"
  - "docs/reference/configuration.md [web] section, plus the profile label/description debt"
  - "docs/reference/environment-variables.md SANELESS_WEB__* rows and the TZ section"
  - "docs/reference/web-api.md: five route entries and a table of every RequestRejection"
  - "four derived doc-truth tests that read routes.py, WebConfig and RequestRejection"
  - "the kris-knigga occurrence pin that proves Phase 30 did not perform DLVR-01"
  - ".planning/UI-SPEC.md describing the shipped UI rather than the superseded one"
affects: [30-19, 31-delivery]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "a doc-truth test whose expectation is read from the source (route decorators, model_fields, enum members) rather than hard-coded, so a future addition cannot ship undocumented"
    - "an occurrence-count pin as proof that a phase did NOT touch another phase's lines"
    - "a placeholder-example check that reuses the production predicate, so widening the predicate cannot leave a stale example behind"
    - "keeping one live environment key (TZ) so the compose environment: block stays a valid non-empty mapping while everything else is commented"

key-files:
  created: []
  modified:
    - docker-compose.yml
    - docs/how-to/deploy-docker-compose.md
    - docs/reference/docker.md
    - docs/reference/configuration.md
    - docs/reference/environment-variables.md
    - docs/reference/web-api.md
    - docs/explanation/architecture.md
    - docs/getting-started/first-web-ui-scan.md
    - tests/test_deployment_config.py
    - .planning/UI-SPEC.md
    - .planning/phases/26-worker-and-web-robustness/26-UI-SPEC.md

key-decisions:
  - "the environment: block keeps ONE live entry, TZ, so the mapping stays valid YAML and valid compose; commenting the environment: key itself was the alternative and was not needed"
  - "the consume mount ships COMMENTED, because the root compose file defines no paperless service for a shared volume to reach, and because the mount does nothing until paperless.consume_dir names the same path -- a live mount would have been the kind of half-true template this plan exists to remove"
  - "docker.md's Full Example keeps a live example for TZ and SANELESS_SCANNER__HOST but not for the token, and shows the token in a config.toml block beside it, so the reference agrees with D-17 rather than contradicting the template it documents"
  - "the how-to's upgrade section shows the lines to delete in COMMENTED form, so the shipped-placeholder test does not have to special-case a page that is quoting the bad example on purpose"
  - "web-api.md documents every RequestRejection with its exact sentence, not just TOKEN_UNSET: the test derives the list from the enum, and a partial table would have failed it"

patterns-established:
  - "Pattern: derive a doc-truth expectation from the source symbol (decorator, model field, enum member) so the test fails when the code grows, not when the prose is reworded"
  - "Pattern: pin an occurrence count to prove a scope boundary was respected, and record in the failure message that the fix is to update the constant in the owning phase, not to weaken the assertion"

requirements-completed: [APPL-05, APPL-07, APPL-10, APPL-11, APPL-12]

# Metrics
duration: 58min
completed: 2026-09-16
---

# Phase 30 Plan 18: The Deployment Template and Every Stale Reference Summary

**The shipped compose file no longer overrides the config file's token, sets `TZ` so APPL-12 is true in practice and not just in letter, and seven documents plus the master UI-SPEC now describe what the code actually does -- held there by four doc-truth tests that read the source rather than a hard-coded list.**

## Performance

- **Duration:** 58 min
- **Tasks:** 3 (5 commits — test-then-docs for tasks 1 and 2)
- **Files created:** 0 · **Files modified:** 11

## Accomplishments

### Task 1 — the compose template (`f51e3da`, `b5d758a`)

`docker-compose.yml`'s `environment:` block shipped `SANELESS_PAPERLESS__URL` and `SANELESS_PAPERLESS__TOKEN=changeme` live. Both are now commented, under a note that says plainly what the review's U-01 finding was: a variable set there **overrides** `./config/config.toml` silently, so an operator who fixes the file sees no change and a status strip that stays red.

- The block keeps exactly one live entry, `TZ=America/Chicago`, with a comment saying a container's clock reports UTC without it. That solved the empty-mapping question — `docker compose config` parses it and reports `TZ: America/Chicago` as the only environment key.
- The consume-directory mount is present with a five-line explanation of what it is for and the requirement that `paperless.consume_dir` name the same container path.
- `changeme` is gone from the compose file entirely, and survives in `docker.md` only in two prose sentences that explain it is a *detected* placeholder.
- `deploy-docker-compose.md` gained a **"Remove your own `SANELESS_PAPERLESS__TOKEN` line"** upgrade section. This is the part that makes D-17 reach a deployment that already exists: the template change alone fixes nothing for anybody who copied the old one.
- `kris-knigga` still appears exactly twice, pinned by a test.

### Task 2 — the reference and explanation documents (`87245d1`, `90fbe0f`)

Four derived doc-truth tests, each reading the source rather than a list:

| Test reads | And requires |
|---|---|
| every `@router` decorator in `routes.py` | a matching `` `METHOD /path` `` heading in `web-api.md` |
| every `RequestRejection` member | its name, status code and exact sentence in `web-api.md` |
| every `WebConfig` field | a row in `configuration.md` and a `SANELESS_WEB__*` row in `environment-variables.md` |
| `architecture.md` | that it stops saying "nothing writes any of them yet", and still names `pages_scanned` and `owner_token` |

The route test found the four undocumented routes without being told what they were; the rejection test forced the full twelve-row rejection table rather than a `TOKEN_UNSET` footnote.

`architecture.md`'s claim that six columns existed but nothing wrote or displayed them is replaced with what is true now, per column, including why a NULL `owner_token` has to mean "unowned" and why `outcome` has no `FAILED` member.

`first-web-ui-scan.md` now walks the System status panel, the profile description line, the checkbox tag picker and its filter, the five help lines, the page-counts line, the queue position, the owner-gated flip prompt and the abort confirmation.

### Task 3 — the master UI-SPEC (`3ff951c`)

All nine edits, in order, quoting the phase spec's values. Nothing re-measured, no token invented. The largest were § Copywriting Contract (the `ERROR` row split into category sentence + next step + disclosure, plus three new sub-tables and two rows added to the *existing* Phase 26 tables rather than a new one) and § Interaction Patterns (eight new orchestration rows, the OOB table extended with `#checks-body`, the `hx-disinherit` promise restated).

`26-UI-SPEC.md` S4's heading no longer claims the rule was carried forward unchanged, and the table beneath it now carries the S8 delta.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `architecture.md` said the job database lives under `tmp_dir`**
- **Found during:** Task 2, while editing the paragraph directly below it
- **Issue:** The Job Storage section opened "Job records live in a SQLite database under the configured `tmp_dir`". Since the `data_dir` split it is `data_dir`; `docker.md` and `environment-variables.md` both say so, and the whole point of the split is that `tmp_dir` is disposable. A reader following this sentence would mount the wrong volume and lose their history.
- **Fix:** Corrected to `data_dir`, naming the contrast with `tmp_dir` explicitly.
- **Files modified:** `docs/explanation/architecture.md`
- **Commit:** `90fbe0f`

**2. [Rule 1 - Bug] `environment-variables.md` listed the valid section names without `WEB`**
- **Found during:** Task 2
- **Issue:** The "Unknown variables are rejected" note said the first segment must be `SCANNER`, `PAPERLESS`, `OUTPUT` or `PROFILES`. `web` has been accepted since 30-02. Verified against the real error, which prints `valid sections: scanner, paperless, output, web, profiles`.
- **Fix:** Added `WEB`.
- **Files modified:** `docs/reference/environment-variables.md`
- **Commit:** `90fbe0f`

**3. [Rule 1 - Bug] two more places asserted `Abort scan` has no confirmation**
- **Found during:** Task 3, edit 1
- **Issue:** The plan named § Deliberate design decisions. The same claim also sat in § Copy style guide ("Destructive confirmations are absent") and § Form semantics ("No confirmation dialogs anywhere"). Fixing only the one named would have left the master spec self-contradictory and the acceptance grep arguably green over a false page.
- **Fix:** Both corrected to name the single `hx-confirm` and that it is on the button, not the form.
- **Files modified:** `.planning/UI-SPEC.md`
- **Commit:** `3ff951c`

**4. [Rule 2 - Missing] § Form semantics still described Tags as a native `<select multiple>`**
- **Found during:** Task 3, edit 3
- **Issue:** Edit 3 covers the Components table. § Form semantics carried the same superseded claim in prose, and § Polling had no entry for the strip's 2 s cold-start poll or its "never a steady-state poll" rule.
- **Fix:** Both corrected; the polling rule now records why there is no steady-state poll.
- **Files modified:** `.planning/UI-SPEC.md`
- **Commit:** `3ff951c`

**5. [Rule 2 - Missing] the Design System glyph row and the history-table row were stale**
- **Found during:** Task 3
- **Issue:** The Icon library row did not list the `!` and `·` the phase added (nor U+2298, which predates it), and the Job history table row described a Title cell with no second line.
- **Fix:** Both updated, with the `!`-over-U+26A0 reasoning recorded.
- **Files modified:** `.planning/UI-SPEC.md`
- **Commit:** `3ff951c`

### Accumulated documentation debt absorbed

The orchestrator routed two debts to files this plan owns:

- **`docs/reference/configuration.md` profile `label`/`description` + D-18's escape hatch** (flagged by 30-02 and 30-05) — **done.** Both keys are now rows in the `[profiles.NAME]` table with their caps, and a paragraph under `auto_generated` states the escape hatch: delete the `auto_generated` line to take a profile over. 30-05 covered the how-to side; this is the reference side. `_SECTION_MODELS` needed no change — `web` was already registered there by 30-02, verified.
- **`TZ` in `docker-compose.yml` and `docs/reference/docker.md`** (flagged by 30-10) — **done**, and extended: `environment-variables.md` gained a `TZ` section naming all four surfaces it makes local, including the paperless-ngx document title, which is the one an operator cannot fix after the fact.

### Nothing in this plan's scope was left uncovered

Every item the plan's three task blocks assign is done. Items **not** assigned to this plan, named here so they can be routed:

- **`docs/reference/configuration.md` has no `[web]`-style section for the check registry or the strip** — there is no config for them, so nothing is missing; recorded only so a future reader does not go looking.
- **The `No correspondent` / `-- None --` copy inconsistency stays open** by instruction (edit 9). The note in `.planning/UI-SPEC.md` now says explicitly that Phase 30 did not touch `partials/correspondents.html`, so a future reader knows it was considered and left.
- **`docs/reference/configuration.md`'s Complete Example** gained `[web]` and a `label`/`description` pair on `profiles.default`. It still does not exercise every key, which is deliberate and pre-existing.

## Notes on the acceptance criteria

Two criteria needed judgement rather than a literal reading:

- **`grep -c "changeme" docs/reference/docker.md` is 2, not 0.** The criterion allows this: "or every occurrence is inside a comment explaining that it is a detected placeholder". Both are in the prose that explains exactly that, and the derived `is_placeholder_token` test is the real guard — it scans all three files for a *live* `SANELESS_PAPERLESS__TOKEN=` assignment and fails on any placeholder value.
- **`grep -riE "multi-select" .planning/UI-SPEC.md` matches zero times** — it does now, literally. Two sentences initially survived that said the opposite ("the Tags row was rewritten", "not a `<select multiple>`"), which satisfied the criterion's "in the Tags context" qualifier but not a naive grep. Both were reworded so the plain grep is clean and no reader has to parse intent.

`docker compose -f docker-compose.yml config` exits 0 under podman-compose (the `docker` binary here is podman's shim).

## Verification

| Check | Result |
|---|---|
| `uv run pytest tests/test_deployment_config.py -q` | 47 passed (14 new) |
| `uv run pytest -q` | 2854 passed |
| `docker compose -f docker-compose.yml config` | exits 0 |
| `grep -c "kris-knigga" docker-compose.yml` | 2 — unchanged, and pinned |
| `grep -n "TZ=" docker-compose.yml` | one match, live, with a comment above it |
| `uv run ruff check .` / `ruff format --check .` | clean |
| `uv run ty check` | clean |
| `uv run pyrefly check src tests` | 0 errors |
| STATE.md / ROADMAP.md | untouched (verified by `git diff --name-only`) |

## Known Stubs

None. This plan ships no code paths — every change is a document, a compose comment, or a test that reads one.

## Self-Check: PASSED

All eleven modified files exist and carry the changes described. All five commits are present in `git log`:
`f51e3da`, `b5d758a`, `87245d1`, `90fbe0f`, `3ff951c`.
