---
phase: 20
slug: ci-gate
status: complete
threats_open: 0
asvs_level: 1
created: 2026-09-10
---

# Security Audit — Phase 20: ci-gate

**Audited:** 2026-09-10
**Repository:** `kdknigga/saneless` (**public**)
**Register:** 27 entries (T-20-01..T-20-26 plus T-20-SC), authored at plan time
**Head audited:** `origin/autodev-filtered` @ `7e977b40cceff426541fc723dbae9b5bb151745c`
**Ruleset audited:** `22777879` ("master gate"), read live
**ASVS level:** not set by the phase; verification performed at L1 breadth with L2 rigour on
the supply-chain and access-control items, which is where this phase's surface actually is.

> **Where this file lives.** It is committed on `autodev` at
> `.planning/phases/20-ci-gate/20-SECURITY.md`, alongside the PLANs and SUMMARYs it cites, where
> `.planning/` is tracked as normal. It must never be created on `autodev-filtered`, the published
> branch, which strips `.planning/` and does not ignore it. See OBS-01.
>
> Nothing in this document is a new disclosure: the ruleset id, the admin bypass, and the required
> contexts are already stated in the public `CONTRIBUTING.md` and are returned by a single
> unauthenticated API call.

---

## Verdict

**27/27 threats resolved.** No blocker. The resolution is not uniform, and the split matters more
than the total:

| Resolution | Count | Threat IDs |
|---|---|---|
| CLOSED — declared mitigation verified present | 23 | T-20-01..08, 10..16, 18..20, 22..26, T-20-SC |
| CLOSED — documented accepted risk, **disposition changed from the plan** | 3 | T-20-09, T-20-17, T-20-21 |
| CLOSED — new threat from a recorded threat flag, verified | 1 | T-20-27 |

Two of the three disposition changes (T-20-09, T-20-21) are **control failures**, not design
choices: a blocking human checkpoint was specified, fired, and was auto-resolved without a human
answering. Both are recorded in full below rather than folded into the CLOSED count. See
[Control Failures](#control-failures).

Evidence for every row was obtained by reading the artifact or querying live repository state.
No row is closed on the strength of a SUMMARY assertion alone.

---

## Threat Verification

### Plan 01 — the workflow

| ID | Category | Disposition | Status | Evidence |
|---|---|---|---|---|
| T-20-01 | Elevation of Privilege | mitigate | CLOSED | `.github/workflows/ci.yml:9-10` — workflow-level `permissions: { contents: read }`. No job re-declares `permissions`. Independently corroborated at the repository level: `actions/permissions/workflow` → `default_workflow_permissions: "read"`, `can_approve_pull_request_reviews: false`. Two layers, both read-only. |
| T-20-02 | Tampering | mitigate | CLOSED | 4 `uses:` lines, 4 full 40-hex SHA pins, **0 unpinned** (`actions/checkout@3d3c42e5…` v7.0.1, `astral-sh/setup-uv@20cfd1bf…` v10.0.1, each twice, each with a version comment). `.github/dependabot.yml` present, `github-actions` ecosystem, weekly. See RES-01 for the residual. |
| T-20-03 | Denial of Service | mitigate | CLOSED | `pyproject.toml:151-152` — `timeout = 60`, `timeout_method = "signal"` under `[tool.pytest.ini_options]`, so it fires locally and in CI identically. `ci.yml:20` `timeout-minutes: 10` (lint), `ci.yml:37` `timeout-minutes: 15` (test) — both jobs bounded, which also bounds `apt-get` and `uv sync`. |
| T-20-04 | Tampering | mitigate | CLOSED | `20-RESEARCH.md:164-175` Package Legitimacy Audit — `pytest-timeout` 2.4.0, pytest-dev, `slopcheck` 3/3 OK, no `[SLOP]`/`[SUS]`/`[ASSUMED]`. Verified against reality, not just the table: `uv.lock` resolves `pytest-timeout` 2.4.0 from `registry = "https://pypi.org/simple"`; installed version is 2.4.0. Audited version == locked version == installed version. |
| T-20-SC | Tampering | mitigate | CLOSED | Same evidence as T-20-04. No `[ASSUMED]`/`[SUS]`/`[SLOP]` package in the phase, so the blocking legitimacy gate was correctly not required. |
| T-20-05 | Information Disclosure | **accept** | CLOSED | `ci.yml:7` is `pull_request`, **not** `pull_request_target` — PR head code therefore runs with the read-only token and without repository secrets. Grep for `secrets.` across `ci.yml`: **0 matches**. Accepted-risk entry recorded below. |

### Plan 02 — filtering and pre-publication

| ID | Category | Disposition | Status | Evidence |
|---|---|---|---|---|
| T-20-06 | Information Disclosure | mitigate | CLOSED (superseded) | The control executed as designed: visibility read back as `isPrivate: true` and `git ls-remote origin` returned 0 refs before the first push (`20-02-SUMMARY`, `20-03-SUMMARY`). The **premise has since been reversed by user decision** — the repository is public now. Superseded by T-20-27, where the consequence is re-verified rather than assumed. |
| T-20-07 | Information Disclosure | mitigate | CLOSED | Re-derived independently, not read from the SUMMARY. `git ls-tree -r origin/autodev-filtered \| grep -c '^\.planning/'` → **0**. Per-commit sweep across **all 117** published commits (`git diff-tree` over `git rev-list`) → **0**. Confirmed against the remote's own view: `git/trees/7e977b4?recursive=1` → **0** paths under `.planning`. No blob, not merely no tip. |
| T-20-08 | Information Disclosure | mitigate | CLOSED, **strengthened** | Fresh sweep, run against the full published **history** rather than only the head tree, because on a public repo a deleted secret is still readable. Patterns: `gh[pousr]_`, `github_pat_`, `AKIA`, `sk-`, `xox[baprs]-`, PEM private-key headers, JWT shape. **0 hits in the 81-file head tree; 0 hits across all 117 commits.** Assignment-style sweep (`password\|secret\|api_key\|token\|private_key` = quoted literal) → 0 after excluding fixtures/placeholders. `saneless.toml` untracked; `site/` 0 files committed; no `.env`, `.pem`, `.key`, `.p12`, `id_rsa`, or `credentials` path in history. |
| T-20-09 | Repudiation | mitigate → **accept (realized)** | CLOSED as accepted risk | **Declared control did not execute.** See [CF-01](#cf-01--pre-publication-checkpoint-auto-resolved). |
| T-20-10 | Elevation of Privilege | **accept** | CLOSED | The user's own OAuth token; `collaborators` shows `kdknigga` with `admin: true`; the action was one setting on one repository and is reversible. Accepted-risk entry recorded below. |

### Plan 03 — publication and the first PR

| ID | Category | Disposition | Status | Evidence |
|---|---|---|---|---|
| T-20-11 | Information Disclosure | mitigate | CLOSED | Same three-way evidence as T-20-07, re-run at the **current** head `7e977b4` rather than at the head the SUMMARY was written against (`f66d132`). Two commits have landed since that SUMMARY (`a901aad`, `7e977b4`); both were checked and both carry 0 `.planning/` paths. |
| T-20-12 | Tampering | mitigate | CLOSED | `git rev-parse origin/master` → `a87b3ddf45b094bb03dedc88a8aade5cd73d33c4`. `git log --oneline origin/master` → a single commit, `a87b3dd Initial commit`. `git ls-remote --heads origin` → exactly 2 refs (`master`, `autodev-filtered`). `git ls-remote --tags origin` → **0**. No merge, no `<branch>:master` push, no tag. |
| T-20-13 | Elevation of Privilege | mitigate | CLOSED | Verified by execution, not by reading the file. Run `34493178468` (head `7e977b4`) job steps: `lint` ran `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check`; `test` ran `uv run pytest -m "not browser"`. **All five, all `success`, as real steps.** Grep of `ci.yml` for `continue-on-error`, `\|\| true`, `pull_request_target`, `permissions:.*write`, `secrets.` → **0 matches**. |
| T-20-14 | Spoofing | mitigate | CLOSED | GraphQL `statusCheckRollup` on PR #1: both `lint` and `test` resolve to `checkSuite.app.databaseId = 15368`, `slug = "github-actions"`. Plan 03's finding that `copilot-pull-request-reviewer` shares app 15368 is confirmed live (run `34486591917`, event `dynamic`) — and is excluded by **name**, since it appears nowhere in `required_status_checks`. Name + integration_id, as Plan 03 required. |
| T-20-15 | Information Disclosure | mitigate | CLOSED | PR #1 body fetched and grepped for `.planning`, `code review`, `REVIEW.md`, `threat model` → **0 matches**. The body describes only the workflow, the pins, the timeout, and `CONTRIBUTING.md`. No planning trail. |

### Plan 04 — the ruleset

| ID | Category | Disposition | Status | Evidence |
|---|---|---|---|---|
| T-20-16 | Spoofing | mitigate | CLOSED | Live ruleset `22777879`: `required_status_checks` = `[{context: "lint", integration_id: 15368}, {context: "test", integration_id: 15368}]`. **Both** entries pinned; neither is context-only. |
| T-20-17 | Elevation of Privilege | mitigate → **accept** | CLOSED as accepted risk | **Declared control (`bypass_actors: []`) is absent.** Deliberate user override. See [AR-03](#ar-03--repository-admin-bypass-on-the-master-ruleset). |
| T-20-18 | Tampering | mitigate | CLOSED | Live ruleset `rules[]` contains `{"type": "deletion"}` and `{"type": "non_fast_forward"}` alongside `required_status_checks`. `enforcement: "active"`. `conditions.ref_name.include` = `["refs/heads/master"]`, `exclude` = `[]`. |
| T-20-19 | Denial of Service | mitigate | CLOSED, **proven live** | The strongest available evidence, and it is behavioural rather than textual: GraphQL reports `isRequired: true` for **both** `lint` and `test` against **matched, real, SUCCESS** check-runs on PR #1, and PR #1's `mergeStateStatus` is `CLEAN`. A phantom context would leave PR #1 permanently `BLOCKED`. The contrast case exists too — PR #2 is `BLOCKED`. Contexts are therefore live-matched, not merely well-spelled. |
| T-20-20 | Tampering | mitigate | CLOSED | PR #2 (`seeded-break-d08-3`): `state: CLOSED`, `mergedAt: null`, `mergeStateStatus: BLOCKED`. The branch is absent from `git ls-remote --heads origin` (only `master` and `autodev-filtered` remain) and absent from `git branch -a`. `git worktree list` shows only the primary tree. `master` still `a87b3dd`. The seeded break left no trace. |
| T-20-21 | Repudiation | mitigate → **accept** | CLOSED as accepted risk | **Declared control did not execute.** See [CF-02](#cf-02--lock-out-checkpoint-auto-resolved). |

### Plan 05 — the toolchain bump

| ID | Category | Disposition | Status | Evidence |
|---|---|---|---|---|
| T-20-22 | Tampering | mitigate | CLOSED | `20-RESEARCH.md:169-170` audits `ty` 0.0.80 (astral-sh) and `pyrefly` 1.2.0 (facebook upstream), `slopcheck` OK. Verified against reality: `uv.lock` resolves both from `registry = "https://pypi.org/simple"`; `uv run ty --version` → `ty 0.0.80`; `uv run pyrefly --version` → `pyrefly 1.2.0`. Audited == locked == installed, for both. |
| T-20-23 | Repudiation | mitigate | CLOSED | Grepped the **published** tree, not the local one. `type: ignore` → **0**. `# (ty\|pyrefly): ignore` → **0**. `# noqa` → exactly **7**, all pre-existing and all enumerated by the SUMMARY (`config.py:124,125`; `scanner/__init__.py:23`; `sane_backend.py:47,49`; `test_cli.py:550`; `test_web.py:406`) — the file:line set matches exactly, so none were added under cover of the seven. `pyproject.toml` `per-file-ignores` inspected directly (`:128-135`): 3 scoped entries, none widened to blanket suppression. |
| T-20-24 | Tampering | mitigate | CLOSED, **re-measured** | Not taken from the SUMMARY. The suite was re-run during this audit: **`332 passed, 8 deselected in 26.78s`** — identical to the declared baseline. Corroborating structural evidence: `@pytest.mark.skip\|xfail` across `tests/` → **0 occurrences**, and `xfail_strict = true`, `strict_markers = true`, `strict_config = true`, `filterwarnings = ["error"]` are all set, so a silently-degrading suite would itself turn the gate red. |
| T-20-25 | Tampering | mitigate | CLOSED | `.planning/` re-verified at the current published head (see T-20-11), covering the two commits that landed after the SUMMARY was written. Push was a genuine fast-forward — `origin/autodev-filtered` is a linear 117-commit ancestor chain and the ref is reachable from local `HEAD` without a force marker. See OBS-01 for the durability caveat. |
| T-20-26 | Elevation of Privilege | accept then mitigate | CLOSED | The mitigate half is evidenced: the bump commit `f66d132` sits on `autodev-filtered` inside PR #1, and run `34488308016` (event `pull_request`, head `f66d132`) is `success` on both gated jobs. The bypass was **not** exercised — `master` still holds exactly one commit, so nothing has ever reached it by any path, gated or bypassed. PR #1 remains `OPEN` with `mergedAt: null`. |

### New — from recorded threat flags

| ID | Category | Disposition | Status | Evidence |
|---|---|---|---|---|
| T-20-27 | Information Disclosure | accept | CLOSED | Registered from `threat_flag: exposure-scope-change` (20-04-SUMMARY). See [AR-04](#ar-04--the-repository-is-public). |

**`threat_flag: admin-bypass-present`** maps to T-20-17. Informational, not a separate threat, and not
an unregistered flag.

**Unregistered flags: none.** Both flags recorded during implementation map to a register entry.

---

## Control Failures

These are the findings that the 27/27 headline would otherwise hide. Neither is remediable — both
describe events that have already happened — but both are process defects that recur unless named.

### CF-01 — pre-publication checkpoint auto-resolved
**Threat:** T-20-09 (Repudiation) — *an irreversible publication performed without the user's knowledge*
**Declared control:** "Blocking human checkpoint enumerating exactly what will be pushed, before any push."
**What happened:** The checkpoint fired and enumerated the contents correctly, and was then resolved by
automated approval rather than by a human reading the evidence (20-02-SUMMARY, carried into
20-03-SUMMARY). The push then proceeded. **The control as specified — a human answering — did not execute.**

**Why it is nonetheless closed rather than open:** the threat's *outcome* (a user unaware their history
is published) is affirmatively disproven, not merely assumed away. After publication the user made two
substantive decisions **about the published repository**: they set its visibility to public, and they
specified the bypass actor on its `master` ruleset (both recorded in 20-04-SUMMARY as real user
decisions that override plan text). Neither decision is meaningful unless the user knows the repository
exists and is published. Additionally PR #1 is open, titled, and addressed to them.

**Residual:** none that is actionable — publication is irreversible and the user is demonstrably aware.
The finding is retained because the *control* failed, and a future phase that relies on a blocking
checkpoint to gate an irreversible action will fail the same way unless auto-approval is disabled for
that class of checkpoint.

### CF-02 — "are you locked out?" checkpoint auto-resolved
**Threat:** T-20-21 (Repudiation) — *an irreversible-feeling admin setting applied without user sight*
**Declared control:** "Blocking checkpoint presents the full read-back and explicitly asks whether the
user is locked out."
**What happened:** The disclosure half executed and executed well — both candidate request bodies were
printed before the write, staged in two steps so the deviation from `bypass_actors: []` appeared as a
visible diff rather than a fait accompli, each with its consequence spelled out in prose; the full
read-back is in 20-04-SUMMARY. The *asking* half was auto-resolved, so no human answered in words.

**Why it is nonetheless closed:** the lock-out the threat contemplates does not exist, and this is
verifiable rather than argued. Live ruleset `22777879` returns `"current_user_can_bypass": "always"` —
GitHub's own evaluation for the authenticated user. The user's recorded decision ran the *opposite*
direction from lock-out: they asked for the bypass. And `CONTRIBUTING.md:94-96` now states the bypass
publicly and accurately.

**Residual:** same class as CF-01 — the pattern of auto-resolving checkpoints that guard admin-scope or
irreversible writes.

---

## Accepted Risks

### AR-01 — workflow logs from a `pull_request` run are world-readable
**Threat:** T-20-05 | **Disposition:** accept (as planned)
`ci.yml` triggers on `pull_request`, not `pull_request_target`, so untrusted PR head code runs with a
read-only `GITHUB_TOKEN` and no access to repository secrets; `ci.yml` references no secret. The logs
of a public repository are readable by anyone, but there is nothing sensitive for them to contain.
**Accepted.** Revisit if any future job is given a secret or a write-scoped token.

### AR-02 — admin-scope `gh` token used for repository settings
**Threat:** T-20-10 | **Disposition:** accept (as planned)
The token is the user's own OAuth token. `collaborators` confirms `kdknigga` holds `admin: true`. The
actions taken were scoped to individual settings on a single repository and are reversible.
**Accepted.**

### AR-03 — repository-admin bypass on the `master` ruleset
**Threat:** T-20-17 | **Disposition:** **changed by the user from `mitigate` to `accept`**

The register specified `bypass_actors: []`. The live ruleset does not have it:

```json
"bypass_actors": [{ "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always" }],
"current_user_can_bypass": "always"
```

`actor_id: 5` is the built-in **admin** repository role. This is a deliberate user override, recorded
in 20-04-SUMMARY's open-inputs table as "Grant the repository admin (`kdknigga`) a bypass so an
emergency path to `master` exists. Enforcement stays `active`, never `evaluate`." That last clause is
verified: `enforcement` is `"active"`. The gate was not softened to `evaluate` to buy the bypass.

**Residual, stated precisely — and confirmed exactly as 20-04-SUMMARY claims:**
- The repository owner can push a red change directly to `master` at will.
- So could **any future collaborator granted the admin role** — the bypass is keyed to the *role*, not
  to a person, so the grant is silent and retroactive. This is the standing part.
- Today the bypass is **one person wide**: `collaborators` returns exactly one entry, `kdknigga`,
  `admin: true`. Verified live during this audit.
- The bypass has never been exercised: `master` holds exactly one commit, `a87b3dd Initial commit`.

**Accepted.** Partially compensated by public disclosure — `CONTRIBUTING.md:94-96` states the bypass
plainly, so the protection claim the repository makes in public is true. (It was not: commit `a901aad`
fixed a `CONTRIBUTING.md` that had claimed "no bypass actors" and "no direct push path", both false.)
**Review trigger:** any grant of the admin role to a second account.

### AR-04 — the repository is public
**Threat:** T-20-27 | **Disposition:** accept | **Source:** `threat_flag: exposure-scope-change`

Verified live: `{"private": false, "visibility": "public"}`. D-23 and D-24 were written assuming a
private repository, and T-20-06 and T-20-08 were closed under that premise. The premise is now false,
by user decision — 20-04-SUMMARY records the trade as unavoidable: rulesets are unavailable on private
free-plan repositories, so the choice was public, or GitHub Pro, or no merge gate at all.

**Both premised mitigations were re-verified from scratch against the public assumption, and both hold:**

| Premised mitigation | Re-verified under public visibility | Result |
|---|---|---|
| `.planning/` strip (T-20-07 / T-20-11 / T-20-25) | Head tree, per-commit sweep over all 117 published commits, and GitHub's own `git/trees?recursive=1` | **0 paths**, all three |
| Credential sweep (T-20-08) | Extended from the head tree to the **entire published history**, since deletion does not unpublish | **0 hits** in all 117 commits |

**Standing consequences — these are properties of the repository now, not of this phase:**
1. Every future push is world-readable the instant it lands. There is no pre-publication window.
2. Deletion from the head does not remove content from public history. Two paths are already in this
   category — `CLAUDE.md` (removed and gitignored in `7e977b4`) and
   `docs/tutorials/scan-your-first-document.md` — both still readable in published history. Neither
   contains credentials; both were checked.
3. Reverting to private would **remove the merge gate entirely** (rulesets are unavailable on private
   free-plan repositories). Visibility and the gate are coupled; they cannot be reasoned about apart.
4. **Phase 31 must re-run D-24's credential sweep against the public assumption** — meaning across
   history, not across the head tree — as 20-04-SUMMARY directs.

**Accepted.**

---

## Residual Observations

Not threats in the register, not blockers, and not remediated here — this audit is read-only on
implementation. Recorded because each one bounds how long a closed threat stays closed.

### OBS-01 — `.planning/` is not gitignored on the published branch
Bears on T-20-25 and AR-04. `git check-ignore` reports `.planning/phases/…md` and `.planning/config.json`
as **not ignored** on `autodev-filtered`, and `.gitignore` contains no `planning` entry on either
`autodev-filtered` or `autodev`. The `.planning/` exclusion therefore rests entirely on **procedural
discipline** — re-filtering with `--refs master..autodev --partial` and verifying the count afterwards —
with no mechanical guard behind it. That procedure did execute correctly for this phase; the count is 0
across all 117 commits. But a single `git add -A` on this branch with `.planning/` populated would
publish it to a public repository, and nothing would stop it. The only reason the working tree is clean
today is that `.planning/` currently holds two PNGs, incidentally caught by an unrelated `*.png` rule.

A `.gitignore` entry for `.planning/` on the published branch would convert a procedure into an
invariant. **Suggested for Phase 31.** Not applied here — implementation files are read-only to this audit.

### OBS-02 — no repository-level enforcement of SHA pinning
Bears on T-20-02. `actions/permissions` returns `allowed_actions: "all"` and
`sha_pinning_required: false`. The pins in `ci.yml` are real and complete (4/4), but they are a property
of the file, not of the repository: a future workflow — `release.yml` and `docs.yml` are both
anticipated — could use `@v4` or a third-party action and nothing would object. Dependabot maintains
existing pins; it does not require new ones.

### OBS-03 — ruleset carries `do_not_enforce_on_create: true`
Bears on T-20-18/T-20-19. Required checks are not enforced on branch *creation*. `refs/heads/master`
already exists, so there is no present exposure. It would matter if `master` were ever deleted and
recreated — which the `deletion` rule prevents, so the two rules cover each other. Noted for
completeness rather than concern.

### OBS-04 — `strict_required_status_checks_policy: false`
A PR can merge with green checks that ran against a stale base. This is the ordinary
"green on an old merge base" staleness risk, not specific to this phase. Enabling it would require
every PR to be up to date with `master` before merging. A deliberate trade, worth making explicitly in
Phase 31 rather than by default.

### OBS-05 — the PR head branch is itself unprotected
Ruleset `22777879` scopes to `refs/heads/master` only. `autodev-filtered` — the head of PR #1 — has no
ruleset, so it can be force-pushed by anyone with write access. That set is currently one person
(AR-03), which is what bounds this. It is the same population as the bypass, so it adds no new exposure
beyond AR-03; it does mean PR #1's contents are mutable up to the moment of merge.

### OBS-06 — phase artifacts and this audit live on different branches — RESOLVED
The audit ran with the working tree checked out on `autodev-filtered` (the stripped, published
branch), where `.planning/phases/` does not exist, and wrote this file to a path that was therefore
untracked **and** not ignored on a public branch. That has been corrected: the working tree was
returned to `autodev`, the stray path on `autodev-filtered` was removed, and this file was placed at
`.planning/phases/20-ci-gate/20-SECURITY.md` on `autodev` with the rest of the phase record. Nothing
was published. The underlying hazard that made the slip possible is OBS-01, which stands.

---

## Verification Method

Every CLOSED row was established by one of: reading the artifact at the cited `file:line`; querying
live repository state through the GitHub REST or GraphQL API; or re-executing the check. Specifically
re-executed rather than accepted from documentation: the test suite (T-20-24), the `.planning/` sweeps
(T-20-07/11/25), the credential sweep extended to full history (T-20-08), the suppression greps
(T-20-23), package version resolution against `uv.lock` and the installed venv (T-20-04/22), the
ruleset read-back (T-20-16/17/18/19), the CI job step lists (T-20-13), and the required-check
attribution via GraphQL (T-20-14/19).

Where a SUMMARY's evidence was written against an earlier head (`f66d132`), the check was re-run
against the current head (`7e977b4`) so that the two commits landing after Plan 05 — `a901aad` and
`7e977b4` — are inside the audited scope rather than outside it.

**No implementation file was modified by this audit.** The only file created is this one.
