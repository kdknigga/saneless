---
status: issues_found
phase: 20-ci-gate
depth: deep
reviewed: 2026-09-10T00:00:00Z
files_reviewed: 6
critical: 2
warning: 6
info: 7
findings:
  critical: 2
  warning: 6
  info: 7
  total: 15
files_reviewed_list:
  - .github/workflows/ci.yml
  - .github/dependabot.yml
  - CONTRIBUTING.md
  - pyproject.toml
  - src/saneless/config.py
  - tests/test_scanner.py
diff_base: bef7ab4^
---

# Phase 20: Code Review Report — CI Gate

**Depth:** deep
**Files reviewed:** 6
**Status:** issues_found

## Summary

The Python-side work in this phase is genuinely clean. I independently re-ran all five checks
(`ruff check`, `ruff format --check`, `ty`, `pyrefly`) and they pass on the bumped toolchain;
`git grep` confirms **zero** `# type: ignore` in `src/` and `tests/`; no ruff rule was disabled and
no `per-file-ignores` was widened to fake a pass. Both action SHA pins were verified against the
GitHub API and resolve **exactly** to their claimed tags:

| Pin | Claimed | `gh api` resolution |
|---|---|---|
| `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1` | v7.0.1 | matches `refs/tags/v7.0.1` |
| `astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d` | v10.0.1 | matches `refs/tags/v10.0.1` |

I also confirmed the negative security findings the phase claims: there is **no**
`pull_request_target`, **no** `${{ }}` interpolation inside any `run:` block (no script-injection
sink), **no** `continue-on-error`, **no** `|| true`, no secrets referenced, and a tracked-file
secret sweep over the published tree returns nothing (the single `ghp_` hit is the `ghp_import`
package URL in `uv.lock`). `.planning/` is confirmed 0 paths on the published head via GitHub's
trees API, and PR #1 is `OPEN` / `mergedAt: null`.

The defects are concentrated in two places the phase's own verification did not reach:

1. **`CONTRIBUTING.md` was written in Plan 01 and never reconciled against what Plan 04 actually
   built.** It is a public-facing document that makes two verifiably false statements about the
   repository's protection model. A live `gh api .../rulesets/22777879` read-back contradicts it.
2. **The `concurrency` group keys on a branch *name*, which is not unique across forks** — the
   classic public-repo footgun, and this repo is now public and accepting fork PRs.

Everything else is hardening and latent-fragility work.

---

## Critical Issues

### CR-01: `CONTRIBUTING.md` asserts a branch-protection posture that does not exist

**File:** `CONTRIBUTING.md:90-92`

**Issue.** The document states, verbatim:

> `master` is protected by a branch ruleset with **no bypass actors**. That means every change
> reaches `master` through a **pull request** with both required checks green — including the
> maintainer's own changes. **There is no direct push path.**

Both load-bearing claims are false against the live ruleset. Read back during this review:

```json
{
  "name": "master gate",
  "enforcement": "active",
  "bypass_actors": [
    { "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always" }
  ],
  "rules": [
    { "type": "required_status_checks", "parameters": {
        "required_status_checks": [
          { "context": "lint", "integration_id": 15368 },
          { "context": "test", "integration_id": 15368 }
        ],
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": true } },
    { "type": "deletion" },
    { "type": "non_fast_forward" }
  ]
}
```

- `bypass_actors` is **not** `[]`. `RepositoryRole` id 5 is the repository-admin role with
  `bypass_mode: "always"`. `20-04-SUMMARY.md:92` records this as a deliberate user decision and even
  raises its own `threat_flag: admin-bypass-present` — but nobody went back and corrected the
  document that says the opposite. `20-01-SUMMARY.md:66` still claims CONTRIBUTING.md "documents the
  `bypass_actors: []` consequence," which is now a false provenance record.
- There is **no `pull_request` rule type in the ruleset at all.** `20-04-SUMMARY.md` states it
  explicitly: *"The `pull_request` rule type is still undecided (add now / Phase 31 / never). It was
  not added."* So "every change reaches `master` through a pull request" is not enforced by anything.

**Concrete failure scenario.** An outside contributor reads CONTRIBUTING.md on a public repository
and forms a false model of the project's integrity guarantees: they believe every commit on `master`
has passed the gate and was human-reviewed in a PR. Neither is guaranteed. The admin can push a red
commit straight to `master` at any time, and the ruleset would not stop it. On a repo being prepared
for open-source release, publishing a security claim that a read-back disproves is the kind of thing
that gets found by someone else.

**Fix.** Rewrite the section to describe the ruleset that exists, and re-derive it from the API
rather than from the plan text:

```markdown
## Everything goes through a pull request

`master` carries an active branch ruleset (`master gate`) that requires the `lint` and `test`
checks to pass before any commit lands, and that forbids branch deletion and non-fast-forward
pushes. The repository-admin role holds an `always` bypass as an emergency path; no automation
uses it. Practically, contributors reach `master` by opening a pull request and waiting for both
required checks to go green.
```

Then either add a `pull_request` rule to the ruleset (which would make the original wording true),
or keep the corrected wording. Do not leave the two out of sync.

---

### CR-02: `concurrency` group keys on a bare branch name, so unrelated fork PRs cancel each other's required checks

**File:** `.github/workflows/ci.yml:12-14`

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.head_ref || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

**Issue.** For a `pull_request` event, `github.head_ref` is the **head branch name only** — it
carries no fork owner and no PR number. Two pull requests from two different forks that happen to
use the same branch name therefore land in the *same* concurrency group, and because
`cancel-in-progress` is `true` for `pull_request`, the newer run cancels the older one.

This is not a hypothetical name collision. GitHub's own web editor names the fork branch it creates
`patch-1` by default, and `main`/`master` are the other obvious collisions. On a public repository
taking drive-by PRs, duplicate head-branch names across forks are the norm, not the exception.

**Concrete failure scenario.**

1. Contributor A opens PR #10 from `forkA:patch-1`. Run starts in group `CI-patch-1`.
2. Contributor B opens PR #11 from `forkB:patch-1` ninety seconds later. Same group.
3. PR #10's `lint` and `test` check runs are **cancelled**.
4. A cancelled check run is not `success`, so the `master gate` ruleset's `required_status_checks`
   rule leaves PR #10 blocked, with no failure to explain why. The contributor sees two grey
   "cancelled" checks and no diagnostic.
5. The state persists until a maintainer manually re-runs the workflow or the contributor pushes an
   empty commit — neither of which is discoverable from the PR UI.

The gate mis-fires against a PR that did nothing wrong. That is incorrect behaviour of the exact
mechanism this phase exists to deliver.

**Fix.** Key the group on something unique per PR. `github.ref` is already unique for both events
(`refs/pull/10/merge` vs `refs/pull/11/merge` vs `refs/heads/master`), so the `head_ref` fallback is
not just unnecessary — it is what introduces the collision:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

If you prefer to keep the intent explicit, `${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}` is equally correct.

---

## Warnings

### WR-01: "`--no-verify` no longer gets you anything" is false for 21 of the 25 hooks

**File:** `CONTRIBUTING.md:81-86`

**Issue.** The section is titled "`--no-verify` no longer skips the gate" and states the hooks are
"a convenience, not the enforcement point," because "the same checks run again on the pull request."
Cross-referencing `.pre-commit-config.yaml` against `.github/workflows/ci.yml`:

| Hook | Re-run in CI? |
|---|---|
| `ruff`, `ruff-format`, `ty-checker`, `pyrefly-checker` | yes (4) |
| `detect-private-key`, `detect-aws-credentials`, `check-added-large-files`, `debug-statements`, `check-merge-conflict`, `check-symlinks`, `check-vcs-permalinks`, `check-executables-have-shebangs`, `check-ast`, `check-json`, `check-json5`, `check-toml`, `check-xml`, `check-yaml`, `end-of-file-fixer`, `fix-byte-order-marker`, `mixed-line-ending`, `pretty-format-json`, `sort-simple-yaml`, `trailing-whitespace`, `sync-with-uv` | **no (21)** |

For 21 of 25 hooks the local hook is the *only* enforcement point, and `--no-verify` removes it
entirely. The document tells contributors the opposite.

**Concrete failure scenario.** A contributor commits a `.pem` or an id_rsa fixture with
`git commit --no-verify`, having read that it "no longer gets you anything." `detect-private-key`
would have caught it; CI runs five checks, none of which scan for secrets. I verified the repo's
compensating control is only partial: `secret_scanning` and `secret_scanning_push_protection` are
enabled, but `secret_scanning_non_provider_patterns` is **disabled** — so provider tokens (AWS keys
included) are push-protected, while generic private-key material is not. On a public repository the
key is world-readable the moment the PR head is pushed. `check-added-large-files` and
`debug-statements` have the same shape of gap with lower stakes.

**Fix.** State the scope honestly and stop implying the hooks are redundant:

```markdown
## `--no-verify` does not skip the CI gate

The five CI checks re-run on every pull request, so skipping ruff, the formatter or the type
checkers locally only moves the failure later. The rest of the hook suite — secret detection,
large-file and debug-statement checks, and the file-hygiene fixers — has **no CI counterpart**,
so `--no-verify` genuinely removes those. Do not use it.
```

Separately, consider enabling `secret_scanning_non_provider_patterns` on the repository, which
would close the private-key half of the gap independently of contributor discipline.

### WR-02: the gate does not require branches to be up to date, so two green PRs can merge to a red `master`

**File:** `.github/workflows/ci.yml` (gate behaviour) / `CONTRIBUTING.md:35-46, 88-97`

**Issue.** The live ruleset carries `"strict_required_status_checks_policy": false`. Required checks
are evaluated against the PR head as it was when CI last ran, with no requirement that the branch be
current with `master`.

**Concrete failure scenario.** PR A renames `SaneBackend._open_device` and updates its callers. PR B,
branched earlier, adds a new caller of the old name. Both are independently green — each was checked
against a `master` that did not contain the other. A merges. B merges. `master` is now red: `ty` and
`pyrefly` both fail on the unresolved attribute, and the failure is only discovered by the *next*
PR's CI run or by the `push: branches: [master]` run after the fact. The phase's success criterion —
"a red run blocks merge" — does not hold for this class of breakage, and nothing in CONTRIBUTING.md
warns contributors to rebase.

**Fix.** Either flip the ruleset parameter (accepting the re-run churn it causes on a busy repo):

```bash
# in the ruleset's required_status_checks parameters
"strict_required_status_checks_policy": true
```

…or, if the churn is not wanted at this project's volume, document the limitation in CONTRIBUTING.md
so contributors know to rebase onto `master` before requesting a merge. Silently having neither is
the worst of the three options.

### WR-03: `dependabot.yml` covers only `github-actions`, and Dependabot alerts/security updates are disabled

**File:** `.github/dependabot.yml:1-7`

**Issue.** D-18's stated rationale for adding Dependabot in the same commit as the SHA pins is that
"pinning without Dependabot freezes the pins forever, including the security fixes pinning exists to
control." That reasoning applies verbatim to `uv.lock`, which is also a pin set — a much larger one,
covering the runtime surface of a network-facing web application (`fastapi`, `uvicorn`, `pillow`,
`pydantic`, `httpx`, `python-multipart`). None of it is covered.

I checked the repository's security settings directly:

```json
{"dependabot_security_updates": {"status": "disabled"},
 "secret_scanning": {"status": "enabled"},
 "secret_scanning_push_protection": {"status": "enabled"},
 "secret_scanning_non_provider_patterns": {"status": "disabled"}}
```

`GET /repos/kdknigga/saneless/dependabot/alerts` → `403 "Dependabot alerts are disabled for this
repository."` and `GET .../automated-security-fixes` → `{"enabled": false}`.

**Concrete failure scenario.** A CVE is published for `pillow` (a direct dependency used to process
untrusted scanned-image data) or for `python-multipart` (which parses untrusted upload bodies). No
alert is raised, no PR is opened, and `uv.lock` holds the vulnerable version indefinitely. Meanwhile
the weekly `github-actions` PRs give the repository the *appearance* of active supply-chain
maintenance, which makes the gap less likely to be noticed. The two actions in `ci.yml` are the
lowest-risk pins in the project; the highest-risk ones have zero coverage.

**Fix.** Add the `uv` ecosystem (Dependabot supports it) and enable alerts + security updates:

```yaml
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
  - package-ecosystem: "uv"
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      python-dev:
        dependency-type: "development"
```

```bash
gh api -X PUT repos/kdknigga/saneless/vulnerability-alerts
gh api -X PUT repos/kdknigga/saneless/automated-security-fixes
```

If the volume of PRs is the concern, enabling alerts + security updates alone (without version
updates) closes the security half at near-zero noise. If this is deliberately Phase 31's, say so in
a comment in `dependabot.yml` — right now the file reads as if the job is done.

### WR-04: `_SettingsFactory` formalises an undocumented pydantic-settings mechanism whose failure mode is silent

**File:** `src/saneless/config.py:162-173, 182`

**Issue.** The suppression removal is a real improvement over `# type: ignore[call-arg]` — the
Protocol names the one kwarg and rejects every other, so a typo in `_toml_file` is now a type error
where before it was invisible. That part is sound and I have no objection to it.

What the Protocol does *not* do is make `_toml_file` supported. It is not a pydantic-settings init
kwarg. The documented reserved kwargs are `_case_sensitive`, `_env_prefix`, `_env_file`,
`_env_nested_delimiter`, `_secrets_dir`, the `_cli_*` family, and so on; `_toml_file` is invented by
this project. It survives only because of one line in the installed pydantic-settings 2.13.1
(`sources/base.py:309`):

```python
# Include any remaining init kwargs (e.g., extras) unchanged
self.init_kwargs.update({key: val for key, val in init_kwargs.items() if key in init_kwarg_names})
```

`InitSettingsSource.__init__` builds a **fresh** dict; the `pop` at `config.py:135` mutates that
fresh dict before `__call__` reads it, which is why the mechanism works today. But the contract being
relied on is "unknown underscore-prefixed kwargs are passed through to `init_kwargs` verbatim," and
that is precisely the namespace pydantic-settings reserves for itself.

**Concrete failure scenario.** A future pydantic-settings release starts filtering `_`-prefixed keys
out of `init_kwargs` (an entirely reasonable change, since those keys are its own reserved namespace
and leaking them into `extra` is a latent bug on their side). The `pop` then returns `None`, the
`if toml_file is not None` branch in `settings_customise_sources` is skipped, and
`TomlConfigSettingsSource` is never added. `load_settings("/etc/saneless/config.toml")` returns
**defaults**, silently. No exception, no warning — the user's scanner host, Paperless URL and output
directory are all quietly ignored. The type checkers cannot see this; `_SettingsFactory` describes a
signature, not a behaviour.

The severity is contained by the fact that `tests/test_config.py` asserts on TOML-loaded values in
~17 places, so CI would go red on the upgrade. That is what keeps this a Warning rather than a
Blocker. But `20-05-SUMMARY.md:415` calls `_SettingsFactory` "the documented home for anything else
`settings_customise_sources` grows," which invites building more on the same undocumented footing.

**Fix.** Keep the Protocol (it is strictly better than the suppression) but add a runtime assertion
so the mechanism fails loudly rather than degrading silently:

```python
    init_src = cast("InitSettingsSource", init_settings)
    toml_file = init_src.init_kwargs.pop("_toml_file", None)
```

becomes

```python
    init_src = cast("InitSettingsSource", init_settings)
    toml_file = init_src.init_kwargs.pop("_toml_file", _SENTINEL)
    if toml_file is _SENTINEL:
        # Guard: pydantic-settings must pass unknown private init kwargs through
        # to init_kwargs. If that ever changes, fail loudly instead of silently
        # dropping the caller's TOML file.
        toml_file = None
```

…paired with a caller-side check in `_build_settings` that the returned `Settings` actually consumed
the file. Alternatively, switch to the supported route and stop relying on the private kwarg at all:
pass the path through a module-level `ContextVar` (or a `ClassVar` set immediately before
construction) that `settings_customise_sources` reads, which uses only public API and cannot be
broken by a pydantic-settings internals change.

### WR-05: `actions/checkout` persists the credential into `.git/config` before untrusted code runs

**File:** `.github/workflows/ci.yml:22, 39`

**Issue.** `actions/checkout` defaults to `persist-credentials: true`, writing the job's
`GITHUB_TOKEN` into `.git/config` as an `http.extraheader`. Both jobs then execute code from the PR
head: `uv sync --locked` runs arbitrary build backends (this project's `python-sane` is sdist-only
and compiles at install time), and the `test` job runs the PR's own pytest code.

The blast radius here is genuinely small — the workflow-level `permissions: contents: read` means the
persisted token is read-only, and on a public repo a read-only token confers nothing an anonymous
clone does not. I am not claiming an exploitable path. Two things still make it worth fixing now:

**Concrete failure scenario.** D-19 defers `zizmor` to Phase 31. `zizmor`'s `artipacked` rule flags
exactly this pattern, at Medium confidence, on both jobs. When Phase 31 adds `zizmor` as a CI step it
will immediately redden CI against `ci.yml` — a file this phase owns and declared clean — and Phase
31 will then have to fix a Phase 20 artifact under time pressure. Second, the mitigation currently
resting entirely on `permissions: contents: read` is one careless job-level `permissions:` escalation
away from mattering.

**Fix.** Two lines, no behavioural cost — nothing in either job pushes or calls the API:

```yaml
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
```

### WR-06: `uv run prek run` does not reproduce the lint half of the gate, but CONTRIBUTING says it does

**File:** `CONTRIBUTING.md:69-79`

**Issue.** The document presents `uv run prek run` as the local pre-flight and states it "covers the
formatter, the linter and both type checkers." That is only true for the type checkers. In
`.pre-commit-config.yaml`, `ty-checker` and `pyrefly-checker` carry `always_run: true` +
`pass_filenames: false` (lines 52-54, 60-62), so they run over the whole project every time. The
`ruff` and `ruff-format` hooks (lines 39-44) carry neither, so `prek run` — without `--all-files` —
scopes them to **staged files only**. With nothing staged, they report "no files to check" and are
skipped.

The phase's own execution proved the divergence without naming it as a defect:
`20-05-SUMMARY.md:391` records `uv run prek run --all-files` exiting **1** while `uv run prek run`
exits **0**.

**Concrete failure scenario.** A contributor commits their change, then runs `uv run prek run` as
instructed. Nothing is staged, so ruff and ruff-format are skipped; ty and pyrefly run and pass;
prek exits 0 and the contributor pushes believing the linters passed. CI runs `uv run ruff check .`
across the whole tree, hits a violation the staged-file scoping never looked at, and the PR goes red
on a check the documented pre-flight claimed to cover. The `ruff` hook's `args: [--fix]` compounds
the confusion: locally it silently rewrites files, whereas CI's `ruff check .` only reports.

**Fix.** Document the invocation that actually matches CI, and correct the coverage claim:

```markdown
uv run prek run --all-files
```

with a note that `prek run` (no flag) only inspects staged files, so it is a fast pre-commit check
rather than a reproduction of the gate. The unambiguous alternative is to tell contributors to run
the five commands from the block above, which is already in the document at lines 50-56 and *is*
byte-identical to `ci.yml`.

---

## Info

### IN-01: `_options_impl` returns the caller's list by reference while the default path returns a fresh list

**File:** `tests/test_scanner.py:70, 103-104`

The delegation is otherwise faithful and the `is not None` guard is correct — a naive
`if self._options_impl:` would have made an empty override fall through to the defaults, and the
implementation avoids that. One asymmetry: the default branch constructs a new list on every call,
whereas the override returns the *same* object each time. `SaneBackend.get_device_capabilities`
(`src/saneless/scanner/sane_backend.py:322, 343`) stores that list directly into
`DeviceCapabilities.raw_options`, so under an override the capabilities object now aliases the mock's
attribute. Harmless today (neither call site mutates, and both modified tests call `get_options`
once), but the old `lambda: [...]` form did not have this property, so a future test that mutates
`raw_options` would behave differently from one exercising the default path.

```python
        if self._options_impl is not None:
            return list(self._options_impl)
```

### IN-02: the pluggable-impl pattern was applied to one of three mock devices

**File:** `tests/test_scanner.py:96, 189, 913`

Three classes in this file define `get_options`. Only `MockSaneDev` gained `_options_impl`; the mocks
at lines 189 (`return []`) and 913 (hard-coded three-tuple list) still hard-code theirs. Anyone
following the pattern established here will find it works on one of the three. Not a bug — worth a
one-line comment or the same attribute on the other two if the pattern is meant to be the convention.

### IN-03: unparameterised generics defeat the checking the mock exists to provide

**File:** `tests/test_scanner.py:70, 96`; `src/saneless/scanner/sane_backend.py:230`

`_options_impl: list[tuple] | None`, `get_options(self) -> list[tuple]`, and the `SaneDevice`
Protocol's `def get_options(self) -> list: ...` are all unparameterised. The consumers at
`sane_backend.py:326-331` and `:465-470` index `opt[1]` and `opt[8]` after a `len(opt) < 9` guard —
i.e. the tuple *shape* is the contract, and neither `ty` nor `pyrefly` can check it. A test that sets
`_options_impl` to eight-element tuples would type-check cleanly and then be silently skipped by the
length guard, producing an empty `available_sources` and a confusing `ScanError`. A
`tuple[int, str, str, str, int, int, int, int, object]` alias would let the checkers catch it.
(Pre-existing shape; the phase widened its blast radius by making the override assignable.)

### IN-04: CONTRIBUTING.md says `# noqa` is "not accepted" while seven live in the tree

**File:** `CONTRIBUTING.md:58-60`

The line "`# noqa`, `# type: ignore` and rule-disabling are not accepted" is accurate for
`# type: ignore` (I confirmed zero across `src/` and `tests/`) but not for `# noqa`:
`config.py:124,125`, `scanner/__init__.py:23`, `sane_backend.py:47,49`, `test_cli.py:550`,
`test_web.py:406`. All pre-date this phase and all carry justifying comments. A first-time
contributor greps, finds seven counterexamples, and either concludes the rule is unenforced or opens
an issue. Either soften to "must be justified inline and are reviewed case by case," or note that the
existing seven are grandfathered.

### IN-05: the CONTRIBUTING setup command diverges from what CI runs

**File:** `CONTRIBUTING.md:20-21` vs `.github/workflows/ci.yml:24-26, 41-43`

CI runs `sudo apt-get update` then `sudo apt-get install -y --no-install-recommends libsane-dev`.
CONTRIBUTING shows only `sudo apt-get install libsane-dev`. On a container or a long-uncached Debian
image the missing `apt-get update` produces a `E: Unable to locate package libsane-dev` that reads as
"this project's docs are wrong" rather than "run apt-get update." One extra line closes it.

### IN-06: both parallel jobs race for the same uv cache key

**File:** `.github/workflows/ci.yml:27, 44`

`astral-sh/setup-uv` is invoked with no `cache-suffix`, so `lint` and `test` derive an identical cache
key from the same `pyproject.toml` + `uv.lock` and race to reserve it in the post step. This is
already observable: `20-05-SUMMARY.md:316` records run 34488308016's "only annotation" as exactly
this cache-reservation race. It is benign (one job saves, the other logs and moves on) but it puts a
permanent warning annotation on every single run, which trains reviewers to ignore annotations. Adding
`with: { cache-suffix: ${{ github.job }} }` to both, or `enable-cache: true` on one and
`save-cache: false` on the other, removes the noise.

### IN-07: `timeout_method = "signal"` cannot interrupt a hang inside the `sane` C extension

**File:** `pyproject.toml:148-149`

The configuration is well-chosen and I have no objection to it: `timeout = 60` against a measured
worst-case single test of 3.0s is ~20x headroom, far outside flake range even on a cold GitHub
runner, and `signal` is the right pick over `thread` — I confirmed from the plugin's semantics that
`thread` ends the process via `os._exit(1)`, which would take out fixture teardown and every
remaining test, turning one deadlock into an undiagnosable red run.

The one documented weakness of `signal` is worth recording for Phase 22/26: `SIGALRM` is only
delivered when the interpreter regains control, so a hang inside a C extension that neither releases
the GIL nor returns cannot be interrupted. This project wraps `python-sane`, a C extension, and a
blocking `dev.snap()` against real hardware is the single most likely real-world hang. It does not
affect the gated suite — every SANE interaction in `-m "not browser"` is mocked — so `signal` is the
correct choice *here*. If a future test ever drives real hardware, `@pytest.mark.timeout(60,
method="thread")` on that specific test is the escape hatch D-13 already identified.

---

_Reviewed by: Claude (gsd-code-reviewer)_
_Depth: deep — cross-file analysis across `ci.yml` ↔ `.pre-commit-config.yaml` ↔ `CONTRIBUTING.md`,
`config.py` ↔ installed `pydantic_settings/sources/base.py`, and `test_scanner.py` ↔
`scanner/sane_backend.py`, plus live `gh api` read-backs of the ruleset, repository security
settings, action tag SHAs, and the published tree._
