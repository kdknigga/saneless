# Phase 31: Delivery, Identity, and Documentation Accuracy - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-09-18
**Phase:** 31-delivery-identity-and-documentation-accuracy
**Areas discussed:** Version identity, The naming guard, Workflow hardening, Non-root
container, Container logging & port, The 34-row audit method, Doc surface decisions,
Release rehearsal & proof, Secrets hygiene

**Areas offered and initially declined:** Release rehearsal & proof (picked up later in
the "explore more gray areas" round).

---

## Version identity

### Release version number

| Option | Description | Selected |
|--------|-------------|----------|
| 1.0.0 (recommended) | First installable artifact; v1.0/v2.0 are milestone names | |
| 2.0.0 | Match the milestone name | |
| 0.2.0 | Stay pre-1.0; honest about a young tool, reserves config-format breakage | ✓ |

**User's choice:** 0.2.0 — declined both the recommendation and the milestone-matching
option in favour of the most conservative version number.

### Version plumbing

| Option | Description | Selected |
|--------|-------------|----------|
| click's `package_name` (recommended) | `version_option(package_name=…)`, metadata via importlib | ✓ |
| `__version__` in the package | Literal in `__init__.py` with `dynamic = ["version"]` | |
| You decide | Constraint: one place, test proves agreement | |

### `--version` output

| Option | Description | Selected |
|--------|-------------|----------|
| Just the version (recommended) | click default, one scriptable line | ✓ |
| Version + Python + platform | Diagnostic block for bug reports | |
| Bare version plus a line in doctor | Identify the build in pasted doctor output | |

**Notes:** Rejected options both duplicate `saneless doctor`, whose output Phase 30 pinned.

### Recommended image tag

| Option | Description | Selected |
|--------|-------------|----------|
| Keep `:latest` (recommended) | Least churn; release.yml already pushes it | ✓ |
| Pin the major tag | `:0` / `:1`, needs a metadata-action major pattern | |
| Show both | `:latest` with a commented alternative | |

### Development Status classifier

| Option | Description | Selected |
|--------|-------------|----------|
| Keep `4 - Beta` (recommended) | Already set, coherent with 0.2.0 | |
| `3 - Alpha` | Maximum humility | ✓ |
| `5 - Production/Stable` | Contradicts a 0.x version | |

**User's choice:** 3 - Alpha. Consistent with choosing 0.2.0 over 1.0.0 — the user is
deliberately understating maturity on the public index.

### Pre-release tag shape and the GHCR `latest` tag

| Option | Description | Selected |
|--------|-------------|----------|
| `v0.2.0rc1`, gate `latest` (recommended) | PEP 440 pre-release; metadata-action `enable=` condition | |
| `v0.2.0rc1`, `latest` is fine | Simpler, nobody is pulling yet | |
| You decide | Constraint: a rehearsal never leaves `latest` or `pip install` on a pre-release | ✓ |

### `.gitignore`'s blanket `*.png` (DLVR-09)

| Option | Description | Selected |
|--------|-------------|----------|
| Ignore the tool output dirs (recommended) | `.playwright-mcp/`, screenshot/trace dirs, `site/` | |
| Drop the rule entirely | Rely on existing directory ignores | |
| You decide | Constraint: no `*.png` glob remains | ✓ |

### Maturity prose in README / docs

| Option | Description | Selected |
|--------|-------------|----------|
| No (recommended) | Version and classifier already say it | ✓ |
| One line in the README | Near the install command | |
| One line in getting-started | Next to DOCS-05's trust sentence | |

---

## The naming guard

### Where the guard lives

| Option | Description | Selected |
|--------|-------------|----------|
| A pytest test (recommended) | Runs in CI, prek push stage, and locally; good failure message | ✓ |
| A CI grep step | Literal CI-02 wording; survives a broken suite | |
| A prek local hook | Fastest feedback; CI does not run prek | |
| Test plus CI step | Belt and braces; two definitions of "shipped files" | |

### What it forbids

| Option | Description | Selected |
|--------|-------------|----------|
| The bare string `kris-knigga` (recommended) | One pattern, nothing slips past | ✓ |
| The three enumerated URL forms | Precise, no false positives on prose | |
| You decide | | |

### "Shipped files" definition

| Option | Description | Selected |
|--------|-------------|----------|
| `git ls-files` minus `.planning/` (recommended) | Self-maintaining; `site/` excluded for free | ✓ |
| An explicit path list | Readable, no git subprocess | |
| You decide | | |

### Self-reference

| Option | Description | Selected |
|--------|-------------|----------|
| Build the string at runtime (recommended) | No exempt file exists | ✓ |
| Exempt the guard's own file | Plainly readable source | |
| You decide | | |

**Notes:** All four answers took the recommendation. The phase-30 count-pinning test and
its constant are deleted rather than adjusted.

---

## Workflow hardening

### `docs.yml` trigger bug (`main` vs `master`)

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — fix it properly (recommended) | Trigger, SHA pins, permissions, `uv sync --locked` | ✓ |
| Yes — trigger only | Minimal diff | |
| No — separate concern | Note for the backlog | |

**Notes:** Surfaced during the scout, not from any requirement. The docs deploy workflow
has never run, which is the second reason the README's five docs-site links are dead.

### How zizmor runs

| Option | Description | Selected |
|--------|-------------|----------|
| Dev dep, lint job + prek hook (recommended) | Mirrors ruff/ty/pyrefly wiring exactly | ✓ |
| A separate CI job | Clearer PR signal, new pattern | |
| CI job with SARIF upload | Findings in the Security tab | |

### zizmor severity

| Option | Description | Selected |
|--------|-------------|----------|
| Default persona, blocking (recommended) | Same contract as the other linters | ✓ |
| Pedantic, blocking | Catches more; would want ignores this project forbids | |
| Default persona, advisory | Visibility without a gate | |

### GitHub Pages verification

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — verified live (recommended) | Same "not by reading the file" standard as criterion 2 | ✓ |
| Yes — link-check only | Automatable, permanent, flaky on external links | |
| No — workflow correctness is enough | | |

### `release.yml`'s broken `test` job

| Option | Description | Selected |
|--------|-------------|----------|
| Call `ci.yml` as reusable (recommended) | One definition of "green"; fixes all three bugs | ✓ |
| Fix it in place | Explicit; a second copy that will drift | |
| Drop the test job | A tag can point at an unverified commit | |

**Notes:** Three independent bugs found during the scout — no `libsane-dev`, unlocked
`uv sync`, and a bare `uv run pytest` that collects browser tests with no Chromium.

### TestPyPI rehearsal wiring

| Option | Description | Selected |
|--------|-------------|----------|
| Pre-release tags route to TestPyPI (recommended) | One workflow, the real tag-triggered path | ✓ |
| A manual `workflow_dispatch` input | Flexible; proves slightly less | |
| You decide | | |

### Build provenance

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, both (recommended) | `attestations: true` + `provenance: true` | ✓ |
| PyPI only | | |
| No — out of scope | Not enumerated in DLVR-03 | |

---

## Non-root container

### Container user

| Option | Description | Selected |
|--------|-------------|----------|
| Fixed UID 1000, documented (recommended) | Matches the typical single-user host's `./config` | ✓ |
| High fixed UID (10001) | Stricter isolation; needs a chown on every deployment | |
| PUID/PGID entrypoint | Familiar to self-hosters; still starts as root | |

### Bind-mount permissions for other UIDs

| Option | Description | Selected |
|--------|-------------|----------|
| Docs + commented compose line (recommended) | Stated UID, chown command, `# user:` line | ✓ |
| Lean on the read-only degradation | Phases 26/27/30 already handle it gracefully | |
| Live compose `user:` line | `${UID}` is not populated unless exported | |

### Digest pinning and staleness

| Option | Description | Selected |
|--------|-------------|----------|
| Pin all three + Dependabot docker (recommended) | Both python stages and the uv image | ✓ |
| Pin the python stages only | Leaves uv on a mutable tag | |
| You decide | | |

### Runtime `WORKDIR`

| Option | Description | Selected |
|--------|-------------|----------|
| `/var/lib/saneless` (recommended) | Relative writes land somewhere durable and owned | ✓ |
| A neutral empty dir | Relative writes fail loudly | |
| You decide | | |

### Build context

| Option | Description | Selected |
|--------|-------------|----------|
| Both (recommended) | Allow-list `.dockerignore` and explicit `COPY` lines | ✓ |
| `.dockerignore` only | Exactly DLVR-06's wording, one place to read | |
| You decide | | |

### Test the exclusion?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes (recommended) | Static-text test in the deployment harness | ✓ |
| Yes, and assert the built image | Docker build step in CI | |
| No | Treat as configuration | |

---

## Container logging & port

### How container logs reach `docker logs`

| Option | Description | Selected |
|--------|-------------|----------|
| New `output.log_to_stderr` key (recommended) | Explicit, discoverable, independent of `-v` | |
| Always mirror to stderr in `serve` | No new key; changes bare-metal terminal behaviour | |
| Reuse `-v` in the Dockerfile | Overloads the flag with DEBUG volume | |
| **Other (free text)** | *"What about simply dropping internal log file management in favor of always just logging to stdout and stderr?"* | ✓ |

**User's choice:** free text — proposed dropping the log file entirely.

**Notes:** Claude measured the blast radius before accepting: `logging_config.py`,
three config keys, `_default_log_file()`, the `~`/XDG expansion, `cli.py`'s `attached`
plumbing and `"Full details in <log_file>"`, four doc-table rows, and four test files —
but **zero** references in `checks.py`, so no doctor check depends on it. Claude then
flagged the one real collision: Phase 28's locked "one line, no traceback unless `-v`"
CLI contract holds today *only because* all INFO logging goes to a file, so
unconditional streaming would make `saneless scan` print its whole INFO stream.

### How far to take it (follow-up)

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, stream-only everywhere (recommended) | Delete the file handler and all three keys | |
| Stream-only, keep the keys as an optional extra sink | | |
| Only change the container | Smallest diff, stays inside DLVR-04 | |
| **Other (free text)** | *"we have kind of two modes: operating via cli commands, and running as a long-running service. 'CLI command mode' should write log file, but 'long running service' mode should act like a 12 factor service and stream logs."* | ✓ |

**User's choice:** free text — a mode split rather than a single global answer.

**Notes:** This resolves the Phase 28 collision cleanly and makes DLVR-04 need **no new
config key and no Dockerfile change**, because `CMD ["serve"]` already selects the mode.
The user further specified "one shot cli commands operate as they currently do, only
serve changes" and "nothing should need to change" about the `"Full details in
<log_file>"` line.

### Silently-ignored `log_file` in serve mode

| Option | Description | Selected |
|--------|-------------|----------|
| Log one line at startup (recommended) | Alongside the existing CFG-11 config-source line | |
| Nothing — document it only | Config reference states mode scope | ✓ |
| Serve honours it as an extra sink | | |

**Notes:** Claude raised this as tension with Phase 27's "a silently-ignored key is a bug"
principle; the user accepted the narrow exception.

### Stream target

| Option | Description | Selected |
|--------|-------------|----------|
| stderr (recommended) | Matches StreamHandler default and the `-v` mirror; keeps stdout clean | ✓ |
| stdout | Strictest 12-factor | |
| Split by level | Unix convention; can split a traceback | |

### uvicorn access log

| Option | Description | Selected |
|--------|-------------|----------|
| Keep it on (recommended) | Only per-request record on a no-auth LAN appliance | ✓ |
| Turn it off in serve | Status-strip polling would dominate `docker logs` | |
| Make it a config key | | |

### `-v` in serve

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, unchanged (recommended) | httpx/uvicorn stay at configured level (credential leak) | ✓ |
| You decide | | |

### Port reconciliation

| Option | Description | Selected |
|--------|-------------|----------|
| 8080 fixed in the container (recommended) | Remap on the host with `-p`; `web_port` is bare metal | ✓ |
| Make HEALTHCHECK follow the env var | Works for env, silently not for config.toml | |
| You decide | | |

### `saneless.toml.example`'s `web_port = 8081`

| Option | Description | Selected |
|--------|-------------|----------|
| Remove the key (recommended) | Nothing to keep in sync | |
| Comment it out with correct guidance | Keeps the key discoverable, states the real rule | ✓ |
| You decide | | |

---

## The 34-row audit method

### What counts as proof

| Option | Description | Selected |
|--------|-------------|----------|
| Test where testable, read where not (recommended) | Honest about which rows are permanently defended | ✓ |
| A doc-truth test for every row | Brittle keyword bans for prose rows | |
| Verified read for every row | No permanent defense for this phase's own rows | |

### Where the disposition lives

| Option | Description | Selected |
|--------|-------------|----------|
| `.planning/` audit artifact (recommended) | Verification record, not user documentation | ✓ |
| A section in the review file | Muddies what the reviewers actually found | |
| You decide | | |

### Fix policy for a still-false row

| Option | Description | Selected |
|--------|-------------|----------|
| Doc first; escalate real gaps (recommended) | New behaviour at milestone-end blows the boundary | ✓ |
| Implement where the doc is right | DOCS-01 allows it; unbounded | |
| You decide | | |

### Re-check the "checked and correct" list?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — re-check them (recommended) | Exit codes, routes and retries all changed since September | ✓ |
| Spot-check the at-risk ones | | |
| No — the 34 rows only | | |

### README example verification (DOCS-02)

| Option | Description | Selected |
|--------|-------------|----------|
| Static assertions, not execution (recommended) | Same harness, no hardware, fails locally | ✓ |
| Execute what's executable | A CI step running `--help`/`--version`/`devices` | |
| Both | | |

**Notes:** Claude spot-checked three rows before asking: row 8 and row 28 are corrected,
**row 30 is still live** (`docs/how-to/cli-scripting.md:45` shows `"id": "a1b2c3d4"`
while ids are UUID4). That finding is what made "audit" a real task rather than a
formality.

---

## Doc surface decisions

### `docs/PRD.md`

| Option | Description | Selected |
|--------|-------------|----------|
| Delete it (recommended) | Git keeps the history | |
| Move it to `.planning/` | Survives where planning artifacts live | ✓ |
| Exclude from the build | Stays in the repo, unmaintained | |

**Notes:** Claude found that `mkdocs.yml`'s `nav:` does not list PRD.md, but MkDocs
publishes any `.md` under `docs/` regardless — so it is currently reachable and
search-indexed, not merely absent from the sidebar.

### "Which setup do I have?" placement and shape

| Option | Description | Selected |
|--------|-------------|----------|
| Getting Started, first page (recommended) | Decision list, three setups, exact compose lines | ✓ |
| How-To Guides | Sits with the deployment guides | |
| Explanation | Derives the shapes from the saned boundary | |

### The USB rule

| Option | Description | Selected |
|--------|-------------|----------|
| Container always goes through saned (recommended) | Matches PROJECT.md's constraint; deletes docker.md's passthrough section | ✓ |
| Document passthrough as advanced | Contradicts the project's own constraint | |
| You decide | | |

**Notes:** Claude found the contradiction is **four** statements, not the two the review
row names: `first-cli-scan.md:9,67`, `install-bare-metal.md:9`, `configuration.md:38`,
and `docker.md:156-158` versus `scanner-host-discovery.md:13`.

### Paperless compose service (row 32)

| Option | Description | Selected |
|--------|-------------|----------|
| Replace with a pointer (recommended) | Do not maintain someone else's stack | ✓ |
| Complete it with Redis | One file that works end to end | |
| Both | | |

### Trust-model page (DOCS-05)

| Option | Description | Selected |
|--------|-------------|----------|
| Sentences pointing at existing content (recommended) | Reverse-proxy and no-auth material already exists | ✓ |
| A short Explanation page | One authoritative home for security posture | |
| You decide | | |

---

## Release rehearsal & proof

*Declined in the opening round; picked up in the "explore more gray areas" round after
Claude found that `saneless` is free on both PyPI and TestPyPI, which forces the pending-
publisher flow.*

### Who pushes tags

| Option | Description | Selected |
|--------|-------------|----------|
| You push every tag (recommended) | Extends the standing never-merge rule | ✓ |
| Claude pushes the rehearsal tag only | Faster iteration on a throwaway index | |
| Claude pushes both after confirmation | Collapses approval and action | |

### "A clean machine"

| Option | Description | Selected |
|--------|-------------|----------|
| Throwaway containers (recommended) | `python:3.14-slim` for pip, `docker run --rm` for the image | ✓ |
| A CI job on a fresh runner | Permanent but post-hoc and propagation-flaky | |
| Both | | |

### Publish environments

| Option | Description | Selected |
|--------|-------------|----------|
| Two environments (recommended) | Required by pending-publisher binding; approval rule on `pypi` | ✓ |
| Two environments, no approval rule | | |
| You decide | | |

### Rehearsal cleanup

| Option | Description | Selected |
|--------|-------------|----------|
| Leave it, document it (recommended) | The RC is the evidence criterion 2 asks for | ✓ |
| Yank and delete the tag | Destroys the evidence | |
| You decide | | |

---

## Secrets hygiene

*Raised by Claude after finding that the untracked `saneless.toml` at the repo root holds
a live paperless-ngx token and has been entering the Docker build context on every local
build, because Docker does not respect `.gitignore` and there is no `.dockerignore`.*

### Rotate the token?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, rotate it (recommended) | Cheap; makes the blast-radius question moot | |
| No | Never left the machine; builder stage only; image never published | ✓ |
| You decide | | |

### Where the working config lives

| Option | Description | Selected |
|--------|-------------|----------|
| Move it out of the tree (recommended) | `~/.config/saneless/config.toml`, the XDG path | ✓ |
| Keep it at the root, guarded | | |
| Move it to `./config/` | Mirrors the compose layout, still in the tree | |

### Extra secrets guard?

| Option | Description | Selected |
|--------|-------------|----------|
| No — allow-list and its test are enough (recommended) | Three gates on the actual path | ✓ |
| A token-pattern prek hook | 40 hex chars collides with SHA pins and digests | |
| You decide | | |

---

## Claude's Discretion

- RC tag shape and the mechanism gating the GHCR `latest` tag (constraint: a rehearsal
  never leaves `latest` or `pip install saneless` on a pre-release).
- The specific `.gitignore` paths replacing the blanket `*.png`.
- Plumbing of the logging mode split (how `serve` selects streaming vs the CLI's file
  configuration), under D-34's "one-shot commands are unchanged" as a literal constraint.
- `cli.py:917`'s "the cause is in the log" wording under the new mode split.
- The exact `.dockerignore` allow-list beyond the starting set.
- Commit shape for the rename + guard ordering constraint.
- Audit artifact table shape and filename.

## Deferred Ideas

- A dedicated trust-model / security-posture Explanation page.
- A post-release CI verification job installing the published wheel and pulling the image.
- SARIF upload of zizmor findings to GitHub code scanning.
- A token-pattern prek hook.
- Major-tag image recommendation and a docs-site version selector.
- Executable README-command checks in CI.
- Documenting `--device /dev/bus/usb` container passthrough as an advanced option.
- Maturity prose in the README or getting-started.
- Remaining cosmetic nits and the N-01..N-45 sweep — Phase 32.
- OpenAPI-related doc claims — Phase 33.

No pending todos matched this phase.
