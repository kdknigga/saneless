# Phase 10: Automatic Scanner Profile Creation - Research

**Researched:** 2026-03-21
**Domain:** TOML config writing, scanner capability introspection, CLI command patterns
**Confidence:** HIGH

## Summary

This phase adds automatic generation of scan profiles from scanner capabilities, eliminating manual TOML profile configuration for new users. The implementation touches three areas: (1) a new `auto_profiles` module that maps `DeviceCapabilities` to `ProfileConfig` instances, (2) TOML config file writing to persist generated profiles, and (3) integration into both the CLI (`saneless auto-profiles` command) and the scan pipeline (lazy trigger on first scan).

The existing codebase provides strong foundations: `ScannerBackend.get_capabilities()` already returns parsed sources/resolutions/modes, `ProfileConfig` already models all profile fields with sensible defaults, and the CLI uses Click with well-established patterns. The main new capability needed is TOML file writing, which requires adding a dependency.

**Primary recommendation:** Use `tomlkit` for TOML writing (preserves comments and formatting in existing config files). Implement profile generation as a standalone module with pure functions, wire it into a new CLI command and a lazy trigger in the pipeline/worker.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Auto-generation triggers lazily on first scan when only the bare (uncustomized) `default` profile exists
- If scanner is unreachable at trigger time, fall back to bare `default` profile and retry next time
- Also available as explicit CLI command: `saneless auto-profiles`
- CLI command requires `--force` flag to overwrite existing profiles
- Generate one profile per available source (e.g., `flatbed-scan`, `adf-simplex`, `adf-duplex`)
- Default resolution: 300 dpi (or closest available if scanner doesn't support 300)
- Default mode: Color (or closest available)
- Naming: descriptive lowercase slugs like `flatbed-scan`, `adf-simplex`, `adf-duplex`
- The `default` profile is also updated to point to Flatbed at 300dpi/Color
- Lazy trigger backs off once user has customized profiles (any non-bare-default profile exists)
- `auto_generated = true` marker field on each auto-generated profile
- CLI `saneless auto-profiles` refuses to overwrite without `--force`
- If scanner capabilities don't match existing profiles (scanner swap), log a warning but don't auto-change
- Auto-generated profiles are written to the TOML config file (permanent, editable by user)
- Both lazy trigger and CLI command print to stdout

### Claude's Discretion
- Exact source-name-to-slug mapping logic
- How to detect "bare default" vs customized (compare against ProfileConfig defaults or check auto_generated markers)
- TOML serialization library choice (tomli-w, tomlkit, etc.)
- Warning message format for scanner capability mismatch
- Whether `auto_generated` field is preserved or stripped when user edits a profile

### Deferred Ideas (OUT OF SCOPE)
None
</user_constraints>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| tomlkit | 0.14.0 | TOML read/write with comment preservation | Preserves user comments/formatting in existing config files; used by Poetry for same use case |
| click | 8.3+ | CLI command framework | Already in use; `auto-profiles` command follows existing patterns |
| pydantic | 2.x | ProfileConfig model | Already in use; `auto_generated` field added to existing model |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| logging | stdlib | Warning/info output for mismatch detection | Already wired throughout codebase |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| tomlkit | tomli-w | Simpler API but destroys comments/formatting when rewriting config file |

**Installation:**
```bash
uv add tomlkit
```

**Why tomlkit over tomli-w:** The user's config file (`saneless.toml`) contains comments (e.g., `# SANE network host, leave empty for local`). When auto-profiles writes new `[profiles.X]` sections, `tomli-w` would require reading the entire file, parsing to dict, modifying, and dumping -- destroying all comments. `tomlkit` preserves the document structure, allowing surgical additions to the TOML while keeping user comments intact. This is the same reason Poetry uses `tomlkit`.

## Architecture Patterns

### Recommended Project Structure
```
src/saneless/
├── auto_profiles.py     # Pure functions: capabilities -> profiles, slug mapping, TOML writing
├── config.py            # ProfileConfig gets auto_generated field
├── cli.py               # New auto-profiles command
├── worker.py            # Lazy trigger hook before first scan
└── pipeline.py          # No changes needed
```

### Pattern 1: Pure Profile Generation (no I/O)
**What:** A pure function that takes `DeviceCapabilities` and returns a dict of profile name -> `ProfileConfig`.
**When to use:** Core logic, easily testable without mocking.
**Example:**
```python
def generate_profiles(
    capabilities: DeviceCapabilities,
) -> dict[str, ProfileConfig]:
    """Generate scan profiles from scanner capabilities."""
    profiles: dict[str, ProfileConfig] = {}
    resolution = pick_closest_resolution(capabilities.resolutions, target=300)
    mode = pick_preferred_mode(capabilities.modes, preferred="Color")

    for source in capabilities.sources:
        slug = source_to_slug(source)
        profiles[slug] = ProfileConfig(
            source=source,
            resolution=resolution,
            mode=mode,
            auto_generated=True,
        )

    # Update default to flatbed if available
    if any("flatbed" in s.lower() for s in capabilities.sources):
        flatbed_source = next(s for s in capabilities.sources if "flatbed" in s.lower())
        profiles["default"] = ProfileConfig(
            source=flatbed_source,
            resolution=resolution,
            mode=mode,
            auto_generated=True,
        )

    return profiles
```

### Pattern 2: TOML Config Update (comment-preserving)
**What:** Read existing TOML with tomlkit, add/update profile sections, write back.
**When to use:** Persisting auto-generated profiles to the config file.
**Example:**
```python
import tomlkit

def write_profiles_to_config(
    config_path: Path,
    profiles: dict[str, ProfileConfig],
) -> None:
    """Write generated profiles to TOML config file, preserving existing content."""
    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text())
    else:
        doc = tomlkit.document()

    if "profiles" not in doc:
        doc.add("profiles", tomlkit.table(is_super_table=True))

    for name, profile in profiles.items():
        profile_table = tomlkit.table()
        profile_table.add("source", profile.source)
        profile_table.add("resolution", profile.resolution)
        profile_table.add("mode", profile.mode)
        profile_table.add("auto_generated", True)
        doc["profiles"][name] = profile_table

    config_path.write_text(tomlkit.dumps(doc))
```

### Pattern 3: Lazy Trigger in Worker
**What:** Check if profiles need generation before first scan job.
**When to use:** The worker's `_process_job` method, before `run_pipeline`.
**Example:**
```python
def _should_auto_generate(settings: Settings) -> bool:
    """Check if auto-profile generation should trigger."""
    profiles = settings.profiles
    # Only trigger when there's exactly one profile ("default") that is bare/uncustomized
    if len(profiles) > 1:
        return False
    if "default" not in profiles:
        return False
    default = profiles["default"]
    # Compare against fresh ProfileConfig defaults
    bare = ProfileConfig()
    return (
        default.source == bare.source
        and default.resolution == bare.resolution
        and default.mode == bare.mode
    )
```

### Pattern 4: Source Name to Slug Mapping
**What:** Convert SANE source strings to descriptive profile slugs.
**When to use:** Generating profile names from scanner-reported source names.

SANE source names vary by backend. Common patterns observed:
- `Flatbed` -> `flatbed-scan`
- `ADF` / `Automatic Document Feeder` / `ADF Front` / `Adf-front` -> `adf-simplex`
- `ADF Duplex` / `Adf-duplex` -> `adf-duplex`
- `ADF Back` / `Adf-back` -> (skip, not useful as a standalone scan mode)

The slug mapping should be case-insensitive and use keyword matching (contains "flatbed", contains "duplex", contains "adf").

### Anti-Patterns to Avoid
- **Modifying Settings in memory and hoping it persists:** Settings are loaded once at startup. Auto-generated profiles must be written to the TOML file AND the in-memory Settings must be updated.
- **Re-reading TOML with tomllib/tomli after writing with tomlkit:** Stick to tomlkit for both read and write in the auto_profiles module to avoid format mismatches.
- **Triggering auto-generation on every scan:** Must be a one-shot check. After generating, the profiles dict will have more than just bare default, so the check naturally fails on subsequent scans.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| TOML writing with comment preservation | Custom string manipulation | tomlkit | Edge cases with multiline strings, inline tables, escaping |
| Resolution matching | Linear search | `min(resolutions, key=lambda r: abs(r - 300))` | Standard approach, one-liner |
| Config file discovery | Custom search | `load_settings` already has search path logic | Reuse existing `search_paths` from `config.py` |

## Common Pitfalls

### Pitfall 1: Config File Path Not Known at Runtime
**What goes wrong:** The auto_profiles module needs to know WHERE to write the TOML file, but `load_settings()` doesn't expose which file was loaded (or if any file was found).
**Why it happens:** `_build_settings` doesn't store the resolved config path.
**How to avoid:** Either (a) modify `load_settings` to return the resolved path alongside Settings, or (b) have the auto-profiles module replicate the search path logic, or (c) pass the config path explicitly from CLI context (the `--config` flag value).
**Warning signs:** Writing to wrong file, or creating a new file when one exists elsewhere.

### Pitfall 2: SANE Source Names Are Backend-Specific
**What goes wrong:** Hardcoding exact source name matches fails across scanner models.
**Why it happens:** Different SANE backends report sources differently: "Flatbed" vs "flatbed", "ADF" vs "Automatic Document Feeder", "ADF Duplex" vs "Adf-duplex".
**How to avoid:** Use case-insensitive keyword matching: `"flatbed" in source.lower()`, `"duplex" in source.lower()`, `"adf" in source.lower()`.
**Warning signs:** Missing profiles for some scanner models.

### Pitfall 3: Scanner Unreachable During Lazy Trigger
**What goes wrong:** First scan blocks or errors because scanner is being queried for capabilities.
**Why it happens:** `get_capabilities()` opens a device connection.
**How to avoid:** Wrap in try/except, log warning, continue with bare default. The CONTEXT.md decision already specifies this behavior.
**Warning signs:** First scan taking unusually long or failing with connection errors.

### Pitfall 4: pydantic-settings Extra Fields Rejection
**What goes wrong:** Adding `auto_generated` field to `ProfileConfig` may conflict with existing TOML parsing if older config files don't have it.
**Why it happens:** pydantic can reject unknown fields depending on model_config.
**How to avoid:** `auto_generated` has a default value (`False`), so existing configs without it will parse correctly. The field is a simple bool with a default.
**Warning signs:** ValidationError when loading old config files.

### Pitfall 5: Concurrent Access to Config File
**What goes wrong:** Lazy trigger writes config while another process might be reading.
**Why it happens:** Worker thread runs in background, config file is shared.
**How to avoid:** The lazy trigger runs once before the first scan in the worker thread. Since there's only one worker thread and config is loaded at startup, there's minimal risk. Use atomic write (write to temp file, rename) as a safety measure.
**Warning signs:** Corrupted TOML file.

### Pitfall 6: tomlkit Type Annotations
**What goes wrong:** ty or pyrefly may flag tomlkit API calls.
**Why it happens:** tomlkit's type stubs may be incomplete for Python 3.14 type checkers.
**How to avoid:** Use TYPE_CHECKING guards if needed, test with both type checkers early.
**Warning signs:** Type checker errors on tomlkit calls.

## Code Examples

### Closest Resolution Selection
```python
def pick_closest_resolution(resolutions: list[int], target: int = 300) -> int:
    """Pick the resolution closest to target from available options."""
    if not resolutions:
        return target
    return min(resolutions, key=lambda r: abs(r - target))
```

### Preferred Mode Selection
```python
def pick_preferred_mode(modes: list[str], preferred: str = "Color") -> str:
    """Pick preferred mode, falling back to first available."""
    # Case-insensitive match
    for mode in modes:
        if mode.lower() == preferred.lower():
            return mode
    return modes[0] if modes else preferred
```

### Source to Slug Mapping
```python
def source_to_slug(source: str) -> str:
    """Convert a SANE source name to a profile slug."""
    lower = source.lower()
    if "flatbed" in lower:
        return "flatbed-scan"
    if "duplex" in lower:
        return "adf-duplex"
    if "adf" in lower or "document feeder" in lower or "feeder" in lower:
        return "adf-simplex"
    # Fallback: slugify the source name
    return lower.replace(" ", "-").replace("_", "-")
```

### Bare Default Detection
```python
def is_bare_default(settings: Settings) -> bool:
    """Check if settings have only the uncustomized default profile."""
    if len(settings.profiles) != 1:
        return False
    if "default" not in settings.profiles:
        return False
    default = settings.profiles["default"]
    bare = ProfileConfig()
    return (
        default.source == bare.source
        and default.resolution == bare.resolution
        and default.mode == bare.mode
        and not getattr(default, "auto_generated", False)
    )
```

### CLI Command Pattern (following existing conventions)
```python
@cli.command(name="auto-profiles")
@click.option("--force", is_flag=True, help="Overwrite existing profiles.")
@click.pass_context
def auto_profiles(ctx: click.Context, *, force: bool) -> None:
    """Generate scan profiles from scanner capabilities."""
    settings = ctx.obj["settings"]
    # ... implementation
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Manual TOML profile editing | Auto-generation from capabilities | This phase | Users don't need to know SANE source names |
| tomli (read-only, stdlib tomllib) | tomlkit (read-write, comment-preserving) | Mature library, stable API | Config file writing without destroying user comments |

## Open Questions

1. **Config file path propagation**
   - What we know: `load_settings()` searches `./saneless.toml`, `~/.config/saneless/config.toml`, `/etc/saneless/config.toml` but does not return which path was found.
   - What's unclear: Best way to expose the resolved path for writing.
   - Recommendation: Add a `config_path` attribute to Settings or return a tuple from `load_settings`. Alternatively, for the CLI command, use the `--config` flag value. For lazy trigger, resolve it separately using the same search logic.

2. **In-memory Settings refresh after write**
   - What we know: Settings are loaded once at startup and stored in `app.state.settings`.
   - What's unclear: Whether to reload Settings from TOML after writing or just update the in-memory dict.
   - Recommendation: Update the in-memory `settings.profiles` dict directly after writing. No need to reload from disk since the auto-profiles module knows exactly what was written.

3. **Config file creation when none exists**
   - What we know: If no config file is found, Settings uses defaults + env vars only.
   - What's unclear: Where to create a new config file if one doesn't exist.
   - Recommendation: Default to `./saneless.toml` (first in search path) for the lazy trigger, or require `--config` for the CLI command. Log a clear message about where the file was created.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/test_auto_profiles.py -x` |
| Full suite command | `uv run pytest` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| AP-01 | Generate profiles from capabilities | unit | `uv run pytest tests/test_auto_profiles.py::TestGenerateProfiles -x` | No - Wave 0 |
| AP-02 | Source-to-slug mapping | unit | `uv run pytest tests/test_auto_profiles.py::TestSourceToSlug -x` | No - Wave 0 |
| AP-03 | Resolution/mode selection | unit | `uv run pytest tests/test_auto_profiles.py::TestResolutionMode -x` | No - Wave 0 |
| AP-04 | Bare default detection | unit | `uv run pytest tests/test_auto_profiles.py::TestBareDefault -x` | No - Wave 0 |
| AP-05 | TOML writing preserves comments | unit | `uv run pytest tests/test_auto_profiles.py::TestTomlWriting -x` | No - Wave 0 |
| AP-06 | CLI auto-profiles command | unit | `uv run pytest tests/test_cli.py::TestAutoProfiles -x` | No - Wave 0 |
| AP-07 | Lazy trigger on first scan | unit | `uv run pytest tests/test_worker.py::TestLazyAutoProfiles -x` | No - Wave 0 |
| AP-08 | Force flag behavior | unit | `uv run pytest tests/test_auto_profiles.py::TestForceOverwrite -x` | No - Wave 0 |
| AP-09 | Scanner unreachable fallback | unit | `uv run pytest tests/test_auto_profiles.py::TestScannerUnreachable -x` | No - Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_auto_profiles.py -x`
- **Per wave merge:** `uv run pytest`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_auto_profiles.py` -- covers AP-01 through AP-09
- [ ] tomlkit dependency: `uv add tomlkit`

## Sources

### Primary (HIGH confidence)
- Codebase inspection: `src/saneless/config.py`, `src/saneless/scanner/base.py`, `src/saneless/cli.py`, `src/saneless/worker.py`, `src/saneless/pipeline.py`
- `saneless.toml` -- existing config file format with comments
- `pyproject.toml` -- current dependencies and tool config

### Secondary (MEDIUM confidence)
- [tomlkit PyPI](https://pypi.org/project/tomlkit/) -- version 0.14.0, comment-preserving TOML library
- [tomli-w PyPI](https://pypi.org/project/tomli-w/) -- version 1.2.0, simple TOML writer (no comment preservation)
- [SANE source option names](https://manpages.ubuntu.com/manpages//trusty/man5/sane-fujitsu.5.html) -- Flatbed, ADF Front, ADF Back, ADF Duplex patterns
- [DEV Community TOML comparison](https://dev.to/pypyr/comparison-of-python-toml-parser-libraries-595e) -- tomlkit vs tomli-w tradeoffs

### Tertiary (LOW confidence)
- SANE source name variations across all backends -- based on a sample of backends (fujitsu, epjitsu, HP, Ricoh); other backends may use different naming

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - tomlkit is well-established, all other libs already in use
- Architecture: HIGH - all integration points clearly identified in existing codebase
- Pitfalls: HIGH - config path propagation is a real gap; SANE name variation confirmed by docs

**Research date:** 2026-03-21
**Valid until:** 2026-04-21 (stable domain, no fast-moving dependencies)
