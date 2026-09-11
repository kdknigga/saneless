---
phase: 23-honest-outcomes-and-never-lose-a-scan
plan: 09
subsystem: documentation
tags: [docs, docker, data_dir, fallback, connection-status, upgrade-note]
requires: ["23-01", "23-02", "23-04", "23-05", "23-06", "23-07"]
provides:
  - "documentation whose behaviour claims match the code Phase 23 shipped"
  - "a volume table that marks /var/lib/saneless required and /tmp/saneless ephemeral"
  - "an upgrade note for the un-migrated job database, split by deployment shape"
  - "a documented drain procedure for <data_dir>/failed/, including the 20-file WARNING"
affects:
  - docs/explanation/consume-directory-fallback.md
  - docs/reference/web-api.md
  - docs/reference/docker.md
  - docs/how-to/deploy-docker-compose.md
  - docs/reference/environment-variables.md
  - docs/reference/configuration.md
tech-stack:
  added: []
  patterns:
    - "documentation claims verified against shipped source, not against the plan that described it"
key-files:
  created: []
  modified:
    - docs/explanation/consume-directory-fallback.md
    - docs/reference/web-api.md
    - docs/reference/docker.md
    - docs/how-to/deploy-docker-compose.md
    - docs/reference/environment-variables.md
    - docs/reference/configuration.md
decisions:
  - "Documented that a plain consume-directory fallback leaves `warning` NULL, because that is what the code does -- the plan assumed a warning string that plan 23-07 never shipped"
  - "Documented that compose deployments keep their job history across this upgrade, because the old mount put saneless.db at the volume root and the new mount point is that same root"
  - "Added the data volume to the Minimal docker-compose.yml example, since a minimal deployment without it loses preserved scans on every container recreation"
  - "Left docs/PRD.md:245 untouched -- it is a historical product document, not a live reference page"
metrics:
  duration: ~35 min
  completed: 2026-09-11
requirements: [OUTC-02, OUTC-08, OUTC-09, OUTC-04]
---

# Phase 23 Plan 09: Documentation Truth Pass Summary

Six documentation pages rewritten so that every behaviour claim matches the code seven
earlier plans actually shipped -- the `FALLBACK` state, the five connection statuses, the
durable `data_dir`, and the `failed/` directory that is now the last copy of an undelivered
scan.

## What Shipped

### Task 1 -- behaviour claims (commit `7286ccc`)

**`docs/explanation/consume-directory-fallback.md`**

- The paragraph headed "**The job status does not distinguish the two paths.**" is gone
  entirely, heading included. It asserted that a fallback ends in `DONE`, that this is the
  same status a direct upload produces, and that nothing in the job history marks the
  document. All three clauses were false as of plan 23-07.
- Replaced by a new `## How the Job Reports It` section describing the shipped surface,
  each claim checked against source rather than against the plan:
  - `JobState.FALLBACK` (`vocabulary.py:49`), terminal (`TERMINAL_STATES`, `vocabulary.py`),
    labelled `"Saved to folder"` (`state_label`, `vocabulary.py:182`)
  - the web UI status area renders `-> Saved to folder: <title>` with class `status-fallback`
    (`web/templates/partials/status.html:15-16`)
  - the history table applies the same `status-fallback` class
    (`web/templates/partials/history.html:7`)
  - `saneless jobs` prints the humanised label via `state_label(j.state)` (`cli.py:278`)
  - `saneless jobs --json` keeps `"state"` as the raw enum value and carries `"outcome"`
    and `"warning"` (`cli.py:250-254`)
- Added the preservation fact to the `## When It Activates` section: when the upload error
  propagates, the assembled PDF is moved into `failed/` inside the data directory and the
  path is appended to the job's error message, so the error text in the UI names the file
  to go and find. Cross-links to the Docker volumes section.

**`docs/reference/web-api.md`**

- `GET /api/paperless/test` now documents all five 200 bodies. Values are byte-identical to
  the `ConnectionStatus` members (`vocabulary.py:107-111`), which the route serialises
  straight into the body (`web/routes.py:127-128`).
- The `connected` row's condition was corrected from "Successful connection with valid
  token" to "paperless-ngx answered with a 2xx" -- `test_connection` returns `CONNECTED`
  only on `response.is_success` (`paperless.py:498-499`).
- `token_rejected` now names the codes (401 or 403), `unreachable` now names its real
  trigger (`httpx.TransportError`, which covers connect refusal, DNS failure and both
  timeout siblings -- `paperless.py:491`).
- The 502 `{"status": "error"}` row was kept; that path still exists for an unexpected
  exception inside the route.
- Out-of-scope-adjacent fix in the same file: `GET /api/jobs/current/status` listed the
  possible states and was missing `FALLBACK`. Added (see Deviations).

### Task 2 -- the data_dir sweep and the upgrade note (commits `e5854bb`, `793a069`)

**`docs/reference/docker.md`** -- the T-23-42 edit, the highest-value one in the plan:

- The volume table row `| /tmp/saneless | Scan temp files and SQLite database | No (ephemeral OK) |`
  was split in two:
  - `/var/lib/saneless` -- "Durable state: the job database (`saneless.db`) and preserved
    scans (`failed/`)" -- **Required: Yes -- do not treat as disposable**
  - `/tmp/saneless` -- "Scratch space for the scan in progress" -- No (ephemeral OK)
- Prose after the table names the four conditions that actually reach the preservation
  guard and states that the preserved copy is then the only remaining copy.
- New `### Preserved scans in failed/` subsection -- **this is paragraph (f), contractually
  required by T-23-45 and the re-dispositioned T-23-29.** It states:
  - the directory grows and **saneless never deletes, moves or rotates anything in it**
  - one PDF per unrecoverable delivery, two for a duplex scan whose halves were uploaded
    separately (`pipeline.py:446` passes both partial PDFs to the guard)
  - **a WARNING fires once the directory holds 20 or more PDFs**
    (`FAILED_DIR_WARN_THRESHOLD = 20`, `pipeline.py:98`), naming the file count, the total
    size and the path (`_warn_if_failed_dir_growing`, `pipeline.py:211-217`) -- and that
    this is the only signal that the directory as a whole is filling up
  - each file corresponds to a job the UI shows as failed, with that path in its error
    message
  - the drain procedure, and that paperless-ngx checksums documents on consumption so
    re-dropping a preserved PDF into the consume directory is safe
- The `Minimal docker-compose.yml` example gained the data volume (see Deviations).
- A `SANELESS_OUTPUT__DATA_DIR` row was added to the Docker environment-variable table,
  noting the image already sets it.
- The `Full Example` mount line now reads `saneless-data:/var/lib/saneless`.
- Cross-links to the new upgrade section in the compose how-to.

**`docs/how-to/deploy-docker-compose.md`**

- Mount line updated to `saneless-data:/var/lib/saneless`.
- New "Data volume" bullet in Key details explaining it is required, not optional.
- New `### Upgrading from a pre-data_dir release` section, split by deployment shape
  (see Deviations -- the plan's framing was incomplete):
  - saneless migrates nothing; it opens `<data_dir>/saneless.db` and nothing else
  - **compose deployments keep their history** if they reuse the volume, because the old
    mount put `saneless.db` at the volume root and the new mount point is that same root
  - **bare-metal installs start clean**: the old path was `<tmp_dir>/saneless.db`, the new
    default is `~/.local/state/saneless/saneless.db`, and the file must be copied by hand
  - an admonition naming the **`-wal` and `-shm` sidecars** explicitly, because
    `job.py` enables `PRAGMA journal_mode=WAL` and a `.db`-only copy loses committed
    transactions after an unclean shutdown
- New `### What else changed in this release` section covering the three user-visible
  changes an upgrading operator will notice: `failed/` preservation, the `FALLBACK` state
  and the humanised `saneless jobs` labels, and the two new `test_connection` outcomes.

**`docs/reference/environment-variables.md`**

- `SANELESS_OUTPUT__DATA_DIR` -> `output.data_dir`, string, example `/var/lib/saneless`,
  added directly beneath the `TMP_DIR` row.
- A Notes bullet drawing the durable-vs-scratch distinction, giving the
  `~/.local/state/saneless` default and noting the image sets the container value.
- The `TMP_DIR` row needed no description fix: that table has no description column.

**`docs/reference/configuration.md`**

- `tmp_dir`'s description no longer claims to hold the SQLite database; it now reads
  "Scratch space for the scan in progress; its contents are deleted as each scan finishes".
- A `data_dir` row was added beside it, and `data_dir = "/var/lib/saneless"` was added to
  the TOML example directly under `tmp_dir` so the pairing is visible.

## Verification

| Check | Result |
|-------|--------|
| `grep -rn 'saneless-data:/tmp/saneless' .` (excl. `.git`, `site`, `.planning`) | **0 matches** |
| `grep -rc 'saneless-data:/var/lib/saneless' docs/` | 3 in `docker.md`, 3 in `deploy-docker-compose.md` |
| `grep -c 'final status is ` + "`DONE`" + `' consume-directory-fallback.md` | 0 |
| `grep -c 'Saved to folder' consume-directory-fallback.md` | 3 |
| `grep -c 'failed/' consume-directory-fallback.md` | 1 |
| `grep -c 'not_found' web-api.md` / `grep -c 'server_error' web-api.md` | 1 / 1 |
| `grep -c 'ephemeral OK' docker.md` | 1, and the row it sits on is `/tmp/saneless` |
| `grep -c 'SANELESS_OUTPUT__DATA_DIR' environment-variables.md` | 2 (see Deviations) |
| `grep -c 'data_dir' configuration.md` | 2 (table row + TOML line) |
| `grep -c 'wal' deploy-docker-compose.md` | 3 |
| `grep -c 'failed/' docker.md` | 4 |
| `grep -rc 'ghcr.io/kris-knigga/saneless' docs/` | unchanged (docker.md 5, deploy 2, quick-start 1, first-cli-scan 1, scanner-host-discovery 1) |
| `uv run mkdocs build --strict` | exit 0, no link or anchor warnings |
| Generated anchors `#volumes` and `#upgrading-from-a-pre-data_dir-release` | both present in built HTML |
| `uv run prek run --all-files` | all hooks Passed |
| `uv run pyrefly check src tests` | 0 errors |
| `git diff --name-only 0d6c063..HEAD` | six paths, all under `docs/` -- no source file touched |
| `git diff --diff-filter=D` | no deletions |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Task 1(b)'s premise was false: there is no fallback `warning` string**

- **Found during:** Task 1
- **Issue:** The plan instructed "add a sentence saying the warning shown on the job repeats
  this, and keep the two wordings consistent with whatever plan 23-07's `warning` string
  actually says." Plan 23-07 shipped no such string. `run_pipeline` constructs its terminal
  `ScanResult` without a `warning` argument (`pipeline.py:787-792`), so `warning` defaults
  to `None` and `finish_job` writes NULL. The only code path that ever populates `warning`
  is `_handle_duplex_mismatch` (`pipeline.py:475-480`), whose string is
  `"Page count mismatch: N fronts, M backs. Partial PDFs saved."` -- about page counts, not
  about metadata.
- **Fix:** Wrote what the code does. The new text states that the `FALLBACK` state is itself
  the signal, that `warning` is rendered beneath the status line only when set
  (`status.html:17-18`), that the single situation filling it in today is a duplex page-count
  mismatch, and that a plain fallback leaves it null. The metadata consequence stays
  documented under Limitations, where it already lived correctly.
- **Files modified:** `docs/explanation/consume-directory-fallback.md`
- **Commit:** `7286ccc`

**2. [Rule 1 - Bug] The plan's upgrade framing was incomplete for compose deployments**

- **Found during:** Task 2(e)
- **Issue:** The plan and D-14 say job history is not migrated and starts clean. That is true
  of the code, but not of the documented compose deployment. The old code opened
  `Path(settings.output.tmp_dir) / "saneless.db"` = `/tmp/saneless/saneless.db`, and the old
  compose file mounted `saneless-data` **at** `/tmp/saneless` -- so the database sat at the
  volume root. The new mount point `/var/lib/saneless` is the root of that same volume, and
  `data_dir` is `/var/lib/saneless`, so `db_path` resolves to the identical file. A compose
  operator who reuses the volume keeps their history automatically. Telling them otherwise
  would have sent them through a pointless and risky hand-copy.
- **Fix:** Split the upgrade note by deployment shape. Compose: reuse the volume and the
  history is found in place; recreating the volume is what loses it. Bare metal: the default
  genuinely moved from `/tmp/saneless` to `~/.local/state/saneless`, and the file must be
  copied by hand -- with the `-wal`/`-shm` admonition attached to that instruction, where it
  is actually needed.
- **Files modified:** `docs/how-to/deploy-docker-compose.md`
- **Commit:** `e5854bb`

**3. [Rule 2 - Missing critical] The Minimal docker-compose.yml example had no data volume**

- **Found during:** Task 2(a)
- **Issue:** Once the volume table marks `/var/lib/saneless` **Required: Yes**, the minimal
  example on the same page contradicted it by mounting only the config file. An operator
  copying it gets a deployment whose job database and preserved scans live in the container's
  writable layer and vanish on every `docker compose up --force-recreate`. That is the exact
  data loss T-23-42 is about, reintroduced two sections below the fix.
- **Fix:** Added `saneless-data:/var/lib/saneless` and the `volumes:` block to the minimal
  example, with one sentence explaining why it is part of the minimum.
- **Files modified:** `docs/reference/docker.md`
- **Commit:** `e5854bb`

**4. [Rule 2 - Missing critical] `GET /api/jobs/current/status` did not list `FALLBACK`**

- **Found during:** Task 1(d)
- **Issue:** `docs/reference/web-api.md` enumerated the possible job states for the status
  partial and stopped at `ERROR`. `FALLBACK` shipped in plan 23-01 and is rendered by
  `status.html:15`. The omission is in a file this plan owns and is the same class of stale
  claim the plan exists to remove.
- **Fix:** Added `FALLBACK` to the list.
- **Files modified:** `docs/reference/web-api.md`
- **Commit:** `7286ccc`

**5. [Rule 1 - Bug] "the API fails" was too broad as a `failed/` trigger**

- **Found during:** post-Task-2 review of my own prose
- **Issue:** The first draft of the docker.md preservation paragraph listed "the API fails"
  as a trigger. It is not one when a consume directory is configured:
  `upload_document` exhausts its retries, copies to the consume directory and returns
  `delivered_to_api=False` **without raising** (`paperless.py:317-323`), so the preservation
  guard never fires and the job ends `FALLBACK`, not failed. The same draft said "one PDF per
  unrecoverable delivery", which undercounts the duplex path -- `pipeline.py:446` hands the
  guard both partial PDFs.
- **Fix:** Replaced with the four conditions that actually reach the guard (upload fails with
  no consume directory; paperless-ngx rejects the upload outright, which raises on any 4xx
  regardless of consume directory per `paperless.py:298-304`; the consumption task reports a
  failure; the task has not finished when `paperless_task_timeout` expires) and noted the
  two-PDF duplex case.
- **Files modified:** `docs/reference/docker.md`
- **Commit:** `793a069`

### Accepted Deviations

**6. `grep -c 'SANELESS_OUTPUT__DATA_DIR' docs/reference/environment-variables.md` returns 2, not 1**

The criterion's stated purpose is that the row exists. It does. The second occurrence is the
Notes bullet explaining the durable-vs-scratch distinction and the container default, which
that table cannot carry because it has no description column. Removing the bullet to satisfy
a literal count would delete the only place the file explains *why* the two directories are
different settings.

**7. `docs/PRD.md:245` was deliberately left alone**

It still reads `tmp_dir = "/tmp/saneless"` inside a historical product document. Per the
plan's own instruction, it is not a live reference page and is recorded here rather than
silently skipped. It is also not wrong: `tmp_dir` still defaults to `/tmp/saneless`; the PRD
simply predates `data_dir` existing.

**8. The `ghcr.io/kris-knigga/saneless` image name is byte-identical everywhere**

Verified by count across `docs/`: docker.md 5, deploy-docker-compose.md 2, quick-start.md 1,
first-cli-scan.md 1, scanner-host-discovery.md 1. The rename to `kdknigga` is Phase 31's
(M-25..M-31).

---

**Total deviations:** 5 auto-fixed (3 Rule 1 bugs, 2 Rule 2 gaps), 3 accepted.
**Impact on plan:** Deviations 1 and 2 are corrections to the *plan's* description of the
shipped system, found by reading the source rather than the plans -- writing what the plan
said would have put two new false claims into documentation whose whole purpose is to stop
being false. Deviations 3 and 4 close stale claims inside files this plan owns.

## Known Stubs

None. This plan created no code and no placeholder content.

## Threat Flags

None. No new network endpoint, auth path, file access pattern or schema change was
introduced -- this plan modified documentation only, and `git diff --name-only` against the
plan's base commit lists six paths, all under `docs/`.

## Threat Mitigations Delivered

| Threat ID | Mitigation | Where |
|-----------|------------|-------|
| T-23-42 | Volume table row split; data directory marked **Required: Yes**, scratch keeps "No (ephemeral OK)" | `docs/reference/docker.md` volume table |
| T-23-43 | Repository-wide `grep` for the old mount returns zero; every documented mount names `/var/lib/saneless` | all four remaining copies |
| T-23-44 | (accept) The upgrade note names the operator's own database path to the operator; no credential is disclosed -- the paperless token lives in `config.toml`, not the job store | `deploy-docker-compose.md` |
| T-23-45 | Paragraph (f) delivered: growth behaviour, the 20-file WARNING and its content, the never-deletes guarantee, per-file job correspondence, the drain procedure | `docs/reference/docker.md` -> "Preserved scans in `failed/`" |
| T-23-46 | Stated that paperless-ngx detects duplicates by checksum on consumption, so re-dropping a preserved PDF is safe | same section, final bullet |

## Self-Check: PASSED

Files claimed as modified -- all six confirmed present and changed in
`git diff --name-only 0d6c063..HEAD`:

- FOUND: `docs/explanation/consume-directory-fallback.md`
- FOUND: `docs/reference/web-api.md`
- FOUND: `docs/reference/docker.md`
- FOUND: `docs/how-to/deploy-docker-compose.md`
- FOUND: `docs/reference/environment-variables.md`
- FOUND: `docs/reference/configuration.md`

Commits claimed -- all confirmed in `git log`:

- FOUND: `7286ccc` docs(23-09): describe FALLBACK and the five connection statuses
- FOUND: `e5854bb` docs(23-09): document data_dir as durable and sweep the mount path
- FOUND: `793a069` docs(23-09): narrow the failed/ trigger list to what the code does
