---
phase: 31-delivery-identity-and-documentation-accuracy
plan: 01
subsystem: packaging
tags: [packaging, pep-639, cli, versioning, supply-chain]
requires: []
provides:
  - "pyproject.toml declaring version 0.2.0, PEP 639 license keys and Alpha maturity"
  - "uv.lock pinning zizmor 1.30.1, making DLVR-03 verifiable"
  - "saneless --version, sourced from installed metadata"
  - "five static packaging assertions in the deployment-config harness"
affects:
  - "Plan 02 (DLVR-01 URL rename) — [project.urls] left untouched on purpose"
  - "Plan 04 (zizmor findings) — the tool is now installed and invocable"
  - "Plan 09 (TestPyPI release rehearsal) — the version assertion admits 0.2.0-rc.N"
tech-stack:
  added:
    - "zizmor 1.30.1 (dev group, GitHub Actions static analysis)"
  patterns:
    - "PEP 639 SPDX license expression + license-files, replacing the license table"
    - "click.version_option(package_name=...) reading importlib.metadata"
    - "static text assertions in tests/test_deployment_config.py for config shape"
key-files:
  created: []
  modified:
    - pyproject.toml
    - uv.lock
    - src/saneless/cli.py
    - tests/test_cli.py
    - tests/test_deployment_config.py
decisions:
  - "The legacy License :: classifier's absence is pinned by a static test, because neither uv_build 0.10.3 nor twine check rejects it alongside License-Expression"
  - "The version assertion accepts ^0\\.2\\.0(-rc\\.\\d+)?$ so Plan 09's RC rehearsal cannot turn the suite red and invite a weakening"
  - "The --version expectation is read from importlib.metadata, never from pyproject.toml, so a venv with stale installed metadata fails on sync, not on the option"
metrics:
  duration: "~35 min"
  completed: 2026-09-18
  tasks: 2
  commits: 4
  files_changed: 5
---

# Phase 31 Plan 01: Packaging Foundation Summary

Shipped the 0.2.0 packaging identity — PEP 639 license keys, Alpha maturity, `saneless --version` from installed metadata — and pinned `zizmor` so the rest of Phase 31 is verifiable.

## What Was Built

**Task 1 — packaging identity** (`0efd32f` RED, `d117467` GREEN)

`pyproject.toml` `[project]` took exactly four edits:

| Before | After |
|--------|-------|
| `version = "0.1.0"` | `version = "0.2.0"` |
| `license = {text = "MIT"}` | `license = "MIT"` + `license-files = ["LICENSE"]` |
| `"Development Status :: 4 - Beta"` | `"Development Status :: 3 - Alpha"` |
| `"License :: OSI Approved :: MIT License"` | *(deleted)* |

`uv add --dev zizmor` appended `zizmor>=1.30.1` to the dev group without re-sorting it and
regenerated `uv.lock` (zizmor 1.30.1 pinned). `uv sync` then made the venv's installed metadata
report 0.2.0, which is what every `--version` assertion in Task 2 reads.

Five new static tests landed in `tests/test_deployment_config.py` under a
`Phase 31: packaging identity (D-01, D-02, D-05, DLVR-08)` banner, using the existing
`_read(PYPROJECT)` helper and a new module-level `PYPROJECT` path constant:
`test_pyproject_declares_the_0_2_0_series`, `test_pyproject_uses_the_pep_639_license_keys`,
`test_pyproject_has_no_legacy_license_classifier`, `test_pyproject_declares_alpha_maturity`,
`test_pyproject_distribution_name_is_saneless`. The module docstring gained a Phase 31 paragraph,
matching the per-phase convention the file already follows.

**Task 2 — `saneless --version`** (`f15900a` RED, `552cf2b` GREEN)

`@click.version_option(package_name="saneless")` sits between `@click.group(cls=_GuardedGroup)`
and the `--config` option, with a comment recording the three things it deliberately does *not*
do: no custom message (D-04 wants click's default; `saneless doctor` covers diagnostics), no
short flag (`-v` is `--verbose` on the same group and click binds the last declaration silently),
and no local version constant (D-03 makes `pyproject.toml` the single source).

`TestVersionOption` in `tests/test_cli.py` carries three tests: the output-shape contract, the
no-config path, and the `-v` collision guard.

## Artifact Evidence (DLVR-08)

The license claims were proven by unzipping a real wheel, not by reading the config:

```
$ unzip -l dist/saneless-0.2.0-py3-none-any.whl | grep licenses
     1068  saneless-0.2.0.dist-info/licenses/LICENSE

$ unzip -p dist/saneless-0.2.0-py3-none-any.whl saneless-0.2.0.dist-info/METADATA | grep License
License-Expression: MIT
License-File: LICENSE
```

`Classifier: Development Status :: 3 - Alpha` is present in METADATA and no
`Classifier: License :: OSI Approved` line remains.

## Verification Results

| Check | Result |
|-------|--------|
| `uv run pytest -m "not browser and not sane_hardware" -q` | 3074 passed |
| `uv run pytest tests/test_deployment_config.py -q` | 52 passed (47 before, +5) |
| `uv run ruff check .` | No issues found |
| `uv run ruff format --check .` | 63 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --all-files` | all hooks passed |
| `uv run prek run --stage pre-push --all-files` | all hooks passed |
| `uv run zizmor --version` | `zizmor 1.30.1` |
| `uv run saneless --version` | `saneless, version 0.2.0`, exit 0 |
| `uv run saneless --config /nonexistent/x.toml --version` | exit 0 |
| `uv run saneless -v devices --help` | exit 0, no version output |

Acceptance greps: `version = "0.2.0"` 1, `license = "MIT"` 1, `license-files = ["LICENSE"]` 1,
`License :: OSI Approved` 0, `Development Status :: 3 - Alpha` 1, `kris-knigga` 3 (unchanged —
Plan 02 owns that rename), `zizmor` in `uv.lock` 14 occurrences,
`version_option(package_name="saneless")` 1, `message=` 0.

`git diff --numstat bdc85ba..HEAD` shows **zero deleted lines in either test file** (86 and 97
insertions, 0 deletions), so no pre-existing test was modified or weakened. No `# noqa`,
`# type: ignore` or rule-disabling was added anywhere.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Reworded a code comment that tripped its own acceptance guard**

- **Found during:** Task 2
- **Issue:** the explanatory comment above `@click.version_option` originally read
  "No message= argument", which pushed `grep -c 'message=' src/saneless/cli.py` from 0 to 1 and
  broke the plan's "`message=` does not increase" criterion. The criterion exists as a
  machine-checkable guard that no custom version message was passed; a comment satisfying the
  pattern in prose defeats it for future readers and future greps.
- **Fix:** reworded to "No custom message is passed", keeping the explanation and restoring the
  grep to 0. No behavioural change.
- **Files modified:** `src/saneless/cli.py`
- **Commit:** `552cf2b`

### Notes, not deviations

- **Two tests passed on their RED run, by design.**
  `test_pyproject_distribution_name_is_saneless` and `test_verbose_short_flag_is_still_verbose`
  are state-pinning guards, not behaviour-driving tests: the first pins DLVR-01's "the
  distribution name does not change" clause, the second guards behaviour that must *survive*
  the new option. The TDD fail-fast rule was considered and does not apply — neither test
  describes behaviour this plan adds. The four/two genuinely new behaviours did fail red first
  (4 of 5 packaging tests, 2 of 3 CLI tests).
- **The worktree base needed correcting.** The agent branch was spawned at `ed2d620`, whose
  merge-base with the plan base `bdc85ba` was `a87b3dd`. The startup guard's
  `git reset --hard bdc85ba` corrected it before any work began.
- **`docs/how-to/install-bare-metal.md:47` needed no edit.** It already instructs the reader to
  run `saneless --version`; that sentence became true with this change, per RESEARCH § 2 gotcha 3.
  Audit row 15 is closed by Task 2, not by a documentation change.
- **`dist/` stayed untracked.** It is gitignored at `.gitignore:37`, so the wheel builds left the
  tree clean (`git ls-files -o --exclude-standard` returned nothing after each build).

## Authentication Gates

None.

## Known Stubs

None. Every artifact this plan claims was executed and its output inspected.

## Threat Flags

None. The two trust boundaries this plan touches were both mitigated as planned:

- **T-31-SC** (PyPI → workstation): `zizmor` entered via `uv add --dev`, version-pinned by
  `uv.lock` rather than a floating range. RESEARCH's Package Legitimacy Audit dispositions it
  Approved (slopcheck `[OK]`, PyPI first upload 2024-12-06, 71 releases, source at
  github.com/zizmorcore/zizmor), so no legitimacy checkpoint was required. The install resolved
  on the first attempt with no name ambiguity.
- **T-31-12 / T-31-13** (published metadata): the distribution name, license expression and
  bundled LICENSE are each pinned by an assertion or proven against the built wheel.

No new network endpoint, auth path, file-access pattern or schema change was introduced.

## Commits

| Gate | Commit | Message |
|------|--------|---------|
| RED | `0efd32f` | `test(31-01): pin the 0.2.0 packaging identity in pyproject.toml` |
| GREEN | `d117467` | `feat(31-01): ship the 0.2.0 packaging identity and add zizmor` |
| RED | `f15900a` | `test(31-01): add the failing --version contract tests` |
| GREEN | `552cf2b` | `feat(31-01): add saneless --version` |

No REFACTOR commit: neither change left anything to clean up.

## TDD Gate Compliance

Both tasks ran a full RED → GREEN cycle with the RED gate committed separately and demonstrably
failing before implementation. Gate sequence in `git log`: `test` → `feat` → `test` → `feat`.

## Requirements Satisfied

- **DLVR-03** — `zizmor` resolves from `uv.lock`; its findings are Plan 04's work
- **DLVR-08** — the wheel carries `dist-info/licenses/LICENSE` and `License-Expression: MIT`
- **DLVR-10** — `saneless --version` prints the installed version and exits 0
- **DLVR-01 (partial)** — the `saneless` distribution name is now asserted; the owner URL rename
  remains Plan 02's work and `[project.urls]` was deliberately left untouched

## Self-Check: PASSED

All five modified files exist on disk; all four commit hashes resolve on the agent branch.
