# Phase 20: CI Gate - Pattern Map

**Mapped:** 2026-09-09
**Files analyzed:** 6 (3 new, 3 modified)
**Analogs found:** 4 / 6

> This is a CI/config/docs phase. Two of the four "analogs" are known-defective files
> (`release.yml`, `docs.yml` — M-25/M-26/N-31, owned by Phase 31). They are mapped as
> **read-only shape references** per D-06. Each entry below states explicitly what to
> imitate and what must NOT be copied.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `.github/workflows/ci.yml` (NEW) | config (CI workflow) | event-driven (push / pull_request) | `.github/workflows/release.yml` job `test` | role-match (structure only — analog is defective) |
| `.github/dependabot.yml` (NEW) | config (supply chain) | batch (scheduled) | *none in repo* | no analog — use RESEARCH.md § Code Examples |
| `pyproject.toml` → `[tool.pytest.ini_options]` (MOD, L141–150) | config (test) | n/a | itself (in-place block, L141–150) | exact — extend existing block |
| `pyproject.toml` → `[dependency-groups].dev` (MOD, L152–163) | config (deps) | n/a | itself (in-place list, L153–163) | exact — extend existing list |
| `CONTRIBUTING.md` (NEW, repo root) | docs | n/a | `README.md` (GitHub-rendered) + `docs/how-to/install-bare-metal.md` (prose) | partial — see the mkdocs warning below |
| `src/saneless/config.py:168`, `tests/test_scanner.py:706`, `:723` (MOD, D-16 fallout) | source (type fixes) | n/a | `config.py:134` (`cast`), `test_scanner.py:69,74-79` (`_snap_impl` delegation) | exact — both fix patterns already exist in-repo |

---

## Pattern Assignments

### `.github/workflows/ci.yml` (NEW — config, event-driven)

**Analog:** `.github/workflows/release.yml` (job `test`, lines 8–16) and `.github/workflows/docs.yml`

**House conventions to imitate (verbatim from both existing workflows):**

- Top-level key order is `name:` → blank line → `on:` → (`permissions:`) → `jobs:`.
- 2-space indent throughout; job steps are a `- ` list under `steps:` at 6 spaces.
- Bare `- run: <one-liner>` with **no** `name:` for simple commands. Only multi-line
  `run: |` blocks get a `name:` (there is currently no example — `ci.yml` introduces the
  first, so RESEARCH.md's `name: Install SANE development headers` is a new-but-consistent choice).
- Every Python command is `uv run <tool>` (never bare `ruff`/`pytest`). See `release.yml:14-16`.
- `runs-on: ubuntu-latest` on every job.
- `permissions:` appears at **job** level in `release.yml` (lines 22–23, 33–35). `docs.yml`
  puts it at **workflow** level (lines 7–8). `ci.yml` follows `docs.yml`'s placement
  (workflow-level) but with `contents: read` — per Claude's Discretion in CONTEXT.md.

**Existing `release.yml` shape — the closest analog for a check job** (`release.yml:8-16`):

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v7
      - run: uv sync
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pytest
```

That is the *only* structural thing to take from it: checkout → setup-uv → sync → `uv run` checks.

**DO NOT COPY from `release.yml`:**

| Line | Defect | What `ci.yml` does instead |
|---|---|---|
| `:13` `- run: uv sync` | no `--locked` → CI silently re-resolves around a stale lockfile | `uv sync --locked` |
| `:11,25,37` `actions/checkout@v5` | floating major tag, and 2 majors stale | `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1` |
| `:12,26` `astral-sh/setup-uv@v7` | floating major tag, 3 majors stale | `astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1` |
| `:28` `pypa/gh-action-pypi-publish@v1.12` | **unresolvable ref** (M-26) | not used in `ci.yml` at all |
| `:16` `- run: uv run pytest` | unfiltered — collects the 8 browser tests | `uv run pytest -m "not browser"` |
| (absent) | no `libsane-dev` apt step → `python-sane` sdist build fails | apt step **before** `uv sync --locked` in every job |
| (absent) | no `ty`, no `pyrefly` | both, as separate `- run:` steps |
| (absent) | no `timeout-minutes`, no `concurrency` | both present |

**DO NOT COPY from `docs.yml`:**

| Line | Defect | Note |
|---|---|---|
| `:5` `branches: [main]` | `main` does not exist in this repo — the workflow has never run (N-31) | **D-01: use `branches: [master]`.** This is the single highest-value "do not copy" in the phase. |
| `:8` `contents: write` | over-privileged for a check workflow | `ci.yml` uses `contents: read` |
| `:15-18` `actions/setup-python` + `pip install mkdocs-material` | unpinned `pip install`; violates CLAUDE.md's uv-only rule | `ci.yml` uses `astral-sh/setup-uv` + `uv sync --locked` only |

**Target content:** RESEARCH.md § Code Examples → `.github/workflows/ci.yml` (lines 537–584) is
complete and already reflects all of the above. Copy it, do not re-derive it.

---

### `.github/dependabot.yml` (NEW — config, batch)

**Analog:** none. `.github/` contains only `workflows/docs.yml` and `workflows/release.yml`;
there is no `dependabot.yml`, no `CODEOWNERS`, no issue templates.

**Instruction to the planner:** take the shape verbatim from RESEARCH.md § Code Examples →
`.github/dependabot.yml` (lines 594–602). Three required keys: `package-ecosystem`,
`directory: "/"`, `schedule.interval`. `version: 2` at top.

**Cross-file constraint (D-18):** this file and `ci.yml` must land in the **same commit**.
Pinning without Dependabot freezes the pins permanently.

**Consequence to document in `CONTRIBUTING.md`:** Dependabot will also open PRs against
`release.yml` and `docs.yml` actions, which this phase deliberately does not own (D-06/D-19).
A sentence in `CONTRIBUTING.md` prevents that being read as scope creep.

---

### `pyproject.toml` → `[tool.pytest.ini_options]` (MODIFIED — config, D-11..D-14)

**Analog:** the block itself. Current state, `pyproject.toml:141-150`, verbatim:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "browser: Playwright browser tests (requires chromium)",
]
addopts = ["-ra", "--strict-markers", "--strict-config"]
strict_markers = true
strict_config = true
xfail_strict = true
filterwarnings = ["error"]
```

**Formatting conventions to match exactly:**
- Double quotes on all strings (matches Ruff's Black-compatible format setting).
- `key = value` with single spaces around `=`.
- Scalar booleans unquoted and lowercase.
- Multi-line arrays use 4-space indent with a trailing comma (see `markers`); single-element
  arrays that fit on one line stay inline (see `testpaths`, `filterwarnings`).
- Keys are **not** alphabetized — they are grouped by concern (paths, markers, addopts,
  strictness, warnings). Append `timeout` / `timeout_method` at the end of the block, after
  `filterwarnings`, as a new "hang guard" group.

**Target addition (D-12/D-13):**
```toml
timeout = 60
timeout_method = "signal"
```

**Load-bearing ordering constraint:** `--strict-config` is already in `addopts` (line 146),
which makes an unknown ini key a **hard error on every pytest invocation**. `timeout` and
`timeout_method` are only known keys once `pytest-timeout` is installed. Therefore the
`[dependency-groups]` edit, the `[tool.pytest.ini_options]` edit, and the regenerated `uv.lock`
must be **one atomic commit** — any intermediate state breaks `uv run pytest`, `uv run prek run`,
and the pre-commit hooks.

---

### `pyproject.toml` → `[dependency-groups].dev` (MODIFIED — config, D-15)

**Analog:** the list itself. Current state, `pyproject.toml:152-163`, verbatim:

```toml
[dependency-groups]
dev = [
    "httpx>=0.28.1",
    "playwright>=1.58.0",
    "pytest-playwright>=0.7.0",
    "prek>=0.3.5",
    "pyrefly>=0.55.0",
    "pytest>=9.0.2",
    "ruff>=0.15.5",
    "ty>=0.0.21",
    "mkdocs-material>=9.7.6",
]
```

**Formatting conventions to match:**
- 4-space indent, one requirement per line, double quotes, trailing comma on the last entry.
- `>=` lower bounds only — **no** upper caps, no `==` pins, no extras markers anywhere.
- **Not alphabetized.** The existing order is `httpx, playwright, pytest-playwright, prek,
  pyrefly, pytest, ruff, ty, mkdocs-material` — arbitrary. Do not "helpfully" sort it; that
  produces a large diff unrelated to the phase.
- **No inline comments anywhere in this list.** RESEARCH.md's example block (line 629) shows
  `"pytest-timeout>=2.4.0",   # D-15 — plain requirement, not optional` — **strip that comment**;
  it does not match the file's convention and `uv add --dev` will not produce it.

**Target addition (D-15):** `"pytest-timeout>=2.4.0",` — added via `uv add --dev pytest-timeout`,
which appends and regenerates `uv.lock` in one step.

**Second, separate commit (D-16/D-17):** `uv add --dev "ty@latest" "pyrefly@latest"` bumps the
`ty>=0.0.21` and `pyrefly>=0.55.0` lines. Do not collapse into the first commit.

---

### `CONTRIBUTING.md` (NEW, repo root — docs)

**Analogs:** `README.md` (for GitHub-rendered markdown conventions) and
`docs/how-to/install-bare-metal.md` (for prose voice and command presentation).

**⚠️ CRITICAL — the two doc sets use DIFFERENT markdown dialects.**
`docs/**` is rendered by **mkdocs-material** and uses syntax GitHub does not understand.
`CONTRIBUTING.md` lives at the repo root and is rendered by **GitHub**, so it must follow
`README.md`'s dialect, not `docs/`'s.

**DO NOT COPY into `CONTRIBUTING.md`** (from `docs/how-to/install-bare-metal.md:25-40`):
```markdown
=== "pipx (recommended)"          <- mkdocs content tabs; renders as literal text on GitHub

    ```bash
    pipx install saneless
    ```

!!! tip                            <- mkdocs admonition; renders as literal text on GitHub
    Prefer `pipx` if you have it installed.
```

**DO copy — `README.md` conventions:**

Heading + fenced-command style (`README.md:5-24`):
```markdown
# saneless

SANE scanner to paperless-ngx bridge. Web UI and CLI for triggering scans, ...

## System Requirements

`python-sane` requires the SANE development headers to compile:

| Distro | Package |
|--------|---------|
| Debian / Ubuntu | `libsane-dev` |
| Fedora / RHEL / Rocky | `sane-backends-devel` |

Install before `pip install saneless`:

```bash
# Debian/Ubuntu
sudo apt-get install libsane-dev
```
```

Extracted conventions:
- Single `#` H1 at the top, then `##` section headings. No `###` used anywhere in `README.md`.
- Sentence-case headings ("System Requirements", "Configuration").
- Every code fence carries a language tag — `bash` or `toml` in `README.md`, and `yaml`/`bash`
  will be needed here.
- Commands presented as a fenced `bash` block, one command per line, with a `#` comment above
  a group when the context needs naming (`README.md:19`).
- Inline literals (package names, file names, flags) in single backticks.
- Multi-command blocks list commands bare with trailing `#` comments aligned loosely
  (`README.md:43-47`) — the model for listing the five checks.
- Tables use `| Col | Col |` with a `|---|---|`-style separator.
- Prose uses `--` (double hyphen) rather than an em dash (`README.md:74-77`).

**Command form this project documents (CLAUDE.md § Commands, and `.pre-commit-config.yaml:50,58`):**
always `uv run <tool>`, never bare `ruff`/`pytest`/`ty`, and `uv run prek run` (never
`pre-commit run`). `CONTRIBUTING.md`'s five-check list must use these exact forms:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pyrefly check
uv run pytest -m "not browser"
```

**D-04 content requirements** (all five must appear): the five checks; the `libsane-dev`
prerequisite; `uv sync --locked`; `uv run prek run` locally; and that `--no-verify` no longer
bypasses the gate. Plus, from RESEARCH.md Pitfall 2, note that with `bypass_actors: []` all
future `master` changes must go through a PR — including the maintainer's.

---

### The three `# type: ignore[...]` sites (MODIFIED — D-16 fallout, second commit only)

RESEARCH.md Pitfall 8 established the cause: `ty` 0.0.80 no longer honours `# type: ignore`
carrying **any** bracketed code. CLAUDE.md forbids suppressions, so the target at each site is
a **real fix**, not a swap to `# ty: ignore[...]`. **Both fix patterns already exist in this
codebase.**

#### Site 1 — `src/saneless/config.py:168` (rule: `unknown-argument`)

Current code, `config.py:162-169`:
```python
def _build_settings(
    toml_file: Path | None = None,
) -> Settings:
    """Build Settings, converting extra-field errors to user-friendly messages."""
    try:
        if toml_file is not None:
            return Settings(_toml_file=toml_file)  # type: ignore[call-arg] -- ty cannot see BaseSettings dynamic __init__ kwargs
        return Settings()
```

**What it is actually suppressing:** `_toml_file` is not a declared field on `Settings`
(`config.py:99-114`). It is a private init kwarg consumed at runtime by the class's own
`settings_customise_sources` override, which pops it out of `init_kwargs`:

```python
# config.py:133-141  — the mechanism ty cannot see
# init_settings is always an InitSettingsSource at runtime
init_src = cast("InitSettingsSource", init_settings)
toml_file = init_src.init_kwargs.pop("_toml_file", None)

if toml_file is not None:
    toml_source = TomlConfigSettingsSource(
        settings_cls,
        toml_file=toml_file,
    )
```

**In-repo fix precedent — `cast` is already the established tool here.** `config.py:14`
already imports `cast` from `typing`, and `config.py:134` already uses the
`cast("Name", value)` string-literal form to bridge exactly this kind of
"runtime-true, statically-invisible" gap. The same file, four lines apart, is the analog.
`from __future__ import annotations` is present (`config.py:9`), and the `TYPE_CHECKING`-only
import block (`config.py:26-27`) is the pattern for any type-only name a fix needs.

#### Sites 2 & 3 — `tests/test_scanner.py:706` and `:723` (rule: `invalid-assignment`)

Current code, `test_scanner.py:704-710` (`:722-727` is identical in shape):
```python
# Add "Auto" to available sources
mock_dev = mock_sane_module._mock_dev
mock_dev.get_options = lambda: [  # type: ignore[assignment]
    (1, "source", "Source", "", 3, 0, 1, 5, ["Flatbed", "ADF", "Auto"]),
    (2, "resolution", "Res", "", 1, 4, 1, 5, [300]),
    (3, "mode", "Mode", "", 3, 0, 1, 5, ["color"]),
]
```

**What it is actually suppressing:** assigning a plain `lambda` over the bound method
`MockSaneDev.get_options`, declared at `test_scanner.py:95` as
`def get_options(self) -> list[tuple]:`. A `Callable[[], list[tuple]]` is not assignable to a
method-typed attribute — a genuine type error, not a checker quirk.

**In-repo fix precedent — the pluggable-impl attribute pattern, in the very same class.**
`MockSaneDev` already solves this exact problem for `snap()`. From `test_scanner.py:69` and
`:74-79`:

```python
        self._snap_impl: MagicMock | None = None
    ...
    def snap(self) -> Image.Image:
        """Return a simple test image, or delegate to _snap_impl if set."""
        if self._snap_impl is not None:
            return self._snap_impl()
        self._snap_calls.append({})
        return Image.new("RGB", (100, 100), "white")
```

Callers then write `mock_dev._snap_impl = MagicMock(return_value=img)` (`test_scanner.py:688`,
`:728-730`) — a plain attribute assignment with **no suppression needed**. Applying the
identical shape to `get_options` (an `_options_impl: list[tuple] | None = None` attribute plus
a delegating `get_options`) removes both suppressions and matches a pattern the class already
uses twice.

A second, weaker precedent for the "make the mock pluggable via `__init__`" variant is the
sibling mock at `test_scanner.py:182-188`, which takes `multi_scan`/`cancel`/`close` callables
as constructor args and stores them as `self._multi_scan_fn` etc.

**Ruff constraint on any fix here:** `tests/**/*.py` per-file-ignores (`pyproject.toml:129-133`)
cover only `S101`, `ARG`, and `S104/5/6`. The `D` docstring rules still apply, so a new or
modified `get_options` needs a docstring — every existing method in these mock classes has one.

---

## Shared Patterns

### The five checks must be spelled identically in three places

**Source:** `.pre-commit-config.yaml:39-62` + CLAUDE.md § Commands
**Apply to:** `ci.yml` steps, `CONTRIBUTING.md` prose, and any plan verification step.

```yaml
# .pre-commit-config.yaml:39-62 — the local hooks CI must mirror
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.15.7
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: local
    hooks:
      - id: ty-checker
        name: ty type checker
        entry: uv run ty check
        language: system
        types: [python]
        pass_filenames: false
        always_run: true

      - id: pyrefly-checker
        name: pyrefly type checker
        entry: uv run pyrefly check
        language: system
        types: [python]
        pass_filenames: false
        always_run: true
```

CI's four lint/type commands must be `uv run ty check` and `uv run pyrefly check` verbatim
(no flags — RESEARCH.md Pitfall 9 notes pyrefly 1.x renamed `--python-interpreter`, which only
matters if flags are ever added). CI adds the fifth check, `uv run pytest -m "not browser"`,
which `prek` does not run.

### `uv run` prefix, never bare tool invocation

**Source:** CLAUDE.md § Commands; `release.yml:14-16`; `.pre-commit-config.yaml:50,58`
**Apply to:** `ci.yml`, `CONTRIBUTING.md`, every plan task command.
CLAUDE.md is explicit: `uv` only — not pip, not poetry, not conda. `docs.yml:18`'s
`pip install mkdocs-material` is the counterexample and is Phase 31's to fix.

### `libsane-dev` as a documented prerequisite

**Source:** `README.md:5-24` and `docs/how-to/install-bare-metal.md:12-21`
**Apply to:** `ci.yml`'s apt step and `CONTRIBUTING.md`'s prerequisites section.
The repo already documents this in a Distro/Package table twice. `CONTRIBUTING.md` can point at
the `README.md` table rather than reproduce it a third time; `ci.yml` needs only the
Debian/Ubuntu row (`sudo apt-get install libsane-dev`) since the runner is Ubuntu 24.04.

### Suppression-free type checking

**Source:** CLAUDE.md § Code Quality Standards (`# type: ignore` / `# noqa` / rule-disabling all forbidden)
**Apply to:** all three D-16 fix sites.
The three existing suppressions already violate this standing rule; removing them is a bonus win
of D-16, not a regression risk.

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `.github/dependabot.yml` | config (supply chain) | batch | No Dependabot config, no other non-workflow `.github/` file exists. Use RESEARCH.md § Code Examples (L594–602). |
| `CONTRIBUTING.md` (structure) | docs | n/a | No contributing/development doc exists anywhere — `README.md` has no Development section (it ends at line 77) and `docs/` is user-facing Diataxis only. Prose *conventions* are borrowed from `README.md`, but the document's structure is new. |
| Branch ruleset JSON | out-of-tree config | n/a | Lives in GitHub repo settings, not in git. `GET /rulesets` currently returns `[]`. Use RESEARCH.md § Code Examples (L643–695) verbatim. |

---

## Metadata

**Analog search scope:** `.github/`, repo root (`*.md`, `pyproject.toml`,
`.pre-commit-config.yaml`), `docs/`, `src/saneless/`, `tests/`
**Files read:** 9 (`release.yml`, `docs.yml`, `pyproject.toml`, `.pre-commit-config.yaml`,
`README.md`, `docs/how-to/install-bare-metal.md`, `src/saneless/config.py`,
`tests/test_scanner.py`, plus a `.github/` tree listing)
**Pattern extraction date:** 2026-09-09
