# Phase 9: Enable Pytest Strict Mode and Test Quality Parity - Research

**Researched:** 2026-03-21
**Domain:** Python test quality enforcement (ruff, ty, pyrefly, pytest strict mode)
**Confidence:** HIGH

## Summary

This phase brings all 14 test files (plus conftest.py) to the same code quality bar as production code. The current state has 676 ruff violations (349 ANN001, 147 ANN201, 77 PLC0415, 61 ANN202, plus minor ANN categories) suppressed by per-file-ignores. Both ty and pyrefly exclude tests/ entirely. Pytest has no strict mode settings.

The work divides into three areas: (1) fix type checker errors and remove ty/pyrefly exclusions, (2) add annotations and docstrings while removing ruff exemptions, and (3) enable pytest strict configuration. A critical finding is that `filterwarnings = ["error"]` will surface unclosed SQLite connections in test_pipeline, test_web, test_worker, test_job, and test_cli -- these need explicit `JobStore.close()` calls or a cleanup fixture.

**Primary recommendation:** Execute in three plans matching CONTEXT.md structure. Plan 1 is small (type checker fixes). Plan 2 is the largest (676 ruff violations). Plan 3 is config + ResourceWarning fixes.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Full ANN compliance on all test files -- same bar as production code
- Remove the ANN exemption from per-file-ignores entirely
- Use precise types where possible (actual types, not MagicMock/Any)
- Fixtures return concrete types (e.g., StubScanner, not ScannerBackend)
- Enforce D rules on tests -- same as production (every test function, class, module gets a docstring)
- Remove ALL non-test-inherent per-file-ignores for tests/ in one pass
- Keep only: S101 (assert), S104/S105/S106 (hardcoded test values)
- Remove: ANN, PLC0415, and any others that aren't test-inherent
- Each remaining justified suppression gets per-line `# noqa:` with comment
- Rename `_device_id` / `_settings` in `_BrowserTestScanner` to match parent ABC signatures
- Replace mock + reassignment pattern in test_scanner.py with concrete stub/fake classes
- Eliminate all 6 `type: ignore` comments in test_scanner.py
- Add `assert fetched is not None` narrowing in test_job.py
- Add explicit `import logging.handlers` in test_logging.py
- Remove `exclude = ["tests/"]` from `[tool.ty.src]` in pyproject.toml
- Remove `project_excludes = ["tests/"]` from `[tool.pyrefly]` in pyproject.toml
- Enable `strict_markers = true`, `strict_config = true`, `xfail_strict = true`
- Enable `filterwarnings = ["error"]` -- unhandled warnings fail tests, no allowlisting
- Add `addopts = ["-ra", "--strict-markers", "--strict-config"]`
- Move most of the 77 lazy imports to top-level; keep only genuinely justified ones
- Remove blanket PLC0415 exemption from per-file-ignores
- 3 plans structure as specified in CONTEXT.md
- Every plan must pass `uv run pytest` with all 226 tests green
- No temporary xfail markers

### Claude's Discretion
- Which lazy imports are justified vs. should move to top-level (case-by-case evaluation)
- Exact stub/fake class designs for test_scanner.py replacements
- Order of annotation additions within a plan
- Docstring content for test functions

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope
</user_constraints>

## Current Violation Inventory

### Ruff Violations (with exemptions removed)
| Rule | Count | Description |
|------|-------|-------------|
| ANN001 | 349 | Missing type annotation for function argument |
| ANN201 | 147 | Missing return type for undocumented public function |
| PLC0415 | 77 | Import outside top level |
| ANN202 | 61 | Missing return type for private function |
| ANN003 | 23 | Missing type annotation for **kwargs |
| ANN002 | 16 | Missing type annotation for *args |
| ANN204 | 2 | Missing return type for special method (__init__ etc.) |
| ANN205 | 1 | Missing return type for static method |
| **Total** | **676** | |

### Type Checker Errors (ty)
| File | Line(s) | Error | Fix |
|------|---------|-------|-----|
| test_browser.py | 56, 64 | invalid-method-override (underscore param names) | Rename `_device_id` -> `device_id`, `_settings` -> `settings` |

### Type Checker Errors (pyrefly)
| File | Line(s) | Error | Fix |
|------|---------|-------|-----|
| test_browser.py | 56, 64 | bad-param-name-override | Same as ty fix above |
| test_job.py | 114, 145 | missing-attribute (NoneType) | Add `assert fetched is not None` |
| test_logging.py | 30, 70, 100 | implicit-import | Add `import logging.handlers` |

### Type Ignore Comments (test_scanner.py)
| Line | Comment | Fix Strategy |
|------|---------|-------------|
| 225 | `type: ignore[abstract]` | Expected -- instantiating ABC for test. Replace with `# type: ignore[abstract]` or use Protocol |
| 471 | `type: ignore[assignment]` | Replace mock_dev with concrete stub |
| 612 | `type: ignore[assignment]` | Replace mock_dev with concrete stub |
| 658 | `type: ignore[assignment]` | Replace mock_dev with concrete stub |
| 705-706 | `type: ignore[assignment]` | Replace mock_dev with concrete stub |

### Docstring Gaps
| File | Missing Count |
|------|--------------|
| test_paperless.py | 20 |
| test_worker.py | 11 |
| test_scanner.py | 4 |
| test_cli.py | 1 |
| test_pdf.py | 1 |
| test_web.py | 1 |
| **Total** | **38** |

### Return Annotation Gaps
| File | Functions | Missing Return |
|------|-----------|---------------|
| test_cli.py | 53 | 52 |
| test_worker.py | 38 | 36 |
| test_pipeline.py | 30 | 28 |
| test_config.py | 22 | 22 |
| test_paperless.py | 42 | 22 |
| test_pages.py | 15 | 15 |
| test_logging.py | 9 | 9 |
| conftest.py | 11 | 11 |
| test_scanner.py | 58 | 6 |
| test_job.py | 9 | 4 |
| test_web.py | 28 | 4 |
| test_browser.py | 12 | 1 |

### Lazy Import Distribution
| File | Count |
|------|-------|
| test_cli.py | 29 |
| test_scanner.py | 20 |
| test_worker.py | 14 |
| test_pdf.py | 5 |
| test_job.py | 4 |
| test_browser.py | 3 |
| conftest.py | 2 |
| test_paperless.py | 1 |
| test_pipeline.py | 1 |
| test_web.py | 1 |
| **Total** | **80** |

## Architecture Patterns

### Annotation Patterns for Test Code

**Test functions:** Most return `None`. Use `-> None` on every test function and fixture that doesn't return a value.

```python
def test_example(self, mock_scanner: MagicMock) -> None:
    """Verify example behavior."""
    ...
```

**Fixtures returning values:** Use the concrete return type.

```python
@pytest.fixture
def default_settings() -> Settings:
    """Return a Settings instance with test-safe defaults."""
    ...
```

**Fixtures with monkeypatch/tmp_path:** Type the pytest parameters.

```python
@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all SANELESS_* env vars before each test."""
    ...
```

**Common pytest fixture types:**
| Parameter | Type |
|-----------|------|
| `tmp_path` | `pathlib.Path` |
| `monkeypatch` | `pytest.MonkeyPatch` |
| `capsys` | `pytest.CaptureFixture[str]` |
| `capfd` | `pytest.CaptureFixture[str]` |
| `request` | `pytest.FixtureRequest` |

### Stub/Fake Pattern for test_scanner.py

Replace `mock_dev.multi_scan = _raising_multi_scan  # type: ignore[assignment]` with a concrete fake device class:

```python
class _FakeSaneDevice:
    """Fake SANE device for testing scan_pages behavior."""

    def __init__(self, multi_scan_fn: Callable[[], Iterator[Image.Image]] | None = None) -> None:
        self.multi_scan = multi_scan_fn or (lambda: iter([]))
        # Add other attributes as needed
```

This avoids all `type: ignore[assignment]` comments since attributes are set at construction time.

### Lazy Import Evaluation Criteria

**Move to top-level (most cases):**
- Standard library imports (`pathlib`, `json`, `io`, etc.)
- Project imports (`from saneless.config import ...`)
- Third-party test utilities (`from unittest.mock import ...`)

**Keep lazy with per-line `# noqa: PLC0415` (rare):**
- Imports that trigger side effects at module load (e.g., `sane.init()`)
- Imports that depend on monkeypatched state being active
- Imports inside conftest fixtures where the import IS the test setup

## Common Pitfalls

### Pitfall 1: ResourceWarning from Unclosed SQLite Connections
**What goes wrong:** `filterwarnings = ["error"]` promotes `ResourceWarning` to `PytestUnraisableExceptionWarning` which fails tests.
**Why it happens:** `JobStore` opens a SQLite connection in `__init__` but tests create `JobStore` instances (via application lifespan or directly) without calling `close()`.
**How to avoid:** Create a fixture that ensures `JobStore.close()` is called in teardown, or fix the test helpers to properly close stores.
**Affected tests:** test_pipeline, test_web, test_worker, test_job, test_cli (any test touching JobStore).
**Warning signs:** `ResourceWarning: unclosed database in <sqlite3.Connection>` in test output.

### Pitfall 2: Annotation of MagicMock Fixtures
**What goes wrong:** Annotating `mock_scanner` as `MagicMock` satisfies ANN but loses type information.
**Why it happens:** conftest.py uses `MagicMock(spec=ScannerBackend)` which returns `MagicMock`.
**How to avoid:** Use `MagicMock` as the return type where the mock IS the return value (this is the actual type). The CONTEXT.md says "use precise types where possible" but a MagicMock IS a MagicMock -- annotate it as such. Fixtures that return concrete stubs should use the stub type.

### Pitfall 3: Generator Return Types on Fixtures
**What goes wrong:** Fixtures using `yield` need `Generator[T, None, None]` or just the yield type.
**Why it happens:** pytest fixtures with cleanup use `yield` pattern.
**How to avoid:** For pytest fixtures, annotate with `Iterator[T]` when using yield, or the yielded type directly (pytest unwraps generators for fixtures).

### Pitfall 4: Plan 2 Size
**What goes wrong:** 676 violations across 14 files in a single plan could be overwhelming.
**Why it happens:** CONTEXT.md groups annotations + docstrings + lazy import refactoring in Plan 2.
**How to avoid:** The plan should be structured with clear per-file tasks. Most changes are mechanical (add `-> None`, add parameter types). The planner should acknowledge the volume but note the mechanical nature.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Pytest fixture type hints | Custom type aliases | `pytest.MonkeyPatch`, `pytest.CaptureFixture[str]`, `pathlib.Path` | Standard types, well-known |
| Mock type annotations | `Any` or `object` | `MagicMock` or `unittest.mock.MagicMock` | Accurate representation |
| SQLite cleanup | Manual try/finally | pytest fixture with yield + close() | Idiomatic, reliable |

## Pytest Strict Configuration Reference

The target configuration in `pyproject.toml`:

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

**What each setting does:**
- `strict_markers = true`: Unregistered markers cause an error (also enforced via `--strict-markers` in addopts)
- `strict_config = true`: Unknown ini keys in config cause an error
- `xfail_strict = true`: Tests marked `@pytest.mark.xfail` that unexpectedly pass will FAIL the suite
- `filterwarnings = ["error"]`: All warnings become errors -- no silent warning swallowing
- `addopts = ["-ra"]`: Show summary of all non-passing tests at end
- `--strict-markers` in addopts: Redundant with `strict_markers = true` but provides CLI-level enforcement

**Currently registered markers:** Only `browser` (Playwright tests). `usefixtures` is a pytest built-in and doesn't need registration.

## Plan Structure Guidance

### Plan 1: Type Checker Fixes (Small)
- Fix test_browser.py: rename `_device_id` -> `device_id`, `_settings` -> `settings`
- Fix test_job.py: add `assert fetched is not None` before attribute access
- Fix test_logging.py: add `import logging.handlers` at top
- Fix test_scanner.py: replace mock patterns with concrete stubs, eliminate 6 `type: ignore`
- Remove `exclude = ["tests/"]` from ty config
- Remove `project_excludes = ["tests/"]` from pyrefly config
- Verify: `uv run ty check` and `uv run pyrefly check src tests` pass clean

### Plan 2: Annotations + Docstrings + Lazy Imports (Large, mechanical)
- Add return type annotations to ~211 functions missing them
- Add parameter type annotations to ~389 parameters
- Add docstrings to ~38 functions missing them
- Move ~70+ lazy imports to top-level, keep ~5-10 with justified `# noqa: PLC0415`
- Remove ANN and PLC0415 from per-file-ignores
- Verify: `uv run ruff check .` passes clean

### Plan 3: Pytest Strict + Verification (Small-medium)
- Fix unclosed SQLite connections (ResourceWarning) -- likely a `job_store` fixture with cleanup
- Add strict pytest configuration to pyproject.toml
- Full verification: `uv run pytest`, `uv run ruff check .`, `uv run ty check`, `uv run pyrefly check src tests`, `uv run prek run`

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2+ |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest -x` |
| Full suite command | `uv run pytest` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| N/A | ty passes on tests/ | lint | `uv run ty check` | N/A (tool check) |
| N/A | pyrefly passes on tests/ | lint | `uv run pyrefly check src tests` | N/A (tool check) |
| N/A | ruff passes without test exemptions | lint | `uv run ruff check .` | N/A (tool check) |
| N/A | pytest strict mode works | unit | `uv run pytest` | Existing 226 tests |
| N/A | filterwarnings=error no failures | unit | `uv run pytest` | Existing 226 tests |

### Sampling Rate
- **Per task commit:** `uv run pytest -x && uv run ruff check . && uv run ty check && uv run pyrefly check src tests`
- **Per wave merge:** Full suite: `uv run prek run`
- **Phase gate:** All tools green before verification

### Wave 0 Gaps
None -- existing test infrastructure covers all phase requirements. No new test files needed; this phase modifies existing tests and config.

## Sources

### Primary (HIGH confidence)
- Direct analysis of pyproject.toml, all 14 test files + conftest.py
- `uv run ruff check --statistics` with exemptions removed: 676 violations counted
- `uv run ty check tests/`: 2 errors confirmed
- `uv run pyrefly check tests/`: 4 errors + 3 warnings confirmed
- `uv run pytest -W error`: ResourceWarning in 5+ test files confirmed
- AST analysis of all test files for annotation/docstring gaps

### Secondary (MEDIUM confidence)
- pytest strict mode documentation (stable, well-documented features)
- Python typing conventions for test code

## Metadata

**Confidence breakdown:**
- Current violations inventory: HIGH - direct tool output analysis
- Fix strategies: HIGH - CONTEXT.md prescribes exact fixes
- Pytest strict pitfalls: HIGH - confirmed via `pytest -W error` run
- Lazy import evaluation: MEDIUM - case-by-case judgment needed at implementation time

**Research date:** 2026-03-21
**Valid until:** 2026-04-21 (stable domain, no fast-moving dependencies)
