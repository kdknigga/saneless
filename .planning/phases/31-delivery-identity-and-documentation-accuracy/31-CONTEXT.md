# Phase 31: Delivery, Identity, and Documentation Accuracy - Context

**Gathered:** 2026-09-18
**Status:** Ready for planning

<domain>
## Phase Boundary

The project stops lying about who it is and how it ships. Three strands, seventeen
requirements:

1. **Identity** — every `kris-knigga` reference becomes `kdknigga`, behind a guard that
   keeps it that way (DLVR-01, CI-02).
2. **Delivery** — a release workflow proven by actually installing the artifacts it
   produces, plus container hardening and supply-chain hygiene (DLVR-02..DLVR-10).
3. **Documentation** — this milestone's final audit that all 34 rows of review section 8
   are true, plus the doc surfaces the audit exposes as missing or wrong
   (DOCS-01..DOCS-06).

**This phase clarifies HOW to ship what exists. It does not add product capability.**

The one place it touches behaviour is `serve`'s logging mode (D-34..D-40), and that is
in service of DLVR-04 — a container whose logs are written and then thrown away.

**Explicitly NOT in this phase:**
- Any new scanner, web, or pipeline capability. A documentation row that turns out to
  need new behaviour rather than a corrected sentence is escalated, not implemented
  (D-43).
- The Phase 32 suite-hygiene and N-01..N-45 sweep.
- The OpenAPI removal (Phase 33, independent).

</domain>

<decisions>
## Implementation Decisions

### Carried forward (already decided, do not re-litigate)

- **Phase 27 D-09 / Phase 30:** the recommended mount is read-write
  `./config:/etc/saneless`. `:ro` was explicitly rejected, and Phase 27's atomic config
  rewrite (`os.replace` beside the file) depends on the *directory* being mounted. The
  non-root work (D-25, D-26) must not break this.
- **Phase 30, deferred explicitly:** "Fixing the `kris-knigga` image reference in
  `docker-compose.yml` — Phase 31, DLVR-01, which has a CI grep guard. This phase edits
  the same file without touching that line." Phase 30 added
  `test_phase_30_does_not_perform_the_dlvr_01_rename` and `COMPOSE_KRIS_KNIGGA_COUNT`
  (`tests/test_deployment_config.py:781, 921`) to prove it. **This phase deletes both.**
  Per that test's own comment, the fix when it fires is to update it in *this* phase, not
  to weaken it — here that means removal, since the constant's whole purpose is gone.
- **Phase 30 / REQUIREMENTS out-of-scope, still binding:** the container `HEALTHCHECK`
  keeps calling `/health`, never `doctor` — doctor does network I/O and would flap.
- **Phase 28 D-06/D-07/D-08:** the CLI prints one line and exits with one table
  (0/1/2/3/4/5/130), no traceback unless `-v`, and `"Full details in <log_file>"` is
  appended only when the file handler really attached. This contract is **preserved
  untouched** by the logging mode split (D-34).
- **Phase 27 CFG-04 / M-21 (orchestrator resolution 5):** `-v` means "saneless's own
  loggers at DEBUG"; uvicorn and httpx stay at the configured level, because httpx at
  DEBUG prints the Paperless `Authorization` header (T-27-23). Unchanged (D-38).
- **Phase 26 D-16..D-18 / Phase 27 D-08:** a read-only or bind-mount-broken config already
  keeps generated profiles in memory, logs it, and (Phase 30) shows a read-only-mount
  notice in the UI. D-26 leans on this rather than duplicating it.
- **Phase 20:** `ci.yml` is SHA-pinned with full version comments
  (`# v7.0.1` style), jobs are `lint` / `test` / `browser`, and the naming grep guard was
  deliberately deferred to this phase "where the rename it guards actually lands — adding
  it here would make CI red from its first run."
- **Established doc-truth pattern:** `tests/test_deployment_config.py` holds plain-text
  assertions over `docker-compose.yml`, `saneless.toml.example` and every page under
  `docs/`, with expectations derived from source (route decorators, `WebConfig` fields,
  `ExitCode`, `RequestRejection`). Phases 27, 28, 29 and 30 each added their rows there.
  This phase adds to that harness rather than inventing a new one.

---

### Version and package identity (DLVR-08, DLVR-10)

- **D-01:** The released version is **`0.2.0`**. `v1.0` and `v2.0` are GSD milestone
  names, not package versions; `0.1.0` was never published anywhere. Staying pre-1.0
  reserves the right to break the config format.
- **D-02:** `Development Status :: 3 - Alpha` replaces `4 - Beta` in the classifiers.
  Maximum humility, coherent with a 0.x version.
- **D-03:** `saneless --version` uses **`@click.version_option(package_name="saneless")`**
  — the version comes from installed package metadata via `importlib.metadata`, so
  `pyproject.toml` is the single source and drift is impossible. This is the exact fix
  review section 8 prescribes for row 15.
- **D-04:** `--version` prints **just the version** (click's default
  `saneless, version 0.2.0`). No Python/platform block — `saneless doctor` already exists
  for diagnostics, and Phase 30 pinned its output with tests.
- **D-05:** PEP 639 migration: `license = "MIT"` (SPDX string) plus
  `license-files = ["LICENSE"]`, and the `License :: OSI Approved :: MIT License`
  classifier is **removed in the same change** — build tools error when a
  `License-Expression` ships alongside a license classifier. The current
  `license = {text = "MIT"}` table form is the deprecated one.
- **D-06:** Docs and the compose example keep recommending **`:latest`**. Every page
  already says it, `release.yml` already pushes it, and self-hosters running an appliance
  want the current build. No major-tag recommendation.
- **D-07:** **No maturity prose is added** to the README or the docs. The version number
  and the classifier say it. This phase removes claims; it does not add hedges.

### The naming guard (DLVR-01, CI-02)

- **D-08:** The guard is a **pytest test** in the existing doc-truth harness. It then runs
  in CI (satisfying CI-02's "CI fails if one reappears"), in prek's push stage, and on a
  plain `uv run pytest` — three enforcement points, one implementation, and a failure
  message that can name the offending `file:line`. No separate CI grep step.
- **D-09:** It forbids the **bare string `kris-knigga`**, not just the three enumerated
  URL forms. One pattern, nothing can slip past it — a new URL shape, a prose mention, an
  attribution. (`authors = [{name = "Kris Knigga", …}]` in `pyproject.toml` uses a space,
  not a hyphen, so there is no collision.)
- **D-10:** "Shipped files" is **`git ls-files` minus `.planning/`**. Derived from git, so
  a newly added file is covered without anyone remembering. `site/` is already gitignored
  (`.gitignore:207`), so the requirement's second exclusion comes for free.
- **D-11:** The guard **assembles the forbidden string at runtime** so it never appears
  literally in its own source. No file is exempt, therefore there is no exempt file where
  a real reference could hide. It needs a comment saying why it is written that way.
- **D-12:** `COMPOSE_KRIS_KNIGGA_COUNT` and
  `test_phase_30_does_not_perform_the_dlvr_01_rename` are **deleted**, not adjusted.

### Workflows and release (DLVR-02, DLVR-03)

- **D-13:** **`docs.yml` is fixed properly, and it is in scope.** Its trigger is
  `branches: [main]` while this repo's branch is `master` — the docs deploy workflow has
  never run, which is the second reason all five `kris-knigga.github.io` README links are
  dead. Fix the trigger, SHA-pin its actions, add a `permissions:` block, and replace the
  bare `pip install mkdocs-material` with `uv sync --locked` + `uv run mkdocs` so the site
  builds with the version `uv.lock` pins.
- **D-14:** **zizmor runs as a dev dependency inside the existing `lint` job, plus a
  `language: system` prek hook** in the existing `repo: local` block — exactly how
  `ruff`/`ty`/`pyrefly` are already wired. No new CI job, no new pattern, version pinned
  by `uv.lock`, fails locally before push.
- **D-15:** zizmor runs at the **default persona and any finding fails the build** — the
  same contract as ruff and the type checkers. With three small workflows the finding
  count is bounded. Findings are fixed properly, not suppressed with
  `# zizmor: ignore` (project standards forbid suppression).
- **D-16:** **`release.yml`'s `test` job becomes `uses: ./.github/workflows/ci.yml`**
  (with `ci.yml` gaining a `workflow_call` trigger). Its current job is broken three ways
  independently — no `libsane-dev` so `uv sync` cannot build `python-sane`, unlocked
  `uv sync`, and a bare `uv run pytest` that collects the browser tests with no Chromium.
  One definition of "green", so the release gate can never drift from the CI gate.
- **D-17:** **Pre-release tags route to TestPyPI; final tags route to PyPI**, in one
  workflow with one code path, so the rehearsal exercises the real tag-triggered path.
  Requires `repository-url: https://test.pypi.org/legacy/` on the TestPyPI branch (the
  OIDC audience differs: `pypi` vs `testpypi`).
- **D-18:** **Two GitHub environments**, `testpypi` and `pypi`, each bound to its own
  pending publisher. This is required by the pending-publisher binding, not a style
  choice: a publisher binds to a single environment name. A **manual approval rule sits on
  the `pypi` environment**, so a real publish needs a human click even if a tag lands by
  accident — a PyPI version number can never be reused.
- **D-19:** **Both artifacts carry provenance**: `attestations: true` on
  `gh-action-pypi-publish` and `provenance: true` on `docker/build-push-action`. Two lines,
  both using the OIDC identity the workflow already has.
- **D-20:** **The user pushes every tag** — rehearsal and real. Claude prepares everything,
  verifies CI is green, and hands over the exact `git tag` / `git push` commands. This
  extends the standing rule that Claude pushes branches and opens PRs but never merges; a
  tag that triggers a publish to a public index is further outward-facing than a merge.
- **D-21:** Criterion 2's "clean machine" means **throwaway containers**:
  `docker run --rm python:3.14-slim` for the verifying `pip install` (installing
  `libsane-dev` first, since `python-sane` compiles), and `docker run --rm` on the pulled
  image for the `docker pull` half. Reproducible, repeatable, and the output is shown —
  which is the "not by reading the workflow file" standard the criterion sets. No
  post-release CI verification job.
- **D-22:** **The rehearsal's artifacts stay.** The RC remains on TestPyPI and the RC tag
  remains in history as the evidence criterion 2 asks for. The audit artifact records the
  tag, the workflow run URL, and the verification output.
- **D-23:** The RC tag shape and the `latest`-tag gating are **Claude's discretion**, under
  a hard constraint: **a rehearsal must never leave GHCR's `latest`, or a plain
  `pip install saneless`, pointing at a pre-release.** `release.yml` currently has an
  unconditional `type=raw,value=latest`, which would do exactly that.

### Container image (DLVR-05, DLVR-06, DLVR-07)

- **D-24 (see also Logging):** DLVR-04 needs **no Dockerfile change and no new config
  key** — `CMD ["serve"]` already selects the streaming mode decided below.
- **D-25:** The container runs as a **fixed UID/GID 1000**, documented. On the
  overwhelmingly common single-user Linux host the operator's own `./config` directory is
  already `1000:1000`, so the read-write bind mount Phase 27 D-09 requires just works with
  no `chown`. A high UID (10001) was rejected: it can never match, so every deployment
  would need a fix-up on the appliance's happy path. A PUID/PGID entrypoint was rejected:
  it still starts as root, which is the thing DLVR-07 exists to stop.
- **D-26:** For operators whose UID is not 1000: **documentation plus a commented compose
  line.** The Docker reference and the deploy how-to state the UID and give the
  `chown -R 1000:1000 ./config` command; `docker-compose.yml` carries a commented
  `# user: "${UID}:${GID}"` with one sentence on when to uncomment. A live
  `user: "${UID:-1000}:…"` line was rejected — compose does not populate `${UID}` unless
  it is exported, so it would silently fall back.
- **D-27:** **All three base references are digest-pinned** — both `python:3.14-slim`
  stages and `ghcr.io/astral-sh/uv` (currently `:latest`, a mutable tag pulling an
  executable into the build) — each with a version comment matching `ci.yml`'s convention.
  **`package-ecosystem: "docker"` is added to `dependabot.yml`** so the pins get PRs
  instead of rotting; Dependabot updates `image:tag@sha256:…` digests in a Dockerfile.
- **D-28:** The runtime stage gets **`WORKDIR /var/lib/saneless`** — the durable data dir
  the image already declares as a `VOLUME` and already points
  `SANELESS_OUTPUT__DATA_DIR` at. A stray relative write lands somewhere durable and
  owned by the app user rather than in `/` (N-26). Note: the named volume inherits
  ownership from the image's directory at that path on first use, so `mkdir` + `chown`
  before `VOLUME` is what makes the non-root user work there. Bind mounts get no such
  treatment — hence D-26.
- **D-29:** **Both** an allow-list `.dockerignore` (`*` then `!src/`, `!pyproject.toml`,
  `!uv.lock`, `!README.md`, `!LICENSE`) **and explicit `COPY` lines** replacing
  `COPY . .`. Two independent gates, so a `.dockerignore` mistake alone cannot leak a real
  config into a layer. **Trap:** `pyproject.toml`'s `readme = "README.md"` and the new
  PEP 639 `license-files` both make the build fail if those files are excluded.
- **D-30:** The build-context exclusion is **pinned by a static-text test** in the
  deployment harness: `.dockerignore` starts with `*` and never re-includes a config,
  secret, `.planning/` or tests path. Nothing else in CI would notice an allow-list being
  weakened. No docker-build-and-inspect step in CI.
- **D-31:** **8080 is fixed inside the container.** The docs state plainly that
  `web_port` is a bare-metal setting and that you remap on the host with `-p 8888:8080`.
  No shell-form `HEALTHCHECK` env expansion — it would work for the env var but silently
  not for a `web_port` in `config.toml`, replacing one false claim with a half-true one.
- **D-32:** `saneless.toml.example`'s `web_port = 8081` is **commented out with correct
  guidance** (`# web_port = 8080  # bare metal only; in Docker remap with -p`), not left
  live. That line is row 21's trap: a shipped example setting a port that disagrees with
  `EXPOSE` and `HEALTHCHECK`.
- **D-33:** `.gitignore:307`'s blanket `*.png` is removed (DLVR-09). Which specific paths
  replace it is **Claude's discretion**, under the constraint that no `*.png` glob remains
  and that whatever actually writes PNGs in this repo is enumerated and ignored by path.

### Logging modes (DLVR-04)

**This is the one behaviour change in the phase.** saneless has two operating shapes and
they get different logging, because they have different log consumers.

- **D-34:** **One-shot CLI commands (`scan`, `devices`, `jobs`, `doctor`,
  `auto-profiles`) are unchanged.** Rotating file handler, the `attached` return,
  `ctx.obj["log_file"]`, `"Full details in <log_file>"`, the `-v` stderr mirror, the
  traceback-free fallback. Phase 28's one-line CLI contract and its doc-truth tests are
  untouched. **No changes here at all.**
- **D-35:** **`serve` behaves as a 12-factor service:** it streams logs and writes **no
  file**. The `log_file` / `log_max_bytes` / `log_backup_count` config keys all survive
  (they govern CLI mode) and simply do not apply in service mode.
- **D-36:** The stream is **stderr**, matching Python's `StreamHandler` default and the
  existing `-v` mirror. This keeps stdout as a clean channel carrying only the
  `Serving on http://host:port` line that `click.echo` already prints. Docker's json-file
  driver captures both, so `docker logs` is unaffected either way.
- **D-37:** **uvicorn's access log stays on** (`access_log=True`, unchanged). `serve`
  already passes `log_config=None`, which means uvicorn attaches no handlers of its own
  and its records propagate to saneless's root handlers — so access lines follow the
  stream with zero extra code. For a LAN appliance with no auth, this is the only
  per-request record, and it becomes genuinely useful once visible.
- **D-38:** **`-v` is unchanged in `serve`**: saneless's own loggers at DEBUG, uvicorn and
  httpx at the configured level. Hard constraint (credential leak, T-27-23).
- **D-39:** An operator who sets `log_file` and runs `serve` gets **no runtime warning** —
  the config reference documents which keys apply to which mode, and that is enough. (This
  is a deliberate, narrow exception to Phase 27's "a silently-ignored key is a bug"
  instinct; the mode split is documented rather than warned about.)
- **D-40:** `serve` does **not** honour `log_file` as an additional sink. It is a service;
  the platform owns retention.

### The documentation audit (DOCS-01, DOCS-02)

- **D-41:** Proof per row is **a doc-truth test where the claim is statically checkable,
  a recorded verified read where it is not.** Rows whose truth is a string present/absent
  or a documented value matching a source constant become tests in the existing harness.
  Rows that need a human to read a paragraph (e.g. row 26's responsiveness overclaim) get
  a recorded verification with `file:line`, because forcing them into tests produces
  brittle keyword bans that get weakened later. The artifact must be honest about which
  rows are permanently defended and which are point-in-time.
- **D-42:** The row-by-row disposition lives in a **`.planning/` audit artifact** in this
  phase directory: row number, claim, disposition, evidence (test name or `file:line`).
  It is a verification record, not user documentation. The original review file is **not**
  edited in place — annotating it would muddy what the reviewers actually found.
- **D-43:** When a row is still false, **correct the sentence to match shipped
  behaviour.** If a row exposes a genuine behaviour gap rather than a stale sentence,
  **stop and escalate to the user** rather than implementing. New behaviour at the end of
  a hardening milestone is how a phase blows its boundary. DOCS-01's "or the behaviour is
  implemented" clause is not a licence to build.
- **D-44:** The audit **also re-checks section 8's "claims that were checked and are
  correct" list** (~20 claims, verified 2026-09-09). Twelve phases have changed behaviour
  since, and several of those claims are squarely in the blast radius: "`scan` exit codes
  1, 2 and 3" (Phase 28 replaced the table with 0/1/2/3/4/5/130), "all eleven routes and
  methods in `web-api.md`" (Phases 26 and 30 added routes), "three upload attempts with
  `2**attempt` backoff" (Phase 28 reworked retries). A claim that was true in September
  and is false now is exactly what this audit exists to catch.
- **D-45:** DOCS-02's README examples are pinned by **static assertions, not execution**:
  the `saneless scan` example carries `--title`, its `source` value matches a real SANE
  spelling (`"Flatbed"`, not `"flatbed"` — the comparison is case-sensitive), and the
  tutorial link resolves to a file that exists. Same harness as everything else; no
  scanner, no network, fails locally.

**Known-live row at the time of this discussion** (spot-checked, do not assume the
in-phase rule held): **row 30 is still false** — `docs/how-to/cli-scripting.md:45` still
shows `"id": "a1b2c3d4"` while ids are UUID4. (`created_at` did gain its `+00:00`.) Rows 8
and 28 were spot-checked and *are* corrected.

**Rough row ownership** (planning should verify, not trust): rows 1–14, 20, 22–28 and 33
were Phases 21–30's in-phase work; rows 15, 16, 17, 18, 19, 21, 29, 30, 31, 32 and 34 are
this phase's own.

### Doc surfaces (DOCS-03, DOCS-04, DOCS-05, DOCS-06)

- **D-46:** **`docs/PRD.md` moves to `.planning/`.** It is a v1.0 planning artifact that
  ended up in the published docs tree (row 34: none of its claims match shipped code). It
  is absent from `mkdocs.yml`'s `nav:` but MkDocs still builds and publishes any `.md`
  under `docs/`, so today it is reachable and search-indexed. Moving it removes a whole
  page from the audit surface while keeping the document where planning artifacts live.
  Suggested destination: alongside `.planning/milestones/v1.0-ROADMAP.md`.
- **D-47:** **"Which setup do I have?" is the first page under Getting Started**, ahead of
  Quick Start in `mkdocs.yml`'s nav, and it is a **short decision list**: three named
  setups, one sentence each on how to tell which is yours, and the exact compose/`docker
  run` lines for each. DOCS-04 says it is linked from the quick-start prerequisites — and
  a reader who picks the wrong shape there loses the next hour, so it goes before the
  install, not under How-To.
- **D-48 (load-bearing):** **One USB rule, everywhere.** Bare metal can enumerate local
  USB directly via libsane; **the container never touches USB and always reaches a scanner
  over the SANE network protocol**, including a `saned` running on its own host. This
  matches PROJECT.md's constraint ("No `--privileged` required; USB device access is
  handled by the `saned` server") and Phase 14's `SANE_NET_HOSTS` design. **Consequence:
  `docs/reference/docker.md`'s "USB Scanner Access" section (lines ~156-158, "pass the USB
  bus") is deleted, not corrected.** It contradicts the project's own constraint, it is
  untested, and it adds a fourth shape to a page whose purpose is to make the choice
  obvious.

  Row 29's contradiction is four statements, not two, and all four must end up consistent:
  - `docs/getting-started/first-cli-scan.md:9, 67` — "USB scanners **must** be connected
    to the machine running `saned`"
  - `docs/how-to/install-bare-metal.md:9` — "via `saned` on the network **or locally via
    USB**"
  - `docs/reference/configuration.md:38` — "Empty = local USB"
  - `docs/reference/docker.md:156-158` vs `docs/how-to/scanner-host-discovery.md:13`
    ("containers **cannot** access USB scanners attached to the host")
- **D-49:** DOCS-06 — the compose example's partial Paperless service is **replaced by a
  pointer to the official paperless-ngx compose file**, keeping saneless's own compose
  focused on saneless. A stale copy of someone else's stack is a claim this project would
  have to keep true forever, and paperless-ngx's requirements change without notice.
  (`docker run` examples still get `$(pwd)` per row 31.)
- **D-50:** DOCS-05 — **one sentence in the quick-start prerequisites and one on the
  Docker reference**, each linking to existing content. **No new trust-model page.** The
  material already exists: `docs/how-to/deploy-docker-compose.md:136` has a "Running
  behind a reverse proxy" section (Phase 26's cross-origin work) and
  `docs/reference/web-api.md:316` already states there is no authentication and to use a
  reverse proxy. Criterion 5's "trust-model page" is satisfied by a link that resolves.

### Secrets hygiene

- **D-51:** The user's real working config moves **out of the repository tree** to
  `~/.config/saneless/config.toml` — the XDG path Phase 27 shipped and the config search
  order already finds. This is a structural fix: a real token cannot reach the build
  context, the git index, or a `tar` of the working directory, because it is not in the
  working directory. **This is a user action, not phase work.**
- **D-52:** **No secrets guard beyond the `.dockerignore` allow-list and its test**
  (D-29, D-30). A token-shaped-string prek hook was rejected: 40 hex characters is also a
  git SHA, so it would fire on the very SHA pins and image digests this phase adds.
- **D-53:** The paperless token is **not being rotated** (user's call). Assessment on
  record: the file is gitignored and was never committed; the exposure was the Docker
  build *context* and the discarded builder stage only; the runtime stage copies only
  `/dist/*.whl`; `uv build --wheel` packages `src/` only; and the release workflow has
  never successfully run, so no image built from this tree was ever published.

---

### Amendments after research (2026-09-18)

Research (`31-RESEARCH.md`) executed rather than read — it ran zizmor, built the wheel,
measured volume ownership, and read Dependabot's parser source. Five decisions above are
amended as a result. **These amendments are locked; treat them exactly as the D-numbered
decisions they modify.**

- **D-13 AMENDED (user decision):** `docs.yml` moves to the **Pages-artifact flow** —
  `mkdocs build --strict` → `actions/upload-pages-artifact` → `actions/deploy-pages`.
  `mkdocs gh-deploy` **cannot** satisfy D-15: it pushes using the checkout's persisted
  credentials, so zizmor raises `artipacked`; `persist-credentials: false` breaks the
  deploy, omitting it is a finding, and suppression is forbidden. The artifact flow was
  measured at zero findings, exit 0. Two consequences:
  - **Blocker 5 changes** to *Settings → Pages → Source: **GitHub Actions*** (not "serve
    the `gh-pages` branch").
  - GitHub auto-creates a **third** environment, `github-pages`, alongside D-18's
    `testpypi` and `pypi`. D-18's "two environments" refers to the publish environments
    only and is otherwise unchanged.
  - The docs job needs **no `libsane-dev`**:
    `uv sync --locked --only-group dev --no-install-project` installs mkdocs-material
    without building `python-sane`.

- **D-27 AMENDED:** Dependabot's Docker parser matches **`FROM` lines only** — it
  explicitly skips `COPY --from=` (verified in `dependabot-core`'s
  `docker/lib/dependabot/docker/file_parser.rb`). A digest pin written directly on the
  `COPY --from=ghcr.io/astral-sh/uv:…` line would never be updated. Restructure to
  `FROM ghcr.io/astral-sh/uv:<ver>@sha256:… AS uv` followed by
  `COPY --from=uv /uv /usr/local/bin/uv`, so Dependabot sees it. Also: adding a second
  `updates:` entry creates a `dependabot-cooldown` zizmor finding unless **both** entries
  carry a `cooldown:` block.

- **D-36/D-38 AMENDED (user decision):** in **serve** mode the stream renders
  **tracebacks**, with or without `-v`. Phase 28 D-06's traceback-free rule was justified
  by "stderr is the user's terminal now" — that is false for a service, where the stream
  *is* the log and `docker logs`/journald is nobody's terminal, and where no file exists
  to carry the traceback instead. **CLI mode is unchanged**: traceback-free unless `-v`,
  exactly as Phase 28 wrote it. D-38's "`-v` is unchanged in serve" still holds for what
  `-v` *means* (saneless loggers at DEBUG; uvicorn and httpx never raised).

- **D-08 CORRECTED:** the claim of "three enforcement points" was wrong.
  `.pre-commit-config.yaml` has **no pytest hook** — its push stage runs ty, pyrefly,
  ruff check and ruff format only. The guard therefore has **two** enforcement points
  (CI, and a local `uv run pytest`) unless the plan also adds a pytest push hook. CI-02
  is satisfied by CI alone, so this is not blocking; the plan may add the hook or not,
  but it must not restate the false claim.

- **D-05 RATIONALE CORRECTED:** neither `uv_build` 0.10.3 nor `twine check` errors when
  the legacy license classifier is left alongside `license-expression`. The migration is
  still correct and PEP 639 support is **verified working** (a real build produced
  `saneless-0.2.0.dist-info/licenses/LICENSE` and `License-Expression: MIT`), but nothing
  in the toolchain will catch a regression — so the classifier's **absence needs a static
  assertion** in the test harness, like every other claim in this phase.

**Verify-at-implementation (not settled):** research reports that zizmor 1.30.1 raises
`self-repository` against `uses: ./.github/workflows/ci.yml` and demands
`uses: $/.github/workflows/ci.yml`, citing a GitHub changelog of 2026-07-30 and runner
≥ 2.336.0. GitHub's own reusable-workflows documentation still shows only the `./` form.
The zizmor rule's existence is corroboration, and the release rehearsal would surface a
break immediately — but the plan must **confirm the syntax against GitHub's current docs
before writing it**, and fall back to `./` if the `$` form does not resolve.

**Also carried from research:** `FLY002` rewrites `"-".join(("kris", "knigga"))` back
into the literal string, which would defeat D-11. Research linted three clean
alternatives; an f-string over named constants is its recommendation. And `S603` applies
to shelling out to `git ls-files` — the fix is a literal argv with the path passed via
`cwd=`, the shape `tests/test_scanner.py` and `tests/test_atomic_write.py` already use.

**Standing project rule, restated for the planner:** Claude never runs `git tag`. Every
tag in this phase is pushed by the user (D-20). No plan task may create a tag.


### Claude's Discretion

- **RC tag shape and `latest` gating (D-23)** — pick the pre-release tag pattern and the
  mechanism that stops it tagging `latest` (metadata-action `enable=` condition or
  equivalent). Hard constraint restated: a rehearsal must never leave `latest` or a plain
  `pip install saneless` pointing at a pre-release.
- **`.gitignore` PNG paths (D-33)** — enumerate what actually writes PNGs in this repo
  and ignore exactly those paths. No `*.png` glob may remain.
- **Plumbing of the logging mode split** — how `serve` selects the streaming
  configuration versus the CLI's file configuration (a second entry point, a mode
  parameter on `configure_logging`, click's `invoked_subcommand`, …). Constraints: D-34's
  "one-shot commands are unchanged" is literal, `ctx.obj["log_file"]` must be `None` in
  serve mode so nothing prints `"Full details in …"`, and `_TracebackFreeFormatter`'s
  no-traceback-unless-`-v` rule (Phase 28 D-06) must still hold on the stream.
- **`cli.py:917`'s wording** — the uvicorn-startup-failure message says "the cause is in
  the log". Still true (the stream is the log), but worth a look now that "the log" means
  something different in serve mode.
- **Exact `.dockerignore` allow-list** — beyond the D-29 starting set, work out what
  `uv build --wheel` actually needs. A real `saneless.toml`, anything under `.planning/`,
  and `tests/` can never reach the build context.
- **Plan sequencing for the rename** — the rename and its guard have an ordering
  constraint (guard-first makes CI red immediately; rename-first leaves a window with no
  guard), and `tests/test_deployment_config.py` both contains the old name and is where
  the guard goes. Landing them in one commit is the obvious resolution; planning should
  make it explicit rather than let the executor discover it.
- **Audit artifact format (D-42)** — table shape and filename, provided each of the 34
  rows plus the re-checked "correct" claims has a named disposition and traceable
  evidence.

### Blockers requiring the user's accounts

Planning must treat these as prerequisites for criterion 2 and criterion 5, not as tasks
Claude can complete:

1. **Pending trusted publisher on PyPI** for project `saneless`, bound to owner
   `kdknigga`, repo `saneless`, workflow `release.yml`, environment `pypi`.
2. **Pending trusted publisher on TestPyPI**, same values, environment `testpypi`.
   (`saneless` is free on both indexes — both `/pypi/saneless/json` endpoints return 404 —
   so both must use the *pending* publisher flow, which lives under the account sidebar,
   not a project's.)
3. **Two GitHub environments** (`testpypi`, `pypi`) created, with the approval rule on
   `pypi` per D-18.
4. **GHCR package visibility** — packages are created private on first push; an anonymous
   `docker pull` needs it made public.
5. **GitHub Pages enabled** on `kdknigga/saneless` serving the `gh-pages` branch, or
   `mkdocs gh-deploy` publishes to a branch nothing serves.
6. **Tag pushes** (D-20).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### This phase's source of truth

- `.planning/ROADMAP.md` § "Phase 31: Delivery, Identity, and Documentation Accuracy" —
  goal, the 17 requirement IDs, and the five success criteria this phase is verified
  against.
- `.planning/REQUIREMENTS.md` — CI-02, DLVR-01..DLVR-10, DOCS-01..DOCS-06 with their
  originating review finding IDs.
- `.planning/reviews/2026-09-09-code-review.md` **§ 8 "Documentation accuracy audit"**
  (lines 980–1027) — the 34 false-claim rows *and* the "claims that were checked and are
  correct" list that D-44 puts back in scope. This is the single most important reference
  for the phase.
- `.planning/reviews/2026-09-09-code-review.md` § 4 (M-25..M-31) and § 5 (N-26, N-30,
  N-31, N-32, N-42) — the delivery findings behind the DLVR requirements.
- `.planning/reviews/2026-09-09-code-review.md` § 11 (U-09, U-10) — the usability findings
  behind DOCS-04 and DOCS-05.

### Decisions this phase builds directly on

- `.planning/phases/30-appliance-layer/30-CONTEXT.md` § "Deferred Ideas" — the explicit
  hand-off of the `kris-knigga` compose reference to DLVR-01.
- `.planning/phases/28-exception-translation/28-CONTEXT.md` § "PdfError and exit codes"
  and § "Claude's Discretion" (D-06, D-07, D-08) — the CLI one-line / exit-code contract
  that D-34 preserves.
- `.planning/phases/27-configuration-strictness/27-CONTEXT.md` (D-02, D-03, D-08, D-09) —
  the tool-owned profile key set, `--force` semantics, read-only-config degradation, and
  the read-write `./config:/etc/saneless` mount that D-25/D-26 must not break.
- `.planning/PROJECT.md` § "Constraints" — "No `--privileged` required; USB passthrough
  handled by `saned`", the constraint D-48 makes the docs match.

### Code and config this phase edits

- `pyproject.toml` — version, classifiers, license (D-01, D-02, D-05), and the dev group
  for zizmor (D-14).
- `src/saneless/cli.py` — `version_option` (D-03/D-04), the `serve` command's
  `uvicorn.run` call at ~line 904, `_load_settings`'s `configure_logging` call at
  ~line 540, and the `"Full details in {log_file}"` suffix at lines 392–394.
- `src/saneless/logging_config.py` — the mode split (D-34..D-40).
- `src/saneless/config.py` — `OutputConfig` (line 445) log keys, unchanged but
  re-documented for mode scope (D-39).
- `.github/workflows/ci.yml` (`workflow_call`, zizmor step), `release.yml` (D-16..D-19,
  D-23), `docs.yml` (D-13), `.github/dependabot.yml` (D-27).
- `Dockerfile`, `.dockerignore` (new), `docker-compose.yml`, `saneless.toml.example`,
  `.gitignore`, `mkdocs.yml`, `README.md`.
- `tests/test_deployment_config.py` — the doc-truth harness the guard (D-08), the
  `.dockerignore` test (D-30), and the audit tests (D-41, D-45) all join; also the home of
  the two Phase 30 artifacts D-12 deletes (lines 781, 921).

### Docs to correct, create, move or delete in-phase

- `README.md` — rows 16, 17, 18, 19.
- `docs/getting-started/quick-start.md` — rows 19, 31; DOCS-04 link; DOCS-05 sentence.
- `docs/getting-started/first-cli-scan.md` — rows 19, 29 (lines 9, 67).
- `docs/how-to/install-bare-metal.md` — rows 15, 29 (line 9).
- `docs/how-to/deploy-docker-compose.md` — rows 19, 32; already holds the reverse-proxy
  section D-50 links to (line 136).
- `docs/how-to/scanner-host-discovery.md` — row 19; line 13 is one of the four USB
  statements.
- `docs/how-to/cli-scripting.md` — **row 30, still live** (line 45).
- `docs/reference/docker.md` — rows 19, 21; **the "USB Scanner Access" section is deleted**
  (D-48).
- `docs/reference/configuration.md` — line 38's USB claim; the log-key rows gain mode
  scope (D-39).
- `docs/reference/web-api.md` — line 316 is the existing no-auth statement D-50 links to.
- `docs/PRD.md` — **moved out** (D-46).
- **New:** `docs/getting-started/` "Which setup do I have?" (D-47).

### External references

- PEP 639 — <https://peps.python.org/pep-0639/> (D-05: `license` as an SPDX string,
  `license-files`, and the rule that a `License-Expression` may not ship alongside a
  license classifier).
- Click `version_option` — <https://click.palletsprojects.com/en/stable/api> (D-03/D-04:
  `package_name=` reads installed metadata; default message
  `"%(prog)s, version %(version)s"`).
- PyPI trusted publishing, pending publishers —
  <https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/> (D-18: the
  five bound values including environment name; pending publishers convert on first use).
- zizmor — <https://docs.zizmor.sh/usage> and <https://github.com/zizmorcore/zizmor>
  (D-14/D-15: PyPI-installable, audits workflows + Dependabot + `.pre-commit-config.yaml`,
  personas, SARIF).
- Dependabot Docker ecosystem — supports `package-ecosystem: "docker"` and updates
  `image:tag@sha256:…` digest pins (D-27).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets

- **`tests/test_deployment_config.py`** — the doc-truth harness. Already defines
  `REPO_ROOT`, `COMPOSE`, `DOCS_DIR`, `_doc_pages()` (every `docs/**/*.md`),
  `_deployment_files()`, and `_read()`. Every new guard and audit test in this phase
  extends it rather than starting a new file. Its convention: plain-text assertions with
  `file:line` offender lists, and expectations derived from source constants
  (`WebConfig`, `ExitCode`, `RequestRejection`, route decorators).
- **`ci.yml`'s SHA-pin convention** — `uses: owner/repo@<40-hex> # vX.Y.Z`, with a header
  comment recording when the SHAs were resolved and by what. D-13 and D-16 follow it.
- **`.pre-commit-config.yaml`'s `repo: local` block** (line 93) — six `language: system`
  hooks calling `uv run …`. D-14's zizmor hook is a seventh in the same shape.
- **`configure_logging`'s `_TracebackFreeFormatter`** — under D-35 this is promoted from a
  fallback-only formatter to the shape the serve stream needs when `-v` is absent. It is
  already written and tested.
- **`docs/how-to/deploy-docker-compose.md:136`** (reverse proxy) and
  **`docs/reference/web-api.md:316`** (no auth, trusted LAN) — the content D-50 links to
  instead of writing a new page.

### Established Patterns

- **Documentation is cross-cutting**: each phase corrects the sentences its own change
  falsified, in that same phase. DOCS-01 is the audit that this actually happened —
  spot-checks say it mostly did (rows 8, 28) and sometimes did not (row 30).
- **No suppression**: ruff, ty and pyrefly must pass with zero errors, and `# noqa` /
  `# type: ignore` / rule-disabling are forbidden. D-15 extends this contract to zizmor.
- **`git ls-files` is the honest definition of "shipped"** in a repo where `site/`,
  `.venv/` and the working `saneless.toml` are all gitignored and `.planning/` is tracked.
- **Config keys are strict** (`extra="forbid"`, Phase 27). D-24 avoids adding one; D-39
  documents rather than warns about the one key whose scope narrows.

### Integration Points

- **`uvicorn.run(app, …, log_config=None, log_level=…, access_log=True)`**
  (`cli.py:904`) — `log_config=None` means uvicorn attaches no handlers and propagates to
  saneless's root handlers. D-37 therefore needs no code change: access lines follow
  whatever the root handlers are.
- **`_load_settings` → `configure_logging(...)` → `ctx.obj["log_file"]`**
  (`cli.py:540-548`) — the single seam where the mode split lands. `warn_on_legacy_duplex_
  sources` and `log_config_sources` fire immediately after and depend on a handler being
  attached; both must still work under the stream.
- **`Dockerfile`'s `ENV SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless` + `VOLUME`** — the
  existing precedent for "bake the right path into the image so the operator does not have
  to know the variable exists". D-28's `WORKDIR` joins it; D-25's `chown` must come before
  `VOLUME`.
- **`docker/metadata-action`'s unconditional `type=raw,value=latest`** in `release.yml` —
  the exact line D-23's constraint applies to.
- **`mkdocs.yml`'s `nav:`** — D-47's new page is inserted ahead of Quick Start; D-46's
  removal needs no nav change (PRD.md is not listed, only published).

</code_context>

<specifics>
## Specific Ideas

- **"Not by reading the workflow file"** is the user's standard for criterion 2, and D-21
  operationalises it: a throwaway `python:3.14-slim` container doing a real `pip install`,
  and a `docker run --rm` on the real pulled image, with the output shown.
- **The mode framing for logging came from the user directly**: *"we have kind of two
  modes: operating via cli commands, and running as a long-running service. CLI command
  mode should write log file, but long running service mode should act like a 12 factor
  service and stream logs."* That framing is what makes DLVR-04 need no new config key —
  the mode already selects the behaviour.
- **Two live findings surfaced during discussion** that planning should not have to
  rediscover:
  1. `docs.yml` triggers on `main`; the branch is `master`. The docs site has never
     deployed. (D-13)
  2. `docs/how-to/cli-scripting.md:45` still shows `"id": "a1b2c3d4"` — row 30 is live.
- **`saneless` is free on PyPI and TestPyPI** (both endpoints 404), confirming DLVR-01's
  "the PyPI distribution name stays `saneless`" and dictating the pending-publisher flow.

</specifics>

<deferred>
## Deferred Ideas

- **A dedicated trust-model / security-posture Explanation page** — rejected by D-50 in
  favour of sentences linking to existing content. If the no-auth, all-interfaces,
  owner-token and reverse-proxy material ever needs one authoritative home rather than
  three partial ones, that is a small follow-up.
- **A post-release CI verification job** that installs the published wheel and pulls the
  published image on a fresh runner — rejected by D-21 in favour of throwaway containers
  now. It would re-prove itself on every release; revisit if releases become frequent.
- **SARIF upload of zizmor findings to GitHub code scanning** — rejected by D-14 in favour
  of a blocking lint step. Revisit if the finding count ever outgrows "fix them all".
- **A token-pattern prek hook** — rejected by D-52; 40 hex characters collides with the
  SHA pins and image digests this phase adds.
- **Major-tag image recommendation (`:1` / `:0.2`)** and a docs-site version selector —
  rejected by D-06 as churn on a project with no published release yet.
- **Executable README-command checks in CI** — rejected by D-45 in favour of static
  assertions; the scan example cannot run without hardware either way.
- **Documenting `--device /dev/bus/usb` container passthrough as an advanced option** —
  rejected by D-48. It contradicts PROJECT.md's own constraint and is untested.
- **Maturity prose in the README / getting-started** — rejected by D-07.
- **Row 30's sibling cosmetic nits** and the remaining N-01..N-45 items — Phase 32.
- **`docs/reference/web-api.md`'s OpenAPI claims** — Phase 33 owns the schema endpoints;
  if the audit finds a row about them, note it and leave it to that phase.

No pending todos matched this phase.

</deferred>

---

*Phase: 31-delivery-identity-and-documentation-accuracy*
*Context gathered: 2026-09-18*
