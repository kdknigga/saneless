# Phase 31: Delivery, Identity, and Documentation Accuracy - Research

**Researched:** 2026-09-18
**Domain:** Supply-chain hardening (GitHub Actions, PyPI trusted publishing, OCI images), Python packaging (PEP 639), CLI logging modes, documentation-truth auditing
**Confidence:** HIGH for everything executed in this session (zizmor, wheel build, `.dockerignore`, volume ownership, ruff behaviour, doc-row truth values); MEDIUM for the GitHub-side behaviours that need the user's account to observe

---

<user_constraints>
## User Constraints (from CONTEXT.md)

All 53 decisions D-01..D-53 in `31-CONTEXT.md` are **locked**. This research investigates the
mechanics of executing them, not alternatives to them. Read `31-CONTEXT.md` in full before
planning; it is not reproduced here.

### Claude's Discretion (verbatim from CONTEXT.md)

- **RC tag shape and `latest` gating (D-23)** — pick the pre-release tag pattern and the
  mechanism that stops it tagging `latest`. Hard constraint restated: a rehearsal must never
  leave `latest` or a plain `pip install saneless` pointing at a pre-release.
- **`.gitignore` PNG paths (D-33)** — enumerate what actually writes PNGs in this repo and
  ignore exactly those paths. No `*.png` glob may remain.
- **Plumbing of the logging mode split** — how `serve` selects the streaming configuration
  versus the CLI's file configuration. Constraints: D-34's "one-shot commands are unchanged"
  is literal, `ctx.obj["log_file"]` must be `None` in serve mode, and
  `_TracebackFreeFormatter`'s no-traceback-unless-`-v` rule must still hold on the stream.
- **`cli.py:917`'s wording** — the uvicorn-startup-failure message says "the cause is in the
  log".
- **Exact `.dockerignore` allow-list** — beyond the D-29 starting set.
- **Plan sequencing for the rename** — guard and rename have an ordering constraint.
- **Audit artifact format (D-42)** — table shape and filename.

### Deferred Ideas (OUT OF SCOPE)

Trust-model Explanation page; post-release CI verification job; SARIF upload; token-pattern
prek hook; major-tag image recommendation; executable README checks in CI; `--device
/dev/bus/usb` passthrough; maturity prose; row 30's sibling nits and N-01..N-45 (Phase 32);
`web-api.md`'s OpenAPI claims (Phase 33).
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CI-02 | CI fails if any shipped file references the three `kris-knigga` forms | §8 — working guard prototype, ruff-clean, exact file:line inventory |
| DLVR-01 | 24 lines in nine files renamed to `kdknigga` | §8 — full inventory reproduced, verified 2026-09-18 |
| DLVR-02 | Release workflow succeeds end to end | §4, §5 — verified-clean workflow set, correct `gh-action-pypi-publish` SHA, pending-publisher flow |
| DLVR-03 | SHA pins + Dependabot + `permissions:` + zizmor | §3, §4 — 23 concrete zizmor findings and a verified zero-finding target |
| DLVR-04 | Container logs appear in `docker logs` | §7 — uvicorn `log_config=None` propagation verified in source |
| DLVR-05 | Example config / `EXPOSE` / `HEALTHCHECK` agree on one port | §9 row 21 — `saneless.toml.example:11` still `web_port = 8081` |
| DLVR-06 | `.dockerignore` allow-list | §6 — allow-list semantics and real wheel build verified |
| DLVR-07 | Non-root, digest-pinned bases, `WORKDIR` | §6 — volume-ownership behaviour measured; Dependabot `FROM`-only parsing found |
| DLVR-08 | Wheel carries LICENSE via PEP 639 | §2 — wheel built and inspected; `dist-info/licenses/LICENSE` present |
| DLVR-09 | No blanket `*.png` in `.gitignore` | §6.4 — only `test-results/` depends on it today |
| DLVR-10 | `saneless --version` prints the installed version | §2 — `version_option(package_name=…)` output measured |
| DOCS-01 | All 34 review-section-8 rows corrected | §9 — row-by-row disposition with `file:line` |
| DOCS-02 | README examples run as written | §9 rows 16/17/18 — all three still live |
| DOCS-03 | `docs/PRD.md` removed from the published site | §9 row 34 + §10 — `mkdocs build --strict` confirms it builds but is not in nav |
| DOCS-04 | "Which setup do I have?" page + reconciled USB statements | §10 — the four USB statements and the three deployment shapes |
| DOCS-05 | One-sentence no-auth note linking to reverse proxy | §10 — link targets located at exact line numbers |
| DOCS-06 | Paperless service replaced by a pointer; `$(pwd)` in `docker run` | §9 rows 31/32 — both still live |
</phase_requirements>

---

## Summary

Nine of the ten investigation targets were **executed, not read about**. zizmor 1.30.1 was
installed and run against this repo's real workflows (23 findings, exit 14); a candidate
fixed workflow set was written and re-run to a verified **zero findings, exit 0**. A PEP 639
`saneless` wheel was actually built with `uv_build` and unzipped to confirm
`saneless-0.2.0.dist-info/licenses/LICENSE`. The `.dockerignore` allow-list was built into a
real image and the resulting context listed. Named-volume, anonymous-volume and bind-mount
ownership were measured against a non-root image. The naming guard was prototyped, run over
the real tracked file list (29 hits, matching the requirement's "24 lines in nine files" plus
five in the test file D-12 deletes), and linted — turning up **two concrete ruff blockers**
the planner must design around.

Three findings change the shape of the plan rather than merely informing it:

1. **`mkdocs gh-deploy` cannot satisfy zizmor at the default persona.** `gh-deploy` pushes
   with the checkout's persisted credentials, so `persist-credentials: false` breaks it and
   omitting it is an `artipacked` finding. The only suppression-free path is the Pages-artifact
   flow (`mkdocs build` → `upload-pages-artifact` → `deploy-pages`), which **changes blocker
   #5**: GitHub Pages must be set to source *GitHub Actions*, not *deploy from the gh-pages
   branch*.
2. **Dependabot's Docker parser only matches `FROM` lines.** `COPY --from=ghcr.io/astral-sh/uv:…`
   is invisible to it, so D-27's digest pin on uv would rot silently. The fix is free:
   promote uv to a named `FROM … AS uv` stage.
3. **D-08's claim that the guard "runs in prek's push stage" is false today** — there is no
   pytest hook anywhere in `.pre-commit-config.yaml`. Either the phase adds one or the guard
   has two enforcement points, not three.

**Primary recommendation:** Land the workflow set in §4 verbatim (it is verified zero-finding),
promote uv to a `FROM` stage, build the guard with a literal argv and a named-constant string
assembly (§8), and treat §9's row table as the audit's starting inventory — eleven rows are
still live and every one of them is a sentence edit, so D-43's escalation clause is not
expected to fire.

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Identity rename | Repo content (docs/config) | Test harness | A string swap plus a regression guard; no runtime code involved |
| Naming guard | Test suite (`tests/`) | CI workflow | D-08 puts one implementation behind three (really two) runners |
| Workflow audit (zizmor) | CI workflow + prek | Dev dependency | Same wiring as ruff/ty/pyrefly |
| Release orchestration | GitHub Actions | GitHub environments | Tag trigger → reusable CI → two publish jobs |
| Artifact provenance | GitHub OIDC | PyPI / GHCR | Both attestation paths reuse the workflow's existing identity |
| Container privilege | Dockerfile (build) | Docs (operator action) | Image sets UID 1000; bind-mount ownership is the operator's |
| Build-context hygiene | `.dockerignore` + `COPY` | Test harness (D-30) | Two independent gates plus a static-text pin |
| Logging mode selection | `cli.py` entry points | `logging_config.py` | The *command* knows the mode; the configurator executes it |
| Doc-truth enforcement | `tests/test_deployment_config.py` | `.planning/` audit artifact | Statically checkable rows become tests; the rest become recorded reads |

---

## Project Constraints (from CLAUDE.md)

These are as binding as CONTEXT.md's locked decisions. The planner must verify compliance.

| Directive | Consequence for this phase |
|-----------|---------------------------|
| Python 3.14, `uv` (not pip/poetry/conda) | zizmor is added with `uv add --dev zizmor`; `uv.lock` pins it |
| `prek`, not `pre-commit` | D-14's hook goes in the `repo: local` block, `language: system`, `entry: uv run zizmor .` |
| ruff + ty + pyrefly clean, **no suppressions** (`# noqa`, `# type: ignore`, rule-disabling all forbidden) | §8's two ruff findings must be designed around, not silenced. D-15 extends this to zizmor: `# zizmor: ignore` is forbidden |
| Prefer external packages over reimplementing | zizmor rather than a hand-rolled workflow linter; `git ls-files` rather than a hand-rolled ignore parser |
| Use Context7 for library docs | Applied for metadata-action / pypi-publish inputs below |
| Playwright MCP for all browser validation; never "manual-only" | No browser surface changes in this phase; the `browser` CI job is unchanged |
| `prek run` defaults to the staged set — always `--all-files` | Any pre-flight instruction in a plan must say `--all-files` |
| pyrefly must be given paths: `uv run pyrefly check src tests` | Unchanged |
| Commit-stage hooks check `src/` only; full check at pre-merge-commit / pre-push; never `--no-verify` or `SKIP=` | A TDD RED commit for the guard test is permitted |

---

## 1. Standard Stack

### New dependency

| Package | Version | Purpose | Why standard |
|---------|---------|---------|--------------|
| `zizmor` | 1.30.1 | Static analysis for GitHub Actions | The de-facto workflow auditor; PyPI-distributed as a Rust binary wheel, so `uv add --dev zizmor` gives a lockfile-pinned binary with no toolchain [VERIFIED: PyPI registry + executed locally] |

**Installation:** `uv add --dev zizmor`
Console script name is `zizmor` [VERIFIED: inspected the installed venv's `bin/`].
`requires_python >= 3.10`; manylinux_2_28_x86_64 wheel available, which is what
`ubuntu-latest` needs [VERIFIED: pip resolution log].

### No other new packages

Everything else in this phase is configuration, workflow YAML, Dockerfile, docs, and edits to
existing modules. No runtime dependency changes.

### Alternatives considered

| Instead of | Could use | Tradeoff |
|------------|-----------|----------|
| zizmor | `actionlint` | actionlint checks syntax and shell, not supply-chain posture; it would not find `unpinned-uses` or `artipacked`. Rejected — different problem |
| `git ls-files` via subprocess | `pathspec` + manual `.gitignore` parsing | Adds a dependency to reimplement what git already answers; D-10 explicitly names `git ls-files` |

---

## Package Legitimacy Audit

| Package | Registry | Age | Releases | Source Repo | slopcheck | Disposition |
|---------|----------|-----|----------|-------------|-----------|-------------|
| `zizmor` | PyPI | first upload 2024-12-06 (~21 months) | 71 | github.com/zizmorcore/zizmor | `[OK]` | Approved |

**Packages removed due to slopcheck `[SLOP]` verdict:** none
**Packages flagged as suspicious `[SUS]`:** none

Verification performed: `slopcheck install zizmor` → `[OK]`; `curl https://pypi.org/pypi/zizmor/json`
for age/release count/source URL; binary executed locally at 1.30.1. The package is also named
directly in CONTEXT.md's External References (`https://docs.zizmor.sh/usage`), an authoritative
source, so it qualifies as `[VERIFIED: PyPI registry]` rather than `[ASSUMED]`.

No postinstall-script vector applies (PyPI wheel, not npm).

---

## 2. Version, packaging and `--version` (D-01..D-05, DLVR-08, DLVR-10)

### PEP 639 under `uv_build` — verified end to end

Built the real project from a minimal context with the D-05 pyproject changes applied:

```toml
version = "0.2.0"
license = "MIT"
license-files = ["LICENSE"]
# "License :: OSI Approved :: MIT License" removed
```

Result [VERIFIED: `uv build --wheel` executed 2026-09-18 with uv 0.10.3]:

```
saneless-0.2.0.dist-info/licenses/LICENSE     1068 bytes
```

```
Metadata-Version: 2.4
License-Expression: MIT
License-File: LICENSE
```

**The literal test for DLVR-08:**

```bash
uv build --wheel --out-dir dist
unzip -l dist/saneless-0.2.0-py3-none-any.whl | grep 'dist-info/licenses/LICENSE'
```

A doc-truth-style assertion can also read the built wheel, but that needs a build step; the
cheaper pin is a static assertion on `pyproject.toml` (§ Validation Architecture).

### Correction to D-05's stated rationale

D-05 says "build tools error when a `License-Expression` ships alongside a license classifier."
**That is not true for this toolchain.** Measured [VERIFIED: executed]:

- `uv_build` 0.10.3 builds the wheel **successfully** with both present, emitting both
  `License-Expression: MIT` and `Classifier: License :: OSI Approved :: MIT License`.
- `twine check` on that wheel reports `PASSED`.

PEP 639's actual wording is: *"If the `License-Expression` field is present, build tools **MAY**
raise an error if one or more license classifiers is included"* and PyPI's mandatory rejection
applies to the deprecated free-text `License` field, not classifiers [CITED: peps.python.org/pep-0639/].

**The decision (remove the classifier) stands** — it is what PEP 639 deprecates and what the
requirement asks for. But **nothing in the build will fail if it is left in**, so the planner
must pin the removal with a static assertion in the deployment harness rather than assuming
the build is the test. This is a "the rationale was wrong, the decision is right" note, not a
decision to revisit.

### `@click.version_option(package_name="saneless")` — verified

[VERIFIED: executed against click 8.3.1 in the project venv]

```
'cli, version 0.1.0\n'  exit 0
```

The version comes from `importlib.metadata.version("saneless")` — installed metadata, so
`pyproject.toml` is the single source, exactly as D-03 requires. Under the installed console
script the prog name is `saneless`, giving `saneless, version 0.2.0`.

**Three gotchas for the planner:**

1. **`CliRunner` prog name is `cli`, not `saneless`.** A test asserting the literal
   `"saneless, version …"` must pass `runner.invoke(cli, ["--version"], prog_name="saneless")`.
   Without it the assertion fails on the prog name, not the version.
2. **The asserted version must come from `importlib.metadata`, not from parsing
   `pyproject.toml`.** Asserting `pyproject.toml`'s version equals the printed version makes
   the test fail in any venv whose installed metadata is stale relative to an edited
   `pyproject.toml` — which is every venv between editing the version and re-running `uv sync`.
   The honest assertion is `output == f"saneless, version {importlib.metadata.version('saneless')}"`
   plus a *separate* static assertion that `pyproject.toml` declares `0.2.0`.
3. **Row 15's doc fix and DLVR-10 are the same change.** `docs/how-to/install-bare-metal.md:47`
   already tells the reader to run `saneless --version`; today that exits 2 with
   `Error: No such option: --version Did you mean --verbose?` [VERIFIED: executed]. Adding the
   option makes the sentence true; no doc edit is needed for that row.

---

## 3. zizmor: what it actually finds here (D-14, D-15)

### Baseline — the repository as it stands today

Command run: `zizmor --no-online-audits .` from the repo root, zizmor 1.30.1, default
(`regular`) persona [VERIFIED: executed 2026-09-18].

**Result: `33 findings (10 suppressed, 1 safe fix, 9 unsafe fixes): 0 informational, 0 low,
10 medium, 13 high` — 23 displayed findings, process exit code `14`.**

Files collected (zizmor's own log): `.github/dependabot.yml`, `.github/workflows/ci.yml`,
`.github/workflows/docs.yml`, `.github/workflows/release.yml`, **and `.pre-commit-config.yaml`**.
So yes — zizmor does audit both Dependabot config and the prek config in this repo. Only
`dependabot.yml` produced a finding; `.pre-commit-config.yaml` was clean.

### The 23 findings, by rule

| Rule | Count | Where | Fix |
|------|-------|-------|-----|
| `unpinned-uses` (error/high) | 11 | `docs.yml:14,15`; `release.yml:11,12,25,26,28,37,38,43,50` | SHA-pin every `uses:` with a `# vX.Y.Z` comment |
| `artipacked` (warning/low-confidence) | 7 | `ci.yml:22,39,61`; `docs.yml:14`; `release.yml:11,25,37` | `with: persist-credentials: false` on every `actions/checkout` |
| `excessive-permissions` (warning/medium) | 2 | `release.yml` workflow level and its `test` job | `permissions:` block at workflow level **and** on every job |
| `cache-poisoning` (error/low-confidence) | 2 | `release.yml:12,26` (`setup-uv`) | `with: enable-cache: false` — fires because the workflow is tag-triggered (publishing) |
| `dependabot-cooldown` (warning/high) | 1 | `dependabot.yml:4` | `cooldown: { default-days: 7 }` on each `updates:` entry |

**Note on `artipacked` firing on `ci.yml` today:** the three CI checkouts are already SHA-pinned,
so the only change CI needs is `persist-credentials: false`. This is the cheapest of the five
fixes and it is the one that touches the currently-green workflow.

**Note on `dependabot-cooldown`:** adding `package-ecosystem: "docker"` (D-27) creates a
*second* `updates:` entry, which will produce a *second* `dependabot-cooldown` finding unless
`cooldown:` is set on both. Plan for two cooldown blocks, not one.

### Exit-code semantics for a blocking CI step

[VERIFIED: measured]

- Findings present → exit `14`
- No findings → exit `0`

A bare `- run: uv run zizmor .` in the `lint` job therefore fails the build on any finding with
no extra shell. No `--min-severity`, no `--min-confidence`, no `|| true`.

**Persona:** default is `regular` (`--persona` values: `auditor`, `pedantic`, `regular`). D-15's
"default persona" is `regular` — no flag needed. The "10 suppressed" in the summary line are
findings the pedantic/auditor personas would show; they are correctly invisible at `regular`.

### Collection scope

`--collect default` (the default) collects workflows, composite actions, `dependabot.yml`,
and pre-commit config, honouring `.gitignore`. Running `zizmor .` from the repo root is
sufficient; no `--collect` flag needed.

### prek hook shape (D-14)

Matching the existing `repo: local` block's convention exactly:

```yaml
      - id: zizmor
        name: zizmor (workflow audit)
        entry: uv run zizmor .
        language: system
        pass_filenames: false
        always_run: true
```

Stage: the block's `default_stages: [pre-commit]` applies (zizmor runs in well under a second
on five files, and it never rewrites a file, so commit stage is safe). If the planner prefers
the push gate, add `stages: [pre-merge-commit, pre-push]` in the "full" hooks' shape.

---

## 4. The verified-clean workflow set (D-13, D-16..D-19, D-23, D-27)

This exact set was written to a scratch directory and audited: **`No findings to report. Good
job! (8 suppressed)` — exit 0** [VERIFIED: executed 2026-09-18 with zizmor 1.30.1].

SHAs resolved 2026-09-18 via `api.github.com` [VERIFIED: GitHub REST API]:

| Action | Tag | SHA |
|--------|-----|-----|
| `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` (already in `ci.yml`) |
| `astral-sh/setup-uv` | v10.1.0 | `bec219d24cd3e171d82865faccec33120bb574f4` (`ci.yml` has v10.0.1 — either is fine; keep one) |
| `pypa/gh-action-pypi-publish` | **v1.14.2** | `dc37677b2e1c63e2034f94d8a5b11f265b73ba33` |
| `docker/login-action` | v4.6.0 | `dbcb813823bdd20940b903addbd779551569679f` |
| `docker/metadata-action` | v6.2.0 | `dc802804100637a589fabce1cb79ff13a1411302` |
| `docker/build-push-action` | v7.4.0 | `c3c9e263c25d99ce0380d002d59b67737d91b0dc` |
| `actions/upload-pages-artifact` | v5.0.0 | `fc324d3547104276b827a68afc52ff2a11cc49c9` |
| `actions/deploy-pages` | v5.0.1 | `368f82528645a54fb793d4d04e342629a3f51346` |

**`pypa/gh-action-pypi-publish@v1.12` is genuinely unresolvable** — `GET /git/ref/tags/v1.12`
returns 404 [VERIFIED]. The tags that exist are `v1.12.0`..`v1.12.4`; current latest is
`v1.14.2`. Note that for this repo the tag object is *annotated*, so the SHA above is the
**dereferenced commit** (`dc37677b…`), not the tag object SHA (`a892a5a6…`). Pinning the tag
object SHA would not resolve.

### `ci.yml` — becomes callable

Add to the `on:` block and give every job a `permissions:` block:

```yaml
on:
  push:
    branches: [master]
  pull_request:
  workflow_call:
```

Each job gains `permissions: { contents: read }` and each `actions/checkout` gains
`with: { persist-credentials: false }`. Add the zizmor step to `lint`.

**Reusable-workflow gotchas, confirmed:**

- Secrets are **not** inherited unless the caller passes `secrets: inherit`. `ci.yml` uses no
  secrets, so nothing is needed.
- A called workflow's `GITHUB_TOKEN` permissions can only be **the same or more restrictive**
  than the calling job's. Give the calling job `permissions: { contents: read }` and the callee
  workflow the same.
- A reusable workflow **can** be called from a tag-triggered run. `./` and `$/` both resolve to
  the running commit (the tagged one).
- `ci.yml`'s `concurrency.group` uses `${{ github.workflow }}` which, in a `workflow_call`
  context, is the **caller's** workflow name (`Release`), and `github.ref` is the tag — so the
  group is `Release-refs/tags/v0.2.0`. `cancel-in-progress` evaluates `github.event_name ==
  'pull_request'` → false. No conflict, but the planner should know the group name changes.
- **`ci.yml`'s `browser` job becomes part of the release gate.** That is exactly D-16's intent
  ("one definition of green") and costs a Chromium download per release. Worth stating in the
  plan so it is not a surprise.

### `release.yml` — self-repository call syntax

zizmor 1.30.1 raises `self-repository` (**High** confidence) against `uses: ./.github/workflows/ci.yml`
and demands `uses: $/.github/workflows/ci.yml`. This is real: GitHub shipped the `$/`
self-repository syntax on 2026-07-30; it works everywhere `./` works, resolves to the running
commit without a checkout, and requires runner ≥ 2.336.0 (GitHub-hosted runners are well past
that) [CITED: github.blog/changelog/2026-07-30-reference-same-repository-actions-with-self-repository-syntax/].

```yaml
jobs:
  ci:
    uses: $/.github/workflows/ci.yml
    permissions:
      contents: read
```

### Pre-release routing and `latest` gating (D-17, D-18, D-23)

**Recommended RC tag shape: `v0.2.0-rc.1`, with `version = "0.2.0-rc.1"` in `pyproject.toml`.**

Why this exact spelling threads every needle [VERIFIED: measured]:

- `packaging.Version("0.2.0-rc.1")` normalises to `0.2.0rc1` — canonical PEP 440.
- `uv build --wheel` with `version = "0.2.0-rc.1"` emits **`saneless-0.2.0rc1-py3-none-any.whl`**
  — uv_build normalises for you, so the file PyPI sees is canonical.
- `v0.2.0-rc.1` **is** valid semver, so `docker/metadata-action`'s `type=semver` parses it.
  `v0.2.0rc1` is **not** valid semver and metadata-action would silently emit no version tag.

**`latest` gating — use the mechanism the action already has.** `docker/metadata-action`'s
default `flavor: latest=auto` already skips `latest` for semver pre-releases: *"Pre-release
(rc, beta, alpha) will only extend `{{version}}` … as tag"* [CITED: github.com/docker/metadata-action].
So the fix for D-23 is **deleting the unconditional `type=raw,value=latest` line**, not
conditioning it:

```yaml
          tags: |
            type=semver,pattern={{version}}
```

If the planner wants belt-and-braces, the documented conditional form is
`type=raw,value=latest,enable=${{ !contains(github.ref_name, '-') }}` — but note that adding it
back re-introduces the exact line D-23 is worried about, now with a condition that has to stay
correct. The delete-and-rely-on-`latest=auto` option has strictly fewer moving parts.

**The `pip install saneless` half of D-23's constraint is satisfied structurally:** the RC only
ever lands on TestPyPI, and PyPI stays empty until the final tag. (For completeness: pip will
install a pre-release when it is the *only* candidate, which is why keeping the RC off PyPI
matters more than the `--pre` default.)

**One job or two?** D-17 wants "one workflow with one code path"; D-18 needs two environments.
A single job **can** do both — `environment.name` accepts an expression, but only in the
**object form**; the string form `environment: ${{ … }}` fails with `Unrecognized named-value`
[CITED: github.com/orgs/community/discussions/38178, actions/runner#998]. Allowed contexts
include `github`.

```yaml
  publish-pypi:
    needs: ci
    runs-on: ubuntu-latest
    environment:
      name: ${{ contains(github.ref_name, '-') && 'testpypi' || 'pypi' }}
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4 # v10.1.0
        with:
          enable-cache: false        # clears zizmor cache-poisoning
      - run: uv build
      - uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2
        with:
          attestations: true
          repository-url: ${{ contains(github.ref_name, '-') && 'https://test.pypi.org/legacy/' || '' }}
```

Caveat to verify at execution time: `gh-action-pypi-publish`'s `repository-url` default is the
PyPI legacy endpoint; passing an **empty string** may or may not fall back to that default
depending on the action's version. The safer single-job form is to compute the URL in a prior
step and always pass a non-empty value:

```yaml
      - id: index
        run: |
          if [[ "${GITHUB_REF_NAME}" == *-* ]]; then
            echo "url=https://test.pypi.org/legacy/" >> "$GITHUB_OUTPUT"
          else
            echo "url=https://upload.pypi.org/legacy/" >> "$GITHUB_OUTPUT"
          fi
```

`attestations: true` works for TestPyPI: the action's docs state attestation support is
"currently limited to Trusted Publishing flows using PyPI **or TestPyPI**"
[CITED: github.com/pypa/gh-action-pypi-publish].

The approval rule on the `pypi` environment (D-18) gates this job only when the expression
resolves to `pypi` — which is exactly the desired behaviour.

### `publish-docker` — provenance and permissions

`provenance: true` on `docker/build-push-action` needs `id-token: write` and
`attestations: write` in addition to `packages: write`. Verified zizmor-clean with:

```yaml
    permissions:
      contents: read
      packages: write
      id-token: write
      attestations: write
```

### `docs.yml` — see §11, "Decisions that need revisiting"

### `dependabot.yml`

```yaml
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule: { interval: "weekly" }
    cooldown: { default-days: 7 }
  - package-ecosystem: "docker"
    directory: "/"
    schedule: { interval: "weekly" }
    cooldown: { default-days: 7 }
```

**Watch item:** Dependabot's docker ecosystem also parses `image:` keys out of YAML manifests
(Kubernetes / Compose). It may therefore open PRs against `docker-compose.yml`'s
`image: ghcr.io/kdknigga/saneless:latest` — i.e. against our own image. If that happens the
remedy is `ignore:` for that dependency, not removing the ecosystem. [ASSUMED — inferred from
dependabot-core's `IMAGE_SPEC` / `deep_fetch_images`; not observed]

---

## 5. Blockers requiring the user's accounts — exact requirements

The planner must state each as a **prerequisite**, not a task, and sequence criterion 2 and
criterion 5 behind them.

| # | Blocker | Exactly what the user does | Verifiable by Claude? |
|---|---------|---------------------------|----------------------|
| 1 | **Pending publisher on PyPI** | Account sidebar → Publishing → "Add a new pending publisher" (it is under the **account**, not a project, because the project does not exist yet). Fields: PyPI Project Name `saneless`, Owner `kdknigga`, Repository name `saneless`, Workflow name `release.yml`, Environment name `pypi` | No — only by a successful publish |
| 2 | **Pending publisher on TestPyPI** | Same five fields, environment `testpypi`, on test.pypi.org | No |
| 3 | **Two GitHub environments** | Settings → Environments → New environment `testpypi` (no rules) and `pypi` (**Required reviewers** rule per D-18) | No |
| 4 | **GHCR package visibility** | After the first successful push, package page → gear icon → Danger Zone → Change visibility → Public. **One-way: a public package cannot be made private again** [CITED: docs.github.com/en/packages/…/configuring-a-packages-access-control-and-visibility] | No — but a failing anonymous `docker pull` is the symptom |
| 5 | **GitHub Pages** | See §11 — the recommended docs workflow changes this from "serve the gh-pages branch" to Settings → Pages → **Source: GitHub Actions** | No |
| 6 | **Tag pushes** | `git tag v0.2.0-rc.1 && git push origin v0.2.0-rc.1`, then later `v0.2.0`. D-20: the user pushes every tag | No |

**Name availability re-verified 2026-09-18:** both `https://pypi.org/pypi/saneless/json` and
`https://test.pypi.org/pypi/saneless/json` return **404** [VERIFIED: executed]. A pending
publisher does **not** reserve the name until first publish — *"if another user registers the
project name before you actually publish to it, your 'pending' publisher will be invalidated"*
[CITED: docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/]. That is an argument
for doing the TestPyPI rehearsal promptly, and for not deferring the real PyPI publish
indefinitely.

**Sequencing consequence:** blockers 1–3 must be done *before* the RC tag is pushed, or the RC
run fails at the publish step with an OIDC error and has to be re-tagged (`v0.2.0-rc.2`) —
a version number on an index can never be reused.

---

## 6. Container mechanics (D-25..D-31, DLVR-05/06/07)

### 6.1 Volume ownership — measured

Built a non-root test image (`useradd 1000`, `mkdir -p /var/lib/saneless`, `chown 1000:1000`,
`VOLUME`, `WORKDIR`, `USER`) and ran three shapes [VERIFIED: executed with podman/buildah 1.39.4]:

| Mount | Owner seen inside | Write by UID 1000 |
|-------|-------------------|-------------------|
| Fresh **named** volume `-v vt1:/var/lib/saneless` | `1000:1000` | **OK** |
| **Anonymous** volume (bare `docker run`, from the `VOLUME` directive) | `1000:1000` | **OK** |
| **Bind** mount of a host directory | host's own ownership, unchanged | depends on the host directory |

**D-28's premise is confirmed:** a named or anonymous volume inherits the image directory's
ownership on first use; a bind mount does not. Hence D-26's documentation-plus-commented-line
approach for operators whose UID is not 1000.

### 6.2 `VOLUME` / `chown` ordering

Tested `chown` **after** `VOLUME` as well: under buildah the ownership still took effect. **Do
not rely on this.** Docker's own documented behaviour is that build steps changing data within
a declared volume path *after* the `VOLUME` instruction are discarded. A local podman test would
therefore pass while the real image (built by `docker/build-push-action` with BuildKit) could
fail. **Keep `mkdir` + `chown` strictly before `VOLUME`**, as D-28 says. [VERIFIED that buildah
tolerates the wrong order; ASSUMED that BuildKit does not — do not test-drive this ordering]

### 6.3 Recommended runtime-stage shape

```dockerfile
FROM ghcr.io/astral-sh/uv:0.10.3@sha256:7a88d4c4e6f44200575000638453a5a381db0ae31ad5c3a51b14f8687c9d93a3 AS uv

FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS builder  # 3.14-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv build --wheel --out-dir /dist

FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6  # 3.14-slim
# … apt, pip install of the wheel — still as root, before USER …
RUN groupadd --gid 1000 saneless \
 && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin saneless \
 && mkdir -p /var/lib/saneless \
 && chown 1000:1000 /var/lib/saneless
ENV SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless
VOLUME ["/var/lib/saneless"]
WORKDIR /var/lib/saneless
USER saneless
EXPOSE 8080
HEALTHCHECK … CMD curl -f http://localhost:8080/health || exit 1
ENTRYPOINT ["saneless"]
CMD ["serve"]
```

Digests resolved 2026-09-18 [VERIFIED: Docker Hub v2 API for python; `podman pull` +
`RepoDigests` for uv]. **Re-resolve at implementation time** — these drift.

**The `FROM … AS uv` promotion is not cosmetic.** Dependabot's Docker file parser iterates
`next unless FROM_LINE.match?(line)` — it **only** matches `FROM` directives and *"deliberately
excludes builder stage references like `COPY --from=`"* [VERIFIED: read
dependabot-core/docker/lib/dependabot/docker/file_parser.rb]. Leaving uv on a `COPY --from=`
line means D-27's third digest pin gets no Dependabot PRs and rots. Promoting it to a named
`FROM` stage costs one line and makes the pin maintained.

**Answers to the specific questions in the brief:**

- **Does `HEALTHCHECK`'s `curl` still work as non-root?** Yes. `curl` to `localhost:8080`
  needs no privilege, and 8080 is above 1024 so the non-root process can bind it.
- **Does the `pip install` of the wheel still work?** Yes — it runs before `USER`, so it is
  still root writing to system site-packages. Confirmed by the ordering above; the existing
  Dockerfile already has the install at lines 13–18, well before any `USER` would be added.
- **Digest-pin syntax with version comments:** `image:tag@sha256:…  # tag` — the tag must stay
  in the reference (not only in the comment) for Dependabot to track and bump it.

### 6.4 `.dockerignore` allow-list (D-29) — measured

Built a probe image with exactly D-29's starting set and listed the context that arrived
[VERIFIED: executed]:

```
*
!src/
!pyproject.toml
!uv.lock
!README.md
!LICENSE
```

Context received: `LICENSE`, `README.md`, `pyproject.toml`, `src/saneless/cli.py`,
`src/saneless/web/templates/index.html`, `uv.lock`. **Excluded:** `config/config.toml`,
`saneless.toml`, `tests/`, `.planning/`.

**The directory-recursion gotcha does not bite here:** `!src/` re-includes the directory *and
everything beneath it* — `src/saneless/web/templates/index.html` arrived. `!src/**` is not
needed. [VERIFIED on buildah; Docker BuildKit uses the same `moby/patternmatcher` library, so
behaviour is expected identical]

**The allow-list is also sufficient for the real build.** Copied exactly those five paths from
the repo into a clean directory, applied the D-05 pyproject changes, and ran
`uv build --wheel` — success, and the wheel carried `src/saneless/web/static/vendor/*`,
`templates/index.html` and `dist-info/licenses/LICENSE` [VERIFIED: executed].

**Two traps confirmed live** (D-29 names them; both are real):
- `readme = "README.md"` → excluding README.md fails the build.
- `license-files = ["LICENSE"]` → excluding LICENSE fails the build.

`uv.lock` is **not** required by `uv build --wheel` (the build succeeded from the minimal
context regardless), but keeping it costs nothing and makes a future `uv sync --locked` in the
builder stage possible.

### 6.5 `.gitignore` `*.png` (D-33) — the complete enumeration

`.gitignore:307` is the blanket `*.png`. Asked git what each candidate PNG path is ignored *by*
[VERIFIED: `git check-ignore -v` executed]:

| Path | Ignored by | Survives removing `*.png`? |
|------|-----------|---------------------------|
| `site/assets/images/favicon.png` | `.gitignore:207` → `/site` | Yes |
| `.planning/ui-reviews/**/*.png` | `.planning/ui-reviews/.gitignore:2` → `*.png` (its own file) | Yes |
| `.playwright-mcp/*.png` | `.gitignore:310` → `.playwright-mcp/` | Yes |
| `test-results/*.png` | **`.gitignore:307` → `*.png` only** | **No** |

`test-results/` is pytest-playwright's default artifact directory
(`default="test-results"`, *"Directory for artifacts produced by tests"*)
[VERIFIED: read `.venv/…/pytest_playwright/pytest_playwright.py:463`]. It holds failure
screenshots, videos and trace zips — `.png`, `.webm`, `.zip` — so the correct replacement is
the **directory**, not a narrower glob.

**Recommendation for D-33:** delete `.gitignore:307` `*.png`; add `/test-results/`. Nothing
else currently depends on the blanket glob. No tracked file is affected (there are no tracked
`.png` files anywhere in the repo).

---

## 7. The logging mode split (D-34..D-40, DLVR-04)

### The seam

`_load_cli_settings` (note: the function is `_load_cli_settings`, **not** `_load_settings` —
the brief and CONTEXT both use the shorter name) at `src/saneless/cli.py:506-555` is the single
call site of `configure_logging` in the whole codebase [VERIFIED: grep across `src/` and
`tests/`]. It is memoised on `ctx.obj["settings"]` and called lazily by each command.

The only caller that needs the stream is `serve` at `cli.py:858`.

**Recommended plumbing** (Claude's discretion, but this is the shape with the fewest edges):

```python
def _load_cli_settings(ctx: click.Context, *, stream_logs: bool = False) -> Settings:
```

- `serve` calls `_load_cli_settings(ctx, stream_logs=True)`; every other command is textually
  unchanged, satisfying D-34's literal "no changes here at all".
- `configure_logging` gains a keyword (`stream: bool = False`) whose default preserves today's
  behaviour, so all 16 `tests/test_logging.py` tests keep passing unmodified.
- In stream mode `configure_logging` returns `False` (nothing attached to a *file*), which
  makes `ctx.obj["log_file"] = settings.output.log_file if attached else None` evaluate to
  `None` with **no change to that line** — satisfying the discretion constraint that nothing
  prints `"Full details in …"` in serve mode.

**Why not `ctx.command.name` / `invoked_subcommand`:** `_load_cli_settings` receives the
*command's* context, where `invoked_subcommand` is `None`. `ctx.command.name` would work
(`"serve"`), and it would still behave correctly in the one test that calls the function
directly (`test_settings_loaded_and_logging_configured_once`, `tests/test_cli.py:1610-1625`,
which builds `click.Context(cli, …)` — so `ctx.command.name` is `"cli"` and it falls into CLI
mode). But it couples the configurator to click's command tree for no gain. The explicit
keyword is cleaner and equally test-safe.

### What the stream handler must be

- `StreamHandler(sys.stderr)` (D-36), root level = configured `log_level`.
- Non-verbose → `_TracebackFreeFormatter(_FORMAT)`; verbose → plain `Formatter(_FORMAT)` as the
  mirror already is. `_TracebackFreeFormatter` is already written and tested
  (`logging_config.py:22-49`, `tests/test_logging.py:278,302`).
- No `RotatingFileHandler`, no directory creation, no `"Cannot write to …"` warning.

### D-37 verified in uvicorn's source

Read `.venv/…/uvicorn/config.py:364-400` [VERIFIED]:

```python
def configure_logging(self) -> None:
    logging.addLevelName(TRACE_LOG_LEVEL, "TRACE")
    if self.log_config is not None:
        … dictConfig / fileConfig …
    if self.log_level is not None:
        logging.getLogger("uvicorn.error").setLevel(log_level)
        logging.getLogger("uvicorn.access").setLevel(log_level)
        logging.getLogger("uvicorn.asgi").setLevel(log_level)
    if self.access_log is False:
        logging.getLogger("uvicorn.access").handlers = []
        logging.getLogger("uvicorn.access").propagate = False
```

With `log_config=None` **no `dictConfig` runs**, so uvicorn attaches no handlers of its own;
its three loggers keep the default `propagate=True` and their records reach whatever the root
handlers are. `access_log=True` leaves `uvicorn.access` propagating. **D-37's "zero extra
code" is exactly right.** A consequence worth stating in the plan: uvicorn's own startup lines
(`Started server process`, `Application startup complete`, `Uvicorn running on …`) will now
appear on stderr too — which is what DLVR-04 wants.

`-v` is unchanged: `logging.getLogger("saneless").setLevel(DEBUG)` only, root stays at the
configured level, so httpx never reaches DEBUG (T-27-23). No code change needed for D-38.

### Every test that would need to change — and every test that must not

**Must NOT change (Phase 28's contract, D-34):**

| Test | File:line | Why it is safe |
|------|-----------|----------------|
| All 16 `TestConfigureLogging` tests | `tests/test_logging.py:51-319` | Call `configure_logging` directly with today's signature; a defaulted new keyword leaves them untouched |
| `test_stderr_log_fallback_prints_no_traceback_without_verbose` (parametrised) | `tests/test_cli.py:3125-3160` | Patches in the **real** `configure_logging` and invokes `scan` — CLI mode |
| `test_stderr_log_fallback_with_verbose_prints_the_traceback` | `tests/test_cli.py:3163-3190` | Same; `scan` |
| `test_unwritable_log_file_falls_back_to_stderr_and_runs` | `tests/test_cli.py:1565-1585` | Real config, `jobs` — CLI mode |
| Everything asserting `"Full details in …"` / `_VERBOSE_HINT` | `tests/test_cli.py:3004,3037,3062,3247` | All invoke `scan` |

**Likely to need attention (all invoke `serve`):**

| Test | File:line | Risk |
|------|-----------|------|
| `TestServeCommand.*` (7 tests) | `tests/test_cli.py:2024-2125` | Use `_patch_cli`, which stubs `configure_logging` with `lambda *_args, **_kwargs: None` — a new keyword is absorbed. **Should pass unchanged**, but `test_serve_calls_uvicorn_defaults` asserts `log_config is None` and `access_log is True`, which D-37 preserves |
| `test_subcommand_help_needs_no_config[serve]` | `tests/test_cli.py:1485-1510` | `recording_logging(*_args, **_kwargs)` — absorbs the keyword. Safe |
| `test_help_with_broken_real_config_creates_no_log_dir` | `tests/test_cli.py:1507-1520` | Asserts **no log dir is created** for `serve --help`. Under the stream mode this becomes trivially true; the test still passes |
| `test_verbose_flag` | `tests/test_cli.py:1306-1340` | `capture_logging(*_args, **kwargs)` reads `kwargs["verbose"]`; invokes `devices`, not `serve`. Safe |
| `test_settings_loaded_and_logging_configured_once` | `tests/test_cli.py:1610-1625` | Calls `_load_cli_settings(ctx)` positionally with one argument — a keyword-only new parameter with a default keeps this green |

**New tests the mode split needs** (none of these exist today):
1. `serve` attaches no `RotatingFileHandler` and creates no log directory.
2. `serve` attaches exactly one `StreamHandler` to stderr at the configured level.
3. `ctx.obj["log_file"]` is `None` after `serve` loads settings.
4. Without `-v`, a record with `exc_info` renders on the serve stream **without** a traceback.
5. With `-v`, it renders **with** one.
6. A one-shot command (`jobs`) still attaches the file handler — the regression guard for D-34.

**`cli.py:917`'s wording** (Claude's discretion): the message is
`"…(uvicorn exit status {exc.code}); the cause is in the log"`. In serve mode "the log" is now
stderr, which is where that very line is also printed — so a reader is told to look at the
place they are already looking. Suggested rewrite: `"; the cause is above"` or `"; see the
preceding log lines"`. It is a one-string change with a doc-truth test in
`tests/test_cli.py` (`test_serve_uvicorn_startup_failure_exits_2_not_3`) that asserts the
line — check that test before editing the string.

---

## 8. The naming guard (D-08..D-12, CI-02, DLVR-01)

### Current inventory — verified 2026-09-18

`git ls-files` minus `.planning/` → **119 files**; `kris-knigga` appears on **29 lines in 10
files**. Splitting out the test file's five Phase-30 artifacts leaves **24 lines in nine
files** — exactly matching DLVR-01's count.

**The 24 real references (this phase renames every one):**

```
README.md:37,72,74,75,76,77                       (6)
docker-compose.yml:15,25                          (2)
docs/getting-started/first-cli-scan.md:38         (1)
docs/getting-started/quick-start.md:21            (1)
docs/how-to/deploy-docker-compose.md:57,88        (2)
docs/how-to/scanner-host-discovery.md:35          (1)
docs/reference/docker.md:9,141,163,181,199        (5)
mkdocs.yml:3,4,5                                  (3)
pyproject.toml:42,43,44                           (3)
```

**The five Phase-30 artifacts (D-12 deletes these, not renames them):**

```
tests/test_deployment_config.py:28    module docstring sentence about pinning the count
tests/test_deployment_config.py:781   COMPOSE_KRIS_KNIGGA_COUNT comment
tests/test_deployment_config.py:921-924  test_phase_30_does_not_perform_the_dlvr_01_rename
```

`COMPOSE_KRIS_KNIGGA_COUNT = 2` is at line 787 (its comment block starts at 781). The test
body runs 920–927. The module docstring sentence at line 28 must go too, or the guard fires
on the test file's own docstring.

**Per-category breakdown (code vs docs vs test):**

| Category | Files | Lines |
|----------|-------|-------|
| Docs (published) | 5 `docs/` pages + `README.md` | 16 |
| Config / build | `pyproject.toml`, `mkdocs.yml`, `docker-compose.yml` | 8 |
| Test (deleted, not renamed) | `tests/test_deployment_config.py` | 5 |
| Source (`src/`) | **none** | 0 |

No Python source file references the name. `src/` is untouched by DLVR-01.

### Two ruff blockers found — design around them, do not suppress

**Blocker A — `S603` on `subprocess.run`.** A naive guard fails ruff:

```
S603 `subprocess` call: check for execution of untrusted input
  --> tests/test_probe_guard.py:14:14
```

[VERIFIED: ruff 0.15.x run with this project's exact `[tool.ruff]` config, including its
`tests/**/*.py` per-file-ignores — which do **not** include `S`'s subprocess rules]

**The repo already solved this.** `tests/test_scanner.py:2627`, `tests/test_atomic_write.py:684,711`
and `tests/test_pdf.py:726` all call `subprocess.run` and pass ruff, because **every argv
element is a string literal** and per-run values travel in the environment or in non-argv
keywords. `test_scanner.py` even documents the rule in a comment. Applying it:

```python
result = subprocess.run(
    ["/usr/bin/git", "ls-files", "-z"],
    cwd=REPO_ROOT,          # a Path keyword, not an argv element
    capture_output=True,
    text=True,
    check=True,
    timeout=30,
)
```

[VERIFIED: `ruff check` → `All checks passed!`, `ruff format --check` → already formatted, and
the call itself returns 741 tracked paths from the real repo]

Note `cwd=` rather than `-C <path>`: putting `str(REPO_ROOT)` into argv is what triggers S603.

**Blocker B — `FLY002` rewrites the runtime assembly into a literal.** D-11 requires the
forbidden string never to appear literally in the guard's own source. The obvious spelling
defeats itself:

```
FLY002 Consider `"kris-knigga"` instead of string join
  --> forbidden = "-".join(("kris", "knigga"))
help: Replace with `"kris-knigga"`
```

Three alternatives were linted; **all three pass** [VERIFIED]:

```python
_OWNER_GIVEN = "kris"
_OWNER_FAMILY = "knigga"
FORBIDDEN_SLUG = f"{_OWNER_GIVEN}-{_OWNER_FAMILY}"   # recommended
# also clean:
ALT = _OWNER_GIVEN + "-" + _OWNER_FAMILY
_PARTS = ("kris", "knigga"); ALT3 = "-".join(_PARTS)   # named constant, not an inline tuple
```

FLY002 only fires on `str.join` with an **inline** literal sequence. The f-string form is the
clearest and needs the D-11 comment explaining why it is written that way.

### Working guard prototype

```python
GIT = "/usr/bin/git"
_EXCLUDED_PREFIX = ".planning/"


def _shipped_files() -> list[str]:
    """Return every tracked path outside ``.planning/`` (DLVR-01, D-10)."""
    result = subprocess.run(
        [GIT, "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return [
        name
        for name in result.stdout.split("\0")
        if name and not name.startswith(_EXCLUDED_PREFIX)
    ]
```

Run against the real repo it produces the 29-line offender list reproduced above, with
`file:line` for each — exactly the failure message D-08 asks for.

**Practical notes:**

- `site/` is gitignored (`.gitignore:207` → `/site`), so it never reaches `git ls-files` —
  the requirement's second exclusion is free, as D-10 says [VERIFIED].
- All 119 files decode as UTF-8 today, but defensive `try/except (UnicodeDecodeError, OSError)`
  is worth having so a future favicon does not break the guard.
- `/usr/bin/git` is correct on this host and on `ubuntu-latest`. A worktree under
  `.claude/worktrees/` is a real git worktree, so `git ls-files` works there.
- `actions/checkout` with `persist-credentials: false` still produces a full `.git`, so the
  guard works in CI.

### D-08's third enforcement point does not exist

D-08 says the guard runs "in CI, in prek's push stage, and on a plain `uv run pytest` — three
enforcement points". **There is no pytest hook in `.pre-commit-config.yaml`** — `grep -c pytest`
returns 0 [VERIFIED]. The push stage runs ty, pyrefly, `ruff check --no-fix`, and
`ruff format --check`, nothing else.

The planner must pick one: add a pytest (or targeted `pytest -k`) hook to the `repo: local`
block at `stages: [pre-merge-commit, pre-push]`, or record that the guard has two enforcement
points. Either satisfies CI-02; only the first satisfies D-08 as written. Adding a full
`uv run pytest -m "not browser"` push hook costs ~27 seconds per push, which is the honest
price.

### Sequencing (Claude's discretion, but the constraint is real)

`tests/test_deployment_config.py` simultaneously (a) contains the old name five times,
(b) contains the Phase-30 test that *requires* the compose file to still say `kris-knigga`, and
(c) is where the new guard lives. Landing the rename, the D-12 deletions and the guard in
**one commit** is the only ordering where the suite is green at every commit. Under TDD mode
the RED step is the guard test alone (it fires on 29 offenders); the GREEN step is the rename
plus the deletions. Commit-stage prek only type-checks `src/`, so a RED commit is permitted
(CLAUDE.md gotcha) — but the *full* check at pre-push means the pair must be squashed or the
push must come after GREEN.

---

## 9. The 34-row audit (D-41..D-45, DOCS-01)

Every row below was re-checked against the shipped tree on **2026-09-18**. Classification:
**(a)** corrected by Phases 21–30; **(b)** still false, fixable by editing a sentence;
**(c)** still false and would need new behaviour (→ D-43 escalation); **(d)** this phase's own work.

| # | Where (current) | Status | Evidence |
|---|-----------------|--------|----------|
| 1 | `docs/how-to/set-up-adf-duplex.md:80-95` | **(a)** corrected | The prompt now exists; page describes CLI confirm and Web UI Continue/Abort |
| 2 | same page:47,105 | **(a)** corrected | "Manual Duplex" source gone; `duplex = "manual"` profile field is the mechanism |
| 3 | `docs/explanation/consume-directory-fallback.md:90-105` | **(a)** corrected | `FALLBACK` is a real `JobState` (Phase 23) and the page describes it accurately |
| 4 | `docs/getting-started/first-web-ui-scan.md:54` | **(a)** corrected | "Done" now means success only; FALLBACK renders "Saved to folder" (line 75) |
| 5 | `docs/reference/environment-variables.md` | **(a)** corrected | The false sentence is gone from the section (lines 55-63 checked) |
| 6 | `docs/reference/configuration.md:111`, `configure-scan-profiles.md:62`, `saneless.toml.example:21` | **(a)** corrected | `title` is read by `resolve_job_title` (Phase 25 D-16); all three now say "used when the title is left blank" |
| 7 | `docs/how-to/configure-scan-profiles.md:~138` | **(a)** corrected | Now documents source-derived slugs (`flatbed`, `automatic-document-feeder`) |
| 8 | same page:150-165 | **(a)** corrected (spot-checked in discussion) | `--force` merge semantics documented accurately |
| 9 | same page:~130-135 | **(a)** corrected | Names the three conditions and says saneless "crops the image after scanning instead, and logs which of the three happened" |
| 10 | same page (thresholds section) | **(a)** corrected | The inverted "lower the thresholds" example is gone |
| 11 | `docs/explanation/empty-page-detection.md:54` | **(a)** corrected | Now `saneless -v scan ...` or `log_level = "DEBUG"` |
| 12 | same page:56-70 | **(a)** corrected | Disabling section no longer overclaims |
| 13 | `docs/reference/cli-commands.md:12` | **(a)** corrected | `-v` described as "Log saneless's own debug detail (DEBUG) to the log file and mirror it to stderr; other libraries and the web server keep the configured `log_level`" |
| 14 | `docs/how-to/cli-scripting.md:82` | **(a)** corrected | Exit 2 row explicitly names "a `--config` file that does not exist" |
| 15 | `docs/how-to/install-bare-metal.md:47` | **(d) this phase** | `saneless --version` → `Error: No such option: --version`, exit 2 [VERIFIED: executed]. Fixed by DLVR-10; the doc sentence needs no edit |
| 16 | `README.md:44` | **(b) still false** | `saneless scan             # Scan a document` — `--title` is not shown. (Note: `--title` is now *optional* with a resolved default, so the claim is arguably true; DOCS-02 still asks the example to carry it) |
| 17 | `README.md:63` | **(b) still false** | `source = "flatbed"`; the comparison is case-sensitive and SANE spells it `"Flatbed"` — `saneless.toml.example:16` already uses `"Flatbed"` |
| 18 | `README.md:74` | **(b) still false** | Links to `…/tutorials/scan-your-first-document/`; the page is `getting-started/first-cli-scan` |
| 19 | 24 lines in 9 files | **(d) this phase** | Full inventory in §8 |
| 20 | `docs/how-to/deploy-docker-compose.md:31` | **(a) corrected** | The note now says "the container still starts; it does not create the file for you" |
| 21 | `saneless.toml.example:11`, `docs/reference/docker.md:111` | **(b) still false** | `web_port = 8081          # change if 8080 is taken` is **live** in the shipped example while `EXPOSE`/`HEALTHCHECK` are fixed at 8080. `docker.md:111` still describes `SANELESS_OUTPUT__WEB_PORT` as "Override web server port" with no Docker caveat. D-31/D-32 fix both |
| 22 | `docs/reference/web-api.md:236-265` | **(a) corrected** | Both flip endpoints now describe the acknowledgment partial precisely |
| 23 | `docs/reference/web-api.md:88-97` | **(a) corrected** | 429/503 documented with `Retry-After: 30` |
| 24 | `docs/getting-started/first-web-ui-scan.md` | **(a) corrected** | No "Cancel", no "face-up" anywhere on the page |
| 25 | `docs/explanation/architecture.md:67` | **(a) corrected** | Explicitly: "An upload paperless-ngx rejects (a 4xx…) … never falls back" |
| 26 | `docs/explanation/architecture.md` | **(a) corrected** | No "responsive" claim remains anywhere on the page |
| 27 | `docs/reference/configuration.md:11,60,61` | **(a) corrected** | `$XDG_CONFIG_HOME` / `$XDG_STATE_HOME` named, with the `~/…` fallbacks in parentheses |
| 28 | `docs/explanation/consume-directory-fallback.md:11` | **(a) corrected** | "up to 3 times" without the false "by default". Source still `max_retries: int = 3` and `time.sleep(2**attempt)` (`paperless.py:455,707`) |
| 29 | four statements, see §10 | **(d) this phase** | All four still live and mutually contradictory |
| 30 | `docs/how-to/cli-scripting.md:45` | **(b) still false** | `"id": "a1b2c3d4"` — ids are UUID4. `created_at` **did** gain `+00:00` (line 49) |
| 31 | `docs/getting-started/quick-start.md:19` | **(b) still false** | `-v ./config:/etc/saneless` — needs `$(pwd)/config` |
| 32 | `docs/how-to/deploy-docker-compose.md:34-48` | **(b) still false** | Paperless service with only `PAPERLESS_SECRET_KEY`, no Redis broker. D-49 replaces it with a pointer |
| 33 | `src/saneless/job.py:1-8` | **(a) corrected** | Docstring now says "A job survives the process that ran it only as a row: on startup the web app fails every job still in an active state through `fail_active_jobs`" |
| 34 | `docs/PRD.md` | **(d) this phase** | File present (322 lines, 22 KB). `mkdocs build --strict` reports: *"The following pages exist in the docs directory, but are not included in the nav configuration: PRD.md"* — confirming it is built and published. D-46 moves it |

**Summary:** 23 rows corrected by Phases 21–30 (a); **7 rows still false and fixable by
sentence edits** (16, 17, 18, 21, 30, 31, 32); **4 rows are this phase's own work** (15, 19, 29,
34). **Zero rows fall into category (c)** — D-43's escalation clause is not expected to fire.

CONTEXT.md's "rough row ownership" guess was close but not exact: it assigned rows 16, 17, 18
and 19 to this phase (correct), and rows 20, 22–28 to Phases 21–30 (correct), but predicted
rows 29, 30, 31, 32 and 34 as this phase's own — they are, in the sense that nothing corrected
them, which is the same outcome by a different route.

### D-44: re-checking the "correct" list

~20 claims were verified 2026-09-09. Twelve phases later, re-checked 2026-09-18:

| Claim | Now | Evidence |
|-------|-----|----------|
| "`scan` exit codes 1, 2 and 3 for the cases that are caught" | **Superseded, doc is correct** | Phase 28 replaced the table with 0/1/2/3/4/5/130. Both `docs/how-to/cli-scripting.md:78-88` and `docs/reference/cli-commands.md` carry the full table, pinned by Phase 28's `ExitCode`-derived tests. The 2026-09-09 *claim* is stale; the *docs* are true and defended |
| "all eleven routes and methods in `web-api.md`" | **Superseded, doc is correct** | Source now has **15** route decorators; `web-api.md`'s table lists 14 plus `GET /` documented in detail = 15. Phase 30 added a test deriving expectations from the route decorators, so a future addition cannot ship undocumented |
| "three upload attempts with `2**attempt` backoff" | **Still true** | `paperless.py:455` `max_retries: int = 3`; `paperless.py:707` `time.sleep(2**attempt)` |
| "`jobs --limit` default 20" | **Still true** | `cli.py:784` `@click.option("--limit", default=20, …)` |
| "`serve` defaults of `0.0.0.0:8080` and the no-auth note" | **Still true** | `OutputConfig` defaults; `docs/reference/web-api.md:316` carries the no-auth note |
| "`/health` 200 and 503 bodies" | **Still true** | `docs/reference/docker.md:23` names both plus the two 503 reasons |
| "`SANE_NET_HOSTS` precedence rule" | **Still true** | `scanner/sane_backend.py:876-882` — config host is used only when the env var is absent; otherwise a warning |
| "PNG-then-img2pdf lossless assembly" | **Still true, defended** | `tests/test_deployment_config.py:759-775` pins it and bans the "byte-for-byte" and "PIL Images" overclaims |
| "dual-threshold empty-page rule" | **Still true** | Unchanged since Phase 21 |
| "`[output]`/`[profiles]` defaults, config search order, env prefix, precedence, `default` profile requirement, `auto_source_mode`/`paper_size` literals, `/api/paperless/test` values, `/api/scan` form fields" | **Still true** | All pinned by existing tests in `tests/test_deployment_config.py` (Phases 27–30) |

**Two of the three claims D-44 singled out as at-risk turned out to be stale *claims* about
docs that are now correct.** The audit artifact should record them as "claim superseded; the
documentation is correct and now pinned by <test name>", which is a materially different
disposition from "still correct" and is exactly what D-41 means by being honest about what is
permanently defended.

### D-45: pinning README examples statically

Three assertions in the existing harness:
1. The `saneless scan` block in `README.md` contains `--title`.
2. Every `source = "…"` value in `README.md` is a member of the set of real SANE spellings
   (derive from the same constant the source classifier uses, so the expectation cannot drift).
3. The tutorial URL in `README.md:74` ends in a path that resolves to an existing file under
   `docs/` (`getting-started/first-cli-scan.md`).

Assertion 3 generalises usefully: check that **every** `kdknigga.github.io/saneless/<path>/`
link in `README.md` maps to an existing `docs/<path>.md`.

---

## 10. Doc surfaces (D-46..D-50, DOCS-03..DOCS-06)

### Row 29 — the four USB statements, verified live

```
docs/getting-started/first-cli-scan.md:9   "connected via USB or network, with `saned` running on the machine that has the scanner attached"
docs/getting-started/first-cli-scan.md:67  "**USB scanners** must be connected to the machine running `saned`, not the machine running saneless"
docs/how-to/install-bare-metal.md:9        "accessible via `saned` on the network or locally via USB"
docs/reference/configuration.md:38         "SANE net host IP/hostname. Empty = local USB."
docs/reference/docker.md:156-170           "## USB Scanner Access … pass the USB bus:" with `devices: - /dev/bus/usb:/dev/bus/usb`
docs/how-to/scanner-host-discovery.md:13   "Docker containers cannot access USB scanners attached to the host."
```

Under D-48's one rule (bare metal enumerates local USB via libsane; the container **always**
uses the SANE network protocol), the consistent set is:

- `first-cli-scan.md:9` — remove "connected via USB or network" framing; say the scanner is
  reached either locally (bare metal) or over `saned` (always, for containers).
- `first-cli-scan.md:67` — change "must be connected to the machine running `saned`" to the
  container-scoped statement; bare metal does not need `saned` at all.
- `install-bare-metal.md:9` — **already correct** under D-48 ("via `saned` on the network or
  locally via USB" is exactly the bare-metal rule). No edit.
- `configuration.md:38` — "Empty = local USB" is **already correct** for bare metal; it needs
  the Docker caveat added ("in a container, leave this set — the container cannot see local USB").
- `docker.md:156-170` — **delete the whole "USB Scanner Access" section** (D-48).
- `scanner-host-discovery.md:13` — **already correct**; it becomes the canonical statement.

### D-47's three deployment shapes, with exact lines

The "Which setup do I have?" page must produce a reader who knows which of these three they
are. Consistent with D-48 and with `docker-compose.yml` as it stands:

**Shape 1 — Bare metal, scanner on this machine (USB or network).**
Only shape where libsane enumerates local USB directly.
```bash
pip install saneless          # after installing libsane-dev
saneless devices
```
*How to tell:* `scanimage -L` on this machine lists your scanner, and you are not using Docker.
No `scanner.host`, no `saned`.

**Shape 2 — Container, scanner attached to the container's own host.**
The container cannot see `/dev/bus/usb`; it reaches the host's `saned` over the network.
```yaml
services:
  saneless:
    image: ghcr.io/kdknigga/saneless:latest
    ports: ["8080:8080"]
    volumes:
      - ./config:/etc/saneless
      - saneless-data:/var/lib/saneless
    environment:
      - TZ=America/Chicago
      - SANELESS_SCANNER__HOST=host.docker.internal   # or the host's LAN IP
```
*How to tell:* the scanner's USB cable goes into the same box that runs Docker. You still need
`saned` running on that box, and `SANELESS_SCANNER__HOST` pointing at it.

**Shape 3 — Container, scanner attached to a different machine (or a network scanner).**
```yaml
    environment:
      - SANELESS_SCANNER__HOST=192.168.1.50
```
*How to tell:* the scanner is plugged into another machine running `saned`, or it speaks SANE
over the network itself.

The `docker run` equivalents must use `$(pwd)` per row 31:
```bash
docker run -p 8080:8080 \
  -v "$(pwd)/config:/etc/saneless" \
  -v saneless-data:/var/lib/saneless \
  -e SANELESS_SCANNER__HOST=192.168.1.50 \
  ghcr.io/kdknigga/saneless:latest
```

Nav insertion (`mkdocs.yml:40-43`), D-47 puts it first:
```yaml
  - Getting Started:
    - Which setup do I have?: getting-started/which-setup.md
    - Quick Start: getting-started/quick-start.md
```

### D-50 — link targets, exact line numbers

| Target | Location |
|--------|----------|
| "There is no authentication on the API. saneless assumes a trusted LAN; use a reverse proxy for auth if needed. The web server binds to `web_host`, which defaults to `0.0.0.0` — all network interfaces…" | `docs/reference/web-api.md:316` |
| "## Running behind a reverse proxy" | `docs/how-to/deploy-docker-compose.md:136` |

The one sentence goes into `docs/getting-started/quick-start.md`'s Prerequisites block
(lines 5–9) and onto `docs/reference/docker.md` (the Image table at lines 7–12 is the natural
anchor). Both link to the two targets above. No new page (D-50).

### D-46 — moving `docs/PRD.md`

`mkdocs build --strict` today **passes** and logs `PRD.md` as built-but-not-in-nav
[VERIFIED: executed]. Moving it to `.planning/milestones/v1.0-PRD.md` (alongside
`v1.0-ROADMAP.md`, as D-46 suggests) removes a 322-line page from the published site and from
the audit surface. No `mkdocs.yml` change is needed. Check for inbound links to `PRD.md` from
other pages before moving (a `--strict` build after the move is the check).

---

## 11. Decisions that need revisiting

### 11.1 D-13 + D-15 are in direct conflict over `mkdocs gh-deploy` (BLOCKING)

**Evidence.** With `mkdocs gh-deploy` retained and every other zizmor finding fixed, the audit
still reports [VERIFIED: executed]:

```
warning[artipacked]: credential persistence through GitHub Actions artifacts
  --> ./.github/workflows/docs.yml:13:9
   |   - uses: actions/checkout@3d3c42e5… does not set persist-credentials: false
```

`gh-deploy` pushes to `gh-pages` using the credentials `actions/checkout` persists into
`.git/config`. Setting `persist-credentials: false` removes them and the deploy fails.
D-15 forbids `# zizmor: ignore`. So **`gh-deploy` and a clean default-persona zizmor cannot
both be true.**

**The only suppression-free resolution** is the Pages-artifact flow. Verified to produce
**zero zizmor findings, exit 0** [VERIFIED: executed]:

```yaml
name: Docs
on:
  push:
    branches: [master]
permissions:
  contents: read
concurrency:
  group: pages
  cancel-in-progress: false
jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4 # v10.1.0
      - run: uv sync --locked --only-group dev --no-install-project
      - run: uv run mkdocs build --strict
      - uses: actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9 # v5.0.0
        with:
          path: site
  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    permissions:
      pages: write
      id-token: write
    steps:
      - uses: actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346 # v5.0.1
        id: deployment
```

**Consequences the user must decide on:**
- **Blocker #5 changes.** Not "Pages enabled serving the `gh-pages` branch" but
  **Settings → Pages → Source: GitHub Actions**. There is no `gh-pages` branch at all.
- A third GitHub environment, `github-pages`, is auto-created by GitHub. Harmless, but it
  means three environments exist, not the two D-18 names.
- `contents: write` disappears from `docs.yml` entirely — a genuine posture improvement.

**Bonus finding, in D-13's favour:** `uv sync --locked --only-group dev --no-install-project`
installs mkdocs-material **without** `python-sane`, so the docs job needs **no `libsane-dev`
apt step** [VERIFIED: `uv sync --dry-run` showed python-sane and the project being excluded].
A plain `uv sync --locked` in `docs.yml` would need the apt step that `ci.yml` has.

**If the user insists on `gh-deploy`,** the alternatives are all bad: run zizmor with
`--min-confidence medium` (weakens the contract project-wide), exclude `docs.yml` from the
audit (defeats the purpose), or accept `# zizmor: ignore` (forbidden by D-15 and CLAUDE.md).
Recommend the Pages-artifact flow.

### 11.2 D-08's "prek's push stage" enforcement point does not exist

See §8. Not blocking — CI-02 is satisfied by CI alone — but the plan should either add a
pytest push hook or restate D-08 as two enforcement points.

### 11.3 D-05's rationale is factually wrong (decision unaffected)

See §2. Neither `uv_build` nor `twine check` errors when a license classifier ships alongside
`License-Expression`. Remove the classifier anyway (PEP 639 deprecates it and DLVR-08 asks for
it), but pin the removal with a static assertion, because **no build step will catch a
regression**.

### 11.4 Consequence of "no traceback unless `-v`" on the serve stream (needs a decision)

The discretion note says `_TracebackFreeFormatter`'s rule "must still hold on the stream". In
CLI mode that rule is safe because the traceback still goes to the **log file**. In serve mode
**there is no file** (D-35, D-40). So a non-verbose `serve` would discard every traceback
entirely: an unexpected exception in the worker would leave one message line in `docker logs`
and nothing else, and the only way to recover a traceback is to restart the service with `-v`.

This may be exactly what the user wants — `docker logs` is arguably a user-facing surface and
the appliance framing supports terseness. But it is a real information loss that D-06 never had
to contemplate, and it is worth one sentence of confirmation before the plan locks it. The
alternative (full tracebacks on the serve stream, traceback-free only in CLI mode) is a
one-line difference in the formatter selection.

**Recommendation:** ask. If no answer, implement as the discretion note says (traceback-free
without `-v`) and record the consequence in the plan, since that is the literal reading.

### 11.5 Not a decision problem, but the verification environment is podman, not Docker

`docker` on this host is `/usr/bin/docker` emulating podman (buildah 1.39.4), and SELinux is
**Enforcing** [VERIFIED: `podman info`, `getenforce`]. Two consequences for D-21's criterion-2
verification:

- **Rootless podman maps host UID 1000 to container UID 0.** A bind mount of a host directory
  owned by `1000:1000` appears as `0:0` inside, and container UID 1000 cannot write it. Adding
  `--userns=keep-id:uid=1000,gid=1000` restores the Docker-equivalent mapping [VERIFIED].
- **SELinux blocks the bind mount regardless of UID** until the mount carries `:z` or `:Z`.
  With `--userns=keep-id` **and** `:Z`, the write succeeds [VERIFIED].

So a "`docker run --rm` on the pulled image with `./config` bind-mounted" verification on this
machine needs `--userns=keep-id:uid=1000,gid=1000` and `:Z`, or it will fail for reasons that
have nothing to do with the image. D-25 remains correct for actual Docker on a non-SELinux
host. The `pip install` half of criterion 2 (`docker run --rm python:3.14-slim`) is unaffected.

*(Out of scope but worth a note in the phase summary: the shipped `./config:/etc/saneless`
mount has never been documented with `:z`/`:Z`, so RHEL/Fedora/Rocky operators hit this today.
That is a pre-existing docs gap, not this phase's work.)*

---

## Don't Hand-Roll

| Problem | Don't build | Use instead | Why |
|---------|-------------|-------------|-----|
| Workflow supply-chain audit | A grep for `@v[0-9]` | `zizmor` | Five rule families, exit-code contract, auto-fixes; a grep finds one of the five |
| "Which files ship?" | A `.gitignore` parser or `Path.rglob` with exclusions | `git ls-files` (literal argv, `cwd=`) | git already answers it, and a new file is covered without anyone remembering (D-10) |
| Blocking `latest` on a pre-release | A hand-written `if` on the tag | `docker/metadata-action`'s default `flavor: latest=auto` | Already skips `latest` for semver pre-releases; a hand-written condition is one more thing that can be wrong |
| PEP 440 / semver normalisation | String munging between tag and version | `version = "0.2.0-rc.1"` + `uv_build` | uv normalises to `0.2.0rc1` in the filename; the same string is valid semver for metadata-action |
| Keeping base-image digests fresh | A cron that re-resolves | Dependabot `package-ecosystem: docker` + `FROM` stages | But only `FROM` lines — hence the uv promotion |
| Secret detection in the build context | A token-pattern hook | The `.dockerignore` allow-list + D-30's static test | Rejected by D-52: 40 hex chars collides with the SHA pins this phase adds |
| Uvicorn access-log routing | A custom log config dict | `log_config=None` (already there) | Verified: no dictConfig, records propagate to root |

---

## Common Pitfalls

### Pitfall 1: Pinning an annotated tag's object SHA
**What goes wrong:** `pypa/gh-action-pypi-publish@a892a5a6…` (the tag object) does not resolve
as an action ref.
**Why:** that repo uses annotated tags; the API's `/git/ref/tags/<tag>` returns `type: "tag"`,
and you must dereference to `type: "commit"`.
**Avoid:** always follow with `GET /git/tags/<sha>` when `type` is `tag`. Correct SHA here is
`dc37677b2e1c63e2034f94d8a5b11f265b73ba33`.

### Pitfall 2: An RC tag that `type=semver` cannot parse
**What goes wrong:** tagging `v0.2.0rc1` makes `docker/metadata-action` emit **no version tag
at all**, silently.
**Avoid:** `v0.2.0-rc.1`. Valid semver; `uv_build` normalises the package version to `0.2.0rc1`
for PyPI anyway.

### Pitfall 3: Dynamic `environment:` in string form
**What goes wrong:** `environment: ${{ … }}` fails with `Unrecognized named-value`.
**Avoid:** object form — `environment:\n  name: ${{ … }}`.

### Pitfall 4: `chown` after `VOLUME`
**What goes wrong:** works under buildah/podman, discarded under Docker. A local test passes
and the published image is broken.
**Avoid:** `mkdir` + `chown` strictly before `VOLUME`. Do not use a local podman build as
evidence that the ordering is safe.

### Pitfall 5: `COPY --from=<image>` is invisible to Dependabot
**What goes wrong:** the uv digest pin never gets a bump PR.
**Avoid:** `FROM ghcr.io/astral-sh/uv:… AS uv` then `COPY --from=uv /uv /usr/local/bin/uv`.

### Pitfall 6: `FLY002` un-does the runtime string assembly
**What goes wrong:** `ruff check --fix` rewrites `"-".join(("kris","knigga"))` into the literal,
putting the forbidden string into the guard's own source and making the guard fail on itself.
**Avoid:** f-string over named module constants.

### Pitfall 7: `S603` on a `git` subprocess with a non-literal argv element
**What goes wrong:** `str(REPO_ROOT)` inside the argv list trips S603, and suppression is
forbidden.
**Avoid:** literal argv only; pass the path as `cwd=`.

### Pitfall 8: `uv sync --locked` in the docs job pulls `python-sane`
**What goes wrong:** the docs build needs `libsane-dev` for no reason.
**Avoid:** `uv sync --locked --only-group dev --no-install-project`.

### Pitfall 9: Asserting `--version` output against `pyproject.toml`
**What goes wrong:** the assertion fails in any venv where installed metadata lags an edited
`pyproject.toml`.
**Avoid:** assert against `importlib.metadata.version("saneless")`; pin `pyproject.toml`'s
declared version separately.

### Pitfall 10: `CliRunner` prog name
**What goes wrong:** `--version` prints `cli, version 0.2.0` under `CliRunner`, not
`saneless, version 0.2.0`.
**Avoid:** `runner.invoke(cli, ["--version"], prog_name="saneless")`.

### Pitfall 11: Adding the docker Dependabot ecosystem without a second `cooldown:`
**What goes wrong:** a fresh `dependabot-cooldown` finding fails the zizmor step.
**Avoid:** `cooldown: { default-days: 7 }` on **both** `updates:` entries.

---

## Runtime State Inventory

This is a rename/refactor phase, so the inventory is mandatory.

| Category | Items found | Action required |
|----------|-------------|------------------|
| **Stored data** | **None.** No database, cache or datastore in this project stores `kris-knigga` as a key, collection name or id. Verified: the only persisted store is the SQLite job DB (`job.py`), whose columns are id/profile/title/state/created_at/outcome/warning — none of which carry a repository slug. | none |
| **Live service config** | **GHCR package** `ghcr.io/kdknigga/saneless` does not exist yet (the release workflow has never successfully run — D-53's assessment, re-confirmed by `release.yml`'s three independent breakages). **No** `ghcr.io/kris-knigga/saneless` package exists to rename. GitHub Pages at `kris-knigga.github.io` has never deployed (`docs.yml` triggers on `main`; the branch is `master`). No n8n / Datadog / Tailscale / Cloudflare surfaces in this project. | none — but the *absence* is what makes every README link 404 |
| **OS-registered state** | **None.** No Task Scheduler, launchd, systemd or pm2 registration references the name. The only container-side state is the `saneless-data` named volume, which carries no slug. | none |
| **Secrets and env vars** | **None renamed.** `SANELESS_*` variable names contain no owner slug. `secrets.GITHUB_TOKEN` is GitHub-provided. Trusted publishing uses OIDC, not a stored token — the *pending publisher* records owner `kdknigga` and is created fresh (blockers 1–2), so there is nothing to rename. The user's paperless token is not being rotated (D-53) and moves out of tree (D-51, user action). | none |
| **Build artifacts / installed packages** | **One.** The project's editable install in `.venv` carries `saneless==0.1.0` metadata. After `version = "0.2.0"` lands, `saneless --version` reports the **installed** version, so `uv sync` (or `uv pip install -e .`) must run before any test or manual check of `--version`. `site/` is a stale mkdocs build carrying `kris-knigga` URLs; it is gitignored and regenerated, so it needs no action beyond not being mistaken for evidence. | `uv sync` after the version bump |

**The canonical question — after every file in the repo is updated, what runtime systems still
have the old string cached, stored or registered?** Answer: **none**, because nothing was ever
published under the old name. That is a genuinely unusual and fortunate position for a rename,
and it is worth stating in the plan so nobody goes looking for migration work that does not
exist.

---

## Environment Availability

| Dependency | Required by | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| `uv` | Everything | ✓ | 0.10.3 | — |
| Python 3.14 | Project | ✓ | 3.14.2 (via the project venv; `pyenv shims/python3.14` is broken — pass `--python /path/to/.venv/bin/python` to `uv build`) | — |
| `zizmor` | D-14/D-15 | ✓ (installs cleanly) | 1.30.1 | — |
| `git` | D-10 guard | ✓ | `/usr/bin/git` | — |
| `docker` | D-21 verification | ✓ **as podman** | podman/buildah 1.39.4, rootless, SELinux Enforcing | Use `--userns=keep-id:uid=1000,gid=1000` and `:Z`, or run the verification on a real Docker host |
| `unzip` | DLVR-08 check | ✓ | — | `python -m zipfile -l` |
| `mkdocs` + material | D-13 / DOCS-03 | ✓ (in the dev group) | mkdocs-material ≥ 9.7.6; `mkdocs build --strict` passes today | — |
| `curl` | Digest/SHA resolution | ✓ | — | — |
| Network to `api.github.com`, `pypi.org`, `hub.docker.com`, `ghcr.io` | SHA/digest resolution | ✓ | — | — |
| **GitHub account actions** | Blockers 1–6 | ✗ | — | **None. Hard prerequisite** |

**Missing dependencies with no fallback:** the six account blockers in §5.
**Missing dependencies with fallback:** real Docker (podman with the two flags above).

---

## Validation Architecture

### Test framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 with pytest-timeout, pytest-playwright |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest -m "not browser and not sane_hardware" -x -q` |
| Full suite command | `uv run pytest -m "not browser and not sane_hardware" && uv run pytest -m sane_hardware && uv run pytest -m browser` |
| Current size | 3066 non-browser / non-hardware tests collected [VERIFIED] |
| Doc-truth harness | `tests/test_deployment_config.py` (1085 lines) — the file every new guard/audit test joins |

### TDD classification — testable I/O contracts vs. config/docs edits

TDD mode is enabled. The planner must classify each work item, because a RED-first test is
meaningless for a YAML edit.

**Genuine TDD (write a failing test first):**

| Work item | Contract under test |
|-----------|--------------------|
| The naming guard (D-08) | Given the current tree, the guard names 29 offenders; after the rename, zero |
| `saneless --version` (D-03/D-04, DLVR-10) | `invoke(cli, ["--version"], prog_name="saneless")` → `f"saneless, version {importlib.metadata.version('saneless')}"`, exit 0 |
| Serve streams, writes no file (D-35) | After `serve` loads settings: no `RotatingFileHandler` on root, one `StreamHandler` to stderr, no log directory created |
| `ctx.obj["log_file"] is None` in serve mode | Direct assertion on the context object |
| Traceback-free stream without `-v`; traceback with `-v` (D-36 + Phase 28 D-06) | Two tests over the stream handler's formatter |
| One-shot commands unchanged (D-34) | `jobs` still attaches the file handler and still sets `ctx.obj["log_file"]` |
| `.dockerignore` static contract (D-30) | Starts with `*`; never re-includes a config, secret, `.planning/` or tests path |
| Row-pinning audit tests (D-41) | Each statically checkable row becomes an assertion in the existing harness |
| README static assertions (D-45) | `--title` present; `source` values are real SANE spellings; the tutorial link resolves to an existing file |
| PEP 639 pyproject shape (D-05) | `license == "MIT"`, `license-files == ["LICENSE"]`, **no** `License ::` classifier — necessary because no build step catches a regression (§11.3) |
| No `*.png` glob remains (D-33) | `.gitignore` contains no line equal to `*.png`; `test-results/` is present |
| Port agreement (DLVR-05) | `saneless.toml.example`'s `web_port` line is commented; `EXPOSE`/`HEALTHCHECK`/docs all say 8080 |

**Config/docs edits — verification, not TDD:**

| Work item | Verified by |
|-----------|-------------|
| zizmor wiring, SHA pins, `permissions:` blocks, `workflow_call`, `dependabot.yml` | `uv run zizmor .` → exit 0 (the tool *is* the test) |
| Dockerfile digest pins, `USER`, `WORKDIR`, `FROM … AS uv` | A local build + `docker run --rm <img> id` |
| `docs.yml` trigger fix, Pages flow | Only observable once blocker #5 is done |
| `docs/PRD.md` move | `mkdocs build --strict` + `git ls-files docs/ | grep -c PRD` → 0 |
| Prose corrections for rows 16–18, 21, 29–32 | Recorded verified reads with `file:line` in the D-42 artifact, plus a pinning test wherever the claim is a string presence/absence |

### Phase requirements → test map

| Req | Behaviour | Type | Command | Exists? |
|-----|-----------|------|---------|---------|
| CI-02 / DLVR-01 | No shipped file contains the forbidden slug | unit | `uv run pytest tests/test_deployment_config.py -k naming_guard -q` | ❌ Wave 0 |
| DLVR-03 | Workflows audit clean | tool | `uv run zizmor .` | ❌ Wave 0 (dep + CI step + prek hook) |
| DLVR-04 | `serve` streams, no file | unit | `uv run pytest tests/test_cli.py -k serve_logging -q` | ❌ Wave 0 |
| DLVR-05 | One port everywhere | unit | `uv run pytest tests/test_deployment_config.py -k web_port -q` | ❌ Wave 0 |
| DLVR-06 | Allow-list contract | unit | `uv run pytest tests/test_deployment_config.py -k dockerignore -q` | ❌ Wave 0 |
| DLVR-07 | Non-root + digest pins + WORKDIR | static | `uv run pytest tests/test_deployment_config.py -k dockerfile -q` | ❌ Wave 0 |
| DLVR-08 | Wheel carries LICENSE | static + build | `pytest -k pep639` for the pyproject shape; `unzip -l dist/*.whl` for the artifact | ❌ Wave 0 |
| DLVR-09 | No `*.png` glob | unit | `uv run pytest tests/test_deployment_config.py -k gitignore -q` | ❌ Wave 0 |
| DLVR-10 | `--version` prints installed version | unit | `uv run pytest tests/test_cli.py -k version_option -q` | ❌ Wave 0 |
| DOCS-01..06 | Row-by-row truth | unit + recorded reads | `uv run pytest tests/test_deployment_config.py -q` + the D-42 artifact | partly ✅ (harness exists) |
| DLVR-02 | Release green end to end | **manual, gated on blockers** | `pip install` + `docker pull` from throwaway containers (D-21) | ❌ blocked |

### Sampling rate

- **Per task commit:** `uv run pytest -m "not browser and not sane_hardware" -x -q` (~27 s)
- **Per wave merge:** the full suite plus `uv run prek run --all-files` and
  `uv run prek run --stage pre-push --all-files`
- **Phase gate:** full suite green, `uv run zizmor .` exit 0, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`

### Wave 0 gaps

- [ ] `uv add --dev zizmor` + `uv.lock` regeneration — nothing else can be verified until this lands
- [ ] `uv sync` after the version bump, or every `--version` assertion reads `0.1.0`
- [ ] New test module/section in `tests/test_deployment_config.py` for the naming guard, the
      `.dockerignore` contract, the Dockerfile contract, the `.gitignore` contract and the
      pyproject PEP 639 shape
- [ ] New `TestServeLogging` class in `tests/test_cli.py`
- [ ] The D-42 audit artifact skeleton in `.planning/phases/31-…/` with all 34 rows plus the
      ~20 re-checked "correct" claims

### The five ROADMAP success criteria — how each is validated

| # | Criterion | Validation | Blocked on a user action? |
|---|-----------|-----------|---------------------------|
| 1 | No shipped file references the three forms; CI fails if one reappears; PyPI name stays `saneless` | The guard test, run locally and in CI. The name claim is verified by `pyproject.toml`'s `name = "saneless"` plus the 404s at both indexes | **No** |
| 2 | A pre-release tag runs the whole release workflow green, verified by real `pip install` and `docker pull` from a clean machine | `docker run --rm python:3.14-slim` doing `apt-get install libsane-dev && pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ saneless==0.2.0rc1 && saneless --version`; and `docker run --rm ghcr.io/kdknigga/saneless:0.2.0-rc.1 --version`. Output pasted into the audit artifact along with the tag and the run URL (D-22) | **Yes — blockers 1, 2, 3, 4, 6.** Also §11.5: on this host the pull half needs podman flags |
| 3 | Container logs in `docker logs`; one port; non-root from digest-pinned bases with a `WORKDIR`; `.dockerignore` allow-list keeps secrets/`.planning/`/tests out | Fully local: the serve-logging unit tests, the static Dockerfile/`.dockerignore`/port tests, and a local `docker build` + `docker run --rm <img> id` showing `uid=1000` | **No** |
| 4 | All actions SHA-pinned with Dependabot; every job has `permissions:`; zizmor runs in CI; wheel carries LICENSE; `--version` prints the installed version | `uv run zizmor .` exit 0 covers the first three (and is itself the CI step); `unzip -l dist/*.whl` covers the fourth; the `--version` unit test covers the fifth. **Dependabot actually opening PRs** is observable only after the config lands on `master` | Partly — Dependabot PRs need the config merged, not an account action |
| 5 | A reader following README and the docs hits no false claim; `saneless scan` examples run as written; the new "Which setup do I have?" and trust-model pages resolve from the quick-start prerequisites | The D-42 audit artifact (every row disposed, with a test name or `file:line`) plus the harness tests plus `mkdocs build --strict`. **"Resolve" in the published sense** — i.e. the links work on the live site — needs the docs site to have deployed at least once | **Partly — blocker 5.** The link *targets* can be verified locally as existing files; the *published* resolution cannot |

---

## Code Examples

### The naming guard (ruff-clean, verified)

```python
# The forbidden slug is assembled from two constants rather than written out,
# so this file -- which the guard scans like every other -- does not contain
# the string it forbids. No file is exempt, so there is no exempt file where a
# real reference could hide (D-11). Do not "simplify" this to a literal, and do
# not use "-".join(("kris", "knigga")): ruff's FLY002 rewrites that form into
# the literal and the guard then fails on itself.
_OWNER_GIVEN = "kris"
_OWNER_FAMILY = "knigga"
FORBIDDEN_OWNER_SLUG = f"{_OWNER_GIVEN}-{_OWNER_FAMILY}"

# Every argv element is a literal and the repository path travels as `cwd`,
# the shape the other child-process tests in this suite established -- ruff's
# S603 fires on any non-literal argv element and suppression is forbidden.
_GIT = "/usr/bin/git"


def _shipped_files() -> list[str]:
    """Return every tracked path outside ``.planning/`` (D-10)."""
    result = subprocess.run(
        [_GIT, "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return [
        name
        for name in result.stdout.split("\0")
        if name and not name.startswith(".planning/")
    ]


def test_no_shipped_file_references_the_old_owner() -> None:
    """No tracked file outside ``.planning/`` names the old GitHub owner (CI-02)."""
    offenders: list[str] = []
    for name in _shipped_files():
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        offenders.extend(
            f"{name}:{number}: {line.strip()}"
            for number, line in enumerate(text.splitlines(), 1)
            if FORBIDDEN_OWNER_SLUG in line
        )
    assert not offenders, (
        "a shipped file still references the old GitHub owner; every URL must "
        "use the kdknigga forms (DLVR-01):\n" + "\n".join(offenders)
    )
```

### DLVR-08's literal check

```bash
uv build --wheel --out-dir dist
unzip -l dist/saneless-0.2.0-py3-none-any.whl | grep 'dist-info/licenses/LICENSE'
# saneless-0.2.0.dist-info/licenses/LICENSE
unzip -p dist/saneless-0.2.0-py3-none-any.whl saneless-0.2.0.dist-info/METADATA | head -8
# Metadata-Version: 2.4
# License-Expression: MIT
# License-File: LICENSE
```

### Criterion 2's clean-machine verification (D-21)

```bash
# PyPI half, against the TestPyPI rehearsal
docker run --rm python:3.14-slim bash -lc '
  apt-get update -qq && apt-get install -y -qq --no-install-recommends libsane-dev >/dev/null &&
  pip install --quiet \
    --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ \
    "saneless==0.2.0rc1" &&
  saneless --version'

# GHCR half
docker pull ghcr.io/kdknigga/saneless:0.2.0-rc.1
docker run --rm ghcr.io/kdknigga/saneless:0.2.0-rc.1 --version
docker run --rm --entrypoint id ghcr.io/kdknigga/saneless:0.2.0-rc.1   # expect uid=1000
```

`--extra-index-url` is required: TestPyPI does not carry saneless's runtime dependencies.

---

## State of the Art

| Old approach | Current approach | When changed | Impact here |
|--------------|------------------|--------------|-------------|
| `license = {text = "MIT"}` + `License ::` classifier | `license = "MIT"` (SPDX) + `license-files = [...]`, Metadata 2.4 | PEP 639, widely supported through 2025–2026 | D-05; verified working under `uv_build` 0.10.3 |
| `uses: ./.github/workflows/x.yml` | `uses: $/.github/workflows/x.yml` | GitHub, 2026-07-30 | zizmor 1.30.1 flags the old form at **High** confidence |
| `mkdocs gh-deploy` pushing to a branch | `mkdocs build` → `upload-pages-artifact` → `deploy-pages` | GitHub Pages "Source: GitHub Actions", 2022 onward | §11.1 — the only zizmor-clean option |
| Version tags (`@v5`) on actions | Full 40-hex SHA pins with a version comment | Post-`tj-actions/changed-files` (2025) | Already `ci.yml`'s convention; extended to the other two workflows |
| API-token PyPI publishing | OIDC trusted publishing + `attestations: true` | PyPI 2023 onward; pending publishers for new projects | D-18/D-19 |

**Deprecated / outdated:**
- `pypa/gh-action-pypi-publish@v1.12` — the ref does not exist; current is `v1.14.2`.
- `docker/metadata-action@v5`, `docker/build-push-action@v6`, `docker/login-action@v3` — all
  one major behind (v6 / v7 / v4 respectively).
- `actions/setup-python@v5` in `docs.yml` — superseded by v7, and removed entirely under the
  recommended `uv`-based docs job.

---

## Security Domain

Security enforcement is on (ASVS L1, blocking on `high`). This phase's threat surface is
supply chain, container privilege, and build-context secrets.

### Applicable ASVS categories

| ASVS Category | Applies | Standard control in this phase |
|---------------|---------|-------------------------------|
| V1 Architecture / SDLC | yes | zizmor in CI + prek; Dependabot for actions and Docker; SHA and digest pinning |
| V2 Authentication | no | Trusted publishing is OIDC with no stored credential; the app's own no-auth posture is Phase-26 territory and only gets a documentation sentence (D-50) |
| V3 Session Management | no | No session surface changes |
| V4 Access Control | yes (workflow) | Least-privilege `permissions:` per job; environment approval on `pypi` (D-18); GHCR visibility is an explicit user action |
| V5 Input Validation | no | No new input surface |
| V6 Cryptography | yes (by reference only) | SHA-256 digest pinning and Sigstore attestations — both delegated to GitHub/PyPI tooling, never hand-rolled |
| V7 Error handling / logging | yes | The mode split changes where logs land. **The stream must not widen what is logged**: `-v` still raises only `saneless`'s own loggers, so httpx never prints the Paperless `Authorization` header (T-27-23, D-38) |
| V8 Data protection | yes | `.dockerignore` allow-list + explicit `COPY` keep a real `config.toml` out of every layer (D-29, D-52, D-53) |
| V14 Configuration | yes | Non-root UID 1000, `WORKDIR`, digest-pinned bases, no `--privileged` |

### Threat patterns for this stack

| Pattern | STRIDE | Mitigation in this phase |
|---------|--------|-------------------------|
| Mutable action tag repointed to hostile code (`tj-actions/changed-files` class) | Tampering / EoP | SHA-pin all 11 unpinned `uses:`; zizmor `unpinned-uses` blocks regressions |
| Mutable image tag (`ghcr.io/astral-sh/uv:latest` pulls an executable into the build) | Tampering | Digest-pin all three base references; promote uv to `FROM` so Dependabot maintains it |
| Credential persisted into a workflow artifact | Info disclosure | `persist-credentials: false` on all seven checkouts; zizmor `artipacked` |
| Actions cache poisoned by a lower-privilege ref, consumed by the publishing job | Tampering | `enable-cache: false` on `setup-uv` in `release.yml`; zizmor `cache-poisoning` |
| Over-broad `GITHUB_TOKEN` in the release path | EoP | Workflow- and job-level `permissions:`; `id-token: write` scoped to the jobs that publish |
| Accidental tag push triggering an irreversible publish | Tampering | Manual approval on the `pypi` environment (D-18); D-20 puts every tag push in the user's hands |
| Real `config.toml` (with the live Paperless token) swept into a build layer | Info disclosure | Allow-list `.dockerignore` **and** explicit `COPY` lines — two independent gates (D-29), pinned by D-30 |
| Container escape / host damage from a root process | EoP | UID/GID 1000, `WORKDIR /var/lib/saneless`, no `--privileged`, no `/dev/bus/usb` passthrough (D-48) |
| Supply-chain substitution of the published artifact | Spoofing | `attestations: true` (PyPI) and `provenance: true` (GHCR), both on the existing OIDC identity |
| Project-name squat between creating the pending publisher and first publish | Spoofing | Both `saneless` endpoints 404 as of 2026-09-18; do the rehearsal promptly (§5) |
| Credential leak through debug logging | Info disclosure | Unchanged: `-v` raises only `saneless`'s loggers (D-38, T-27-23). The serve stream inherits this — **no new exposure** |

**Residual risk on record (not this phase's to fix):** D-53 declines to rotate the Paperless
token. The assessment stands — the file was gitignored and never committed, the exposure was
the build *context* and the discarded builder stage only, and no image built from this tree was
ever published because `release.yml` has never run successfully (confirmed: its `test` job is
broken three independent ways). D-51 moves the file out of tree, which removes the vector
structurally.

---

## Assumptions Log

| # | Claim | Section | Risk if wrong |
|---|-------|---------|---------------|
| A1 | Docker BuildKit discards writes to a `VOLUME`-declared path made after the `VOLUME` instruction (measured only that buildah does *not*) | §6.2 | Low — the recommendation (chown before VOLUME) is correct either way |
| A2 | Docker BuildKit's `.dockerignore` matching is identical to buildah's (both use `moby/patternmatcher`) | §6.4 | Low — a mismatch would show as a failed `uv build` in the builder stage, loudly |
| A3 | Dependabot's docker ecosystem may open PRs against `docker-compose.yml`'s `image:` line | §4 | Low — noise, not breakage; remedy is an `ignore:` entry |
| A4 | `gh-action-pypi-publish` treats an empty `repository-url` as "use the default" | §4 | Medium — mitigated by the recommended explicit-URL step |
| A5 | Adding a pytest hook to prek's push stage costs ~27 s (the current non-browser suite time) | §8 | Low |
| A6 | GHCR packages published by `GITHUB_TOKEN` are private by default and need a one-way manual switch to public | §5 | Low — cited from GitHub docs, but the *workflow-published* case was not observed directly |
| A7 | The resolved action SHAs and image digests will still be current at implementation time | §4, §6.3 | Medium — **re-resolve them**; they are dated 2026-09-18 |

---

## Open Questions

1. **Does the serve stream carry tracebacks without `-v`?**
   - What we know: the discretion note says `_TracebackFreeFormatter`'s rule must hold on the
     stream; D-35/D-40 say there is no file.
   - What's unclear: whether the user intends tracebacks from a running service to be
     unrecoverable without a restart.
   - Recommendation: ask before the plan locks it; default to the literal reading (§11.4).

2. **`gh-deploy` or the Pages-artifact flow?**
   - What we know: `gh-deploy` cannot pass zizmor at the default persona; the Pages flow can,
     and is verified zero-finding.
   - What's unclear: whether the user is willing to change the Pages source setting.
   - Recommendation: the Pages flow; it also removes `contents: write` (§11.1).

3. **Does D-08's third enforcement point get built?**
   - Recommendation: add `uv run pytest -m "not browser and not sane_hardware"` at
     `stages: [pre-merge-commit, pre-push]`, or restate D-08 as two points (§8).

4. **What version does `pyproject.toml` carry during the rehearsal?**
   - `0.2.0-rc.1` normalises to `0.2.0rc1`, which is correct for TestPyPI but means the final
     release is a second version bump commit. The alternative (tag the RC against a
     `0.2.0` pyproject) would publish `0.2.0` to TestPyPI and burn the number there.
   - Recommendation: bump to `0.2.0-rc.1`, rehearse, then bump to `0.2.0` for the real tag.

5. **Is `mkdocs build --strict` added to the docs job?**
   - It passes today [VERIFIED], and it would catch a broken nav or link. Recommendation: yes.

---

## Sources

### Primary (HIGH confidence — executed or read in this session)

- `zizmor 1.30.1` run against this repo and against a candidate workflow set (23 findings/exit 14 → 0 findings/exit 0)
- `uv build --wheel` with PEP 639 keys; wheel contents and METADATA inspected
- `click 8.3.1` `version_option(package_name=…)` invoked via `CliRunner`
- `podman`/`buildah 1.39.4` image builds for volume-ownership and `.dockerignore` semantics
- `ruff 0.15.x` with this project's exact config, over four guard prototypes
- `.venv/…/uvicorn/config.py:364-400` — `configure_logging` source
- `.venv/…/pytest_playwright/pytest_playwright.py:463` — `--output` default
- `dependabot-core/docker/lib/dependabot/docker/file_parser.rb` — `FROM_LINE` regex and the `COPY --from` exclusion
- `api.github.com` release/tag refs for all eight actions
- `pypi.org` / `test.pypi.org` JSON endpoints for `saneless` (404) and `zizmor`
- `mkdocs build --strict` against `docs/`
- `git ls-files`, `git check-ignore -v`, and full greps over the repo

### Secondary (MEDIUM confidence — official documentation)

- PEP 639 — <https://peps.python.org/pep-0639/>
- <https://docs.zizmor.sh/audits/#self-repository>, `#artipacked`, `#unpinned-uses`, `#cache-poisoning`, `#dependabot-cooldown`
- <https://github.com/docker/metadata-action> — `flavor: latest=auto` and pre-release handling
- <https://github.com/pypa/gh-action-pypi-publish> — inputs, TestPyPI, attestations
- <https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/> — pending publishers
- <https://github.blog/changelog/2026-07-30-reference-same-repository-actions-with-self-repository-syntax/>
- <https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility>

### Tertiary (LOW confidence — community, flagged)

- <https://github.com/orgs/community/discussions/38178> and `actions/runner#998` — dynamic `environment.name` requires the object form. Corroborated by two independent sources but not confirmed against a live workflow run.

---

## Metadata

**Confidence breakdown:**
- Standard stack: **HIGH** — one new package, executed and slopchecked
- Workflows / zizmor: **HIGH** — baseline and target both measured, exit codes observed
- Packaging (PEP 639, `--version`, RC versioning): **HIGH** — wheels built and inspected
- Container mechanics: **HIGH** for volume/context semantics (measured); **MEDIUM** for the
  BuildKit-vs-buildah `VOLUME` ordering difference (A1)
- Logging split: **HIGH** — uvicorn source read, every affected test enumerated by file:line
- The 34-row audit: **HIGH** — every row re-checked against the shipped tree today
- GitHub-side behaviour (environments, Pages, GHCR visibility): **MEDIUM** — documented, not
  observed, because it needs the user's account

**Research date:** 2026-09-18
**Valid until:** 2026-10-18 for the analysis; **the action SHAs and image digests in §4 and
§6.3 should be re-resolved at implementation time regardless.**
