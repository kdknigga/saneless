# Phase 27: Configuration Strictness - Pattern Map

**Mapped:** 2026-09-15
**Files analyzed:** 24 (8 source, 10 test, 2 config/example, docs set counted as 4 groups)
**Analogs found:** 23 / 24. The one partial match is `tests/test_deployment_config.py`: no test reads repo-root files yet, so the closest analog is a static-asset text test.

All line numbers were read in this session against HEAD `5d78599`.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `src/saneless/config.py` (M) | model + loader | transform / validation | itself: `_translate_legacy_manual_duplex` (123-146), `config_search_paths` (430-446), `warn_on_legacy_duplex_sources` (286-333), `_build_settings` (353-380) | exact (self) |
| `src/saneless/atomic_write.py` (NEW) | utility | file-I/O | `src/saneless/paperless.py` `_deliver_to_consume_dir` (331-383) | exact (same stage, fsync, replace, cleanup idiom) |
| `src/saneless/auto_profiles.py` (M) | service | file-I/O + transform | itself: `write_profiles_to_config` (459-553), `_is_auto_generated` (425-443); frozen result from `worker.py` `_OwedWrite` (95-117) | exact (self) |
| `src/saneless/logging_config.py` (M) | config utility | setup | itself (18-65) | exact (self) |
| `src/saneless/cli.py` (M) | controller | request-response | itself: group callback (162-198), `scan` (201-271), `serve` (433-465), `auto_profiles` (468-500) | exact (self) |
| `src/saneless/worker.py` (M) | service | event-driven (startup) | itself: `_profiles_after_persist` (120-156), `_persist_generated_profiles` (799-859), `get_profile` (675-690) | exact (self) |
| `src/saneless/web/app.py` (M) | factory | request-response | itself (60-64) | exact (self) |
| `src/saneless/web/routes.py` (M) | controller | request-response | itself `start_scan` (370-412) + `worker.get_profile` (worker.py 675-690) | exact (self) |
| `tests/conftest.py` (M) | test fixture | — | itself `clean_env` (130-135) | exact |
| `tests/test_config.py` (M) | test | unit | itself: `TestLoadedConfigPath` (145-243), `TestLegacyDuplexWarning` (564-673), `TestTomlStructureErrors` (302-336), `TestValidateSettingsDirs` (700-769) | exact |
| `tests/test_atomic_write.py` (NEW) | test | file-I/O | `tests/test_paperless.py` 148-161 + 713-775; `tests/test_worker.py` 3823-3862 | role-match |
| `tests/test_auto_profiles.py` (M) | test | file-I/O | itself: `TestOrphanedProfilePrune` (1010-1103), `TestTomlWriting` (1176-1307), `TestMalformedProfilesSection` (914-950) | exact |
| `tests/test_logging.py` (M) | test | unit | itself `_cleanup_handlers` (20-26) | exact |
| `tests/test_cli.py` (M) | test | CliRunner | itself: `_patch_cli` (68-179), `TestCliFlags` (841-901), `TestLegacyDuplexWarningReachesLogFile` (904-951), `TestAutoProfiles` (1259-1408), `TestServeCommand` (1163-1256) | exact |
| `tests/test_worker.py` (M) | test | threaded unit | itself `TestStartupProfileGeneration` (3663-3862), `_worker_records` (1983-1993) | exact |
| `tests/test_web.py` (M) | test | TestClient | itself `test_settings` fixture (91-109), `test_scan_form_submit` (264-270) | exact |
| `tests/test_outcomes_e2e.py` (M, lines 663, 734) | test | e2e | `src/saneless/web/app.py` 60-64 (the unwrap site) | exact |
| `tests/test_deployment_config.py` (NEW) | test | static text | `tests/test_vendor_assets.py` (16-30, 129-171) | partial (reads package files, not repo root) |
| `docker-compose.yml` (M) | config | — | itself (1-32) | exact |
| `saneless.toml.example` (M) | config | — | itself (1-21) | exact |
| `docs/how-to/deploy-docker-compose.md`, `docs/reference/docker.md`, `docs/getting-started/quick-start.md` (M) | docs | — | `deploy-docker-compose.md` 27-28, 51-53, 67-72 | exact |
| `docs/reference/configuration.md`, `docs/reference/environment-variables.md` (M) | docs | — | the files themselves (RESEARCH "Docs and Compose Change List") | exact |
| `docs/how-to/configure-scan-profiles.md`, `docs/reference/cli-commands.md`, `docs/how-to/cli-scripting.md` (M) | docs | — | the files themselves | exact |
| `docs/explanation/empty-page-detection.md`, `docs/explanation/architecture.md` (M) | docs | — | the files themselves | exact |

`src/saneless/exceptions.py` does not change. `ConfigError` (line 23) is reused as is.

---

## Shared Patterns (apply to every source file in this phase)

These are lint and type rules enforced in `pyproject.toml` `[tool.ruff.lint] select` (it includes `EM`, `G`, `PTH`, `FBT`, `DTZ`, `D`, `ANN`, `TCH`, `PL`). CLAUDE.md forbids `# noqa` and `# type: ignore`. RESEARCH's code sketches break several of these rules, so the planner must apply the house form below.

### S-1. Exception messages go through a `msg` variable (ruff EM101/EM102)
**Source:** `src/saneless/config.py` 280-282, 401-402. `auto_profiles.py` 510-511.
```python
if "default" not in v:
    msg = "A 'default' profile must be defined in config"
    raise ValueError(msg)
```
```python
msg = f"[profiles] in {config_path} is not a table; refusing to overwrite it"
raise ConfigError(msg)
```
**Apply to:** every new `raise ConfigError(...)` / `PermissionError(...)`. RESEARCH Pattern 4 and 6 write `raise ConfigError(f"...")` inline, which fails EM102. Assign to `msg` first. For the renderer, use `msg = "\n".join(lines)` then `raise ConfigError(msg) from None`.

### S-2. `os.replace` is forbidden; use `Path.replace` (ruff PTH105, no per-line suppression)
**Source:** `src/saneless/paperless.py` 352-357 (docstring) and 380.
```python
#   * The rename is ``Path.replace``, which delegates to ``os.replace``
#     and so is the atomic, unconditionally-overwriting one.
#     ``os.replace`` is not called directly only because ruff's PTH105
#     forbids it and this project does not permit per-line suppressions.
staged.replace(dest)
```
**Apply to:** `atomic_write.py`. Use `tmp.replace(target)`, not `os.replace(tmp, target)`. The same applies to `os.stat` (PTH116), where you use `target.stat()`, and `os.chmod` (PTH101), where you use `os.fchmod` on the fd. `os.fchmod`, `os.fchown`, `os.fsync`, `os.open`, and `os.access` have no PTH rule. **Test consequence:** the EBUSY test must patch `pathlib.Path.replace` (`monkeypatch.setattr(Path, "replace", busy)`), not `saneless.atomic_write.os.replace` as RESEARCH's example does.

### S-3. Logging uses %-args, never f-strings (ruff G004), and names user values with `%r`
**Source:** `src/saneless/config.py` 312-320. `worker.py` 830-837.
```python
logger.warning(
    "Auto-profiles: could not write %s (%s: %s); the generated "
    "profiles are used for this run only and will not survive a "
    "restart",
    config_path,
    type(exc).__name__,
    exc,
)
```
**Apply to:** CFG-11 `log_config_sources`, the worker's grouped result line, and the D-07 symlink line. Error-renderer strings (not logs) may use f-strings, but every user-controlled name uses `!r` (RESEARCH Pattern 2 rule 5).

### S-4. Docstring shape (ruff D, D212 ignored, so the summary goes on line 2)
**Source:** `src/saneless/config.py` 61-79.
```python
def _is_legacy_manual_duplex_source(source: str) -> bool:
    """
    Recognise the deprecated ``source = "Manual Duplex"`` config form (DPLX-02).

    This exists ONLY to detect a legacy profile at config load so it can be
    translated to ``duplex = "manual"`` ...

    Args:
        source: The profile's configured source string.

    Returns:
        True if the source contains both "manual" and "duplex", ignoring case.

    """
```
Google sections (`Args:`, `Returns:`, `Raises:`, `Attributes:`) are followed by a blank line before the closing `"""`. Cite decision IDs in the prose (`(D-16)`, `(WR-05)`), as the existing code does. One-line docstrings are fine for trivial helpers (`cli.py` 73-74, 155-156).

### S-5. Explanatory comments carry the "why" and the decision ID
**Source:** `src/saneless/auto_profiles.py` 446-456 and 502-507. `cli.py` 68-71. Comment density is high: every non-obvious guard gets a comment that says what broke without it. Match this in `atomic_write.py` (chown before chmod, the `os.access` pre-check, best-effort directory fsync) and in the renderer (why `from None`).

### S-6. Keyword-only booleans (ruff FBT001/FBT002)
**Source:** `auto_profiles.py` 459-464, `logging_config.py` 18-25, `cli.py` 176.
```python
def write_profiles_to_config(
    config_path: Path,
    profiles: dict[str, ProfileConfig],
    *,
    force: bool = False,
) -> list[str]:
```
**Apply to:** any new bool parameter (for example `_nearest_existing_ancestor`, or `resolve_job_title(..., *, now: datetime)`).

### S-7. Timezone-aware datetimes (ruff DTZ)
**Source:** `src/saneless/web/routes.py` 7 and 409: `from datetime import UTC, datetime` ... `datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M')`. **Apply to:** `resolve_job_title` and both callers.

### S-8. Imports: `from __future__ import annotations`, TYPE_CHECKING block for type-only names (ruff TCH)
**Source:** `src/saneless/config.py` 9-36, `auto_profiles.py` 9-25, `worker.py` 9-51. Package-relative imports are used in `cli.py` / `worker.py` (`from .config import ...`). Absolute `saneless.` imports are used in `config.py`, `auto_profiles.py`, and `web/`. Follow the file you are editing. A new module that imports `Path` only for annotations must put it under `if TYPE_CHECKING:` (as `auto_profiles.py` 22-25 does). Use a runtime import when `Path(...)` is called.

### S-9. `__all__` maintained at the top of each module
**Source:** `config.py` 38-49 (alphabetical, constants first, then classes, then functions). **Apply to:** new exports `resolve_job_title`, `xdg_config_home`, `xdg_state_home`, `log_config_sources`, `env_sourced_keys`, `LogLevel` in `config.py`. Add `ProfileWriteResult` in `auto_profiles.py`. Add `replace_file_atomically` in the new module.

### S-10. Post-logging, load-time messages are plain functions the CLI calls, never validators
**Source:** `config.py` 286-307 (`warn_on_legacy_duplex_sources` docstring explains WR-05). `cli.py` 194-195.
```python
    # WR-05: emitted only now, once the log file handler exists to receive it.
    warn_on_legacy_duplex_sources(settings)
```
**Apply to:** the CFG-11 `log_config_sources(settings)` function. The D-13 unknown-env scan also runs in `load_settings`/`_build_settings`, not in a `Settings` validator. Tests build `Settings(...)` directly (conftest 138-154).

---

## Pattern Assignments

### `src/saneless/config.py` (model + loader, transform/validation)

**Analog:** itself.

**Imports block to extend** (lines 9-36). Add `difflib`, `datetime`/`UTC`, `SecretStr`, `EnvSettingsSource`, `pydantic_settings.exceptions.SettingsError`, and `TITLE_MAX_LENGTH` from `saneless.vocabulary` (research verified there is no cycle).
```python
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from saneless.exceptions import ConfigError
```

**Model config pattern.** Only `ProfileConfig` has one today (line 100). Add the same line shape to `ScannerConfig` (82-86), `PaperlessConfig` (89-94), and `OutputConfig` (149-177). Keep `populate_by_name=True` on `ProfileConfig`.
```python
class ProfileConfig(BaseModel):
    """Scan profile configuration."""

    model_config = ConfigDict(populate_by_name=True)
```
Add `extra="forbid"` explicitly to the `Settings` `SettingsConfigDict` at 215-218.

**Validator pattern** to copy for the `log_level` before-validator and the path `expanduser` after-validator (123-146). Keep the decorator order `@field_validator(...)` then `@classmethod`, as `validate_default_profile` does at 273-283:
```python
    @field_validator("profiles")
    @classmethod
    def validate_default_profile(
        cls,
        v: dict[str, ProfileConfig],
    ) -> dict[str, ProfileConfig]:
        """Ensure a default profile is always defined."""
        if "default" not in v:
            msg = "A 'default' profile must be defined in config"
            raise ValueError(msg)
        return v
```

**Bounded field pattern with explanatory comment** (166-174). Copy it for `default_title: str = Field(default="", alias="title", max_length=TITLE_MAX_LENGTH)` at line 117:
```python
    flip_timeout_seconds: int = Field(default=600, ge=1, le=86_400)
```

**Call-time default pattern** (430-446). XDG helpers and the `Field(default_factory=...)` on `data_dir`/`log_file` follow this "read when called, not at import" rule. The comment at 153-156 explicitly anticipates this edit:
```python
    # Durable state: the job database and preserved scans. Deliberately NOT
    # under tmp_dir, which is disposable scratch space. The hardcoded
    # Path.home() form matches log_file below so Phase 27's XDG expansion
    # changes both defaults in a single edit; no env var is consulted here.
    data_dir: str = str(Path.home() / ".local" / "state" / "saneless")
    log_file: str = str(Path.home() / ".local" / "state" / "saneless" / "saneless.log")
```
Also change `output: OutputConfig = OutputConfig()` (line 222) to `Field(default_factory=OutputConfig)` (RESEARCH Pitfall 3).

**Error-conversion pattern to replace** (353-380). Keep the function name and the `_SettingsFactory` cast (339-359). Replace the body's `extra_forbidden`-only handling with the full renderer. Change `from exc` to `from None` (Pitfall 1). Remove `_VALID_SECTIONS` (336).
```python
def _build_settings(
    toml_file: Path | None = None,
) -> Settings:
    """Build Settings, converting extra-field errors to user-friendly messages."""
    try:
        if toml_file is not None:
            return cast("_SettingsFactory", Settings)(_toml_file=toml_file)
        return Settings()
    except ValidationError as exc:
        extra_fields: list[str] = []
        for err in exc.errors():
            if err["type"] == "extra_forbidden":
                loc = err.get("loc", ())
```

**Load path to change** (467-477). Add expanduser plus `is_file()` for CFG-02, and `is_file()` for discovery. The comment at 474-475 names this phase:
```python
    path = (
        Path(config_path)
        if config_path
        else next((p for p in config_search_paths() if p.exists()), None)
    )
    # With no path, only defaults + env vars are used.
    settings = _build_settings(toml_file=path)
    # An explicit path is recorded even if missing; CFG-02 (Phase 27) owns
    # making that an error.
    settings._config_path = path
    return settings
```

**Triplicated writability checks to fold into one ancestor-walk helper** (399-427):
```python
    tmp = Path(settings.output.tmp_dir)
    if tmp.exists() and not os.access(tmp, os.W_OK):
        msg = f"tmp_dir is not writable: {tmp}"
        raise ConfigError(msg)
    if not tmp.exists():
        parent = tmp.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            msg = f"tmp_dir parent is not writable: {parent}"
            raise ConfigError(msg)
```
Keep the `"not writable"` substring. Tests at test_config.py 723, 748, and 766 match on it.

**`resolve_job_title`:** place it next to `ProfileConfig`. It is a pure function in the same style as `_is_legacy_manual_duplex_source` (61-79): Google docstring, no I/O.

---

### `src/saneless/atomic_write.py` (NEW utility, file-I/O)

**Analog:** `src/saneless/paperless.py` `_deliver_to_consume_dir`, lines 331-383.

**Module header shape** (paperless.py 1-26; exceptions.py 1-6):
```python
"""
Paperless-ngx REST API client with retry, polling, and connection test.

Uploads PDFs with metadata ...
"""

from __future__ import annotations

import logging
import os
...
from .exceptions import PaperlessError, PaperlessTimeoutError

__all__ = ["PaperlessClient", "UploadResult"]

logger = logging.getLogger(__name__)
```

**Core stage, fsync, replace, cleanup pattern** (paperless.py 374-383):
```python
        staged = dest_dir / f".{pdf_path.name}.part"
        try:
            with staged.open("wb") as staged_file, pdf_path.open("rb") as source:
                shutil.copyfileobj(source, staged_file)
                staged_file.flush()
                os.fsync(staged_file.fileno())
            staged.replace(dest)
        except OSError:
            staged.unlink(missing_ok=True)
            raise
```
Deltas the new helper needs (D-05..D-08, RESEARCH Pattern 4):
- Use `tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")` instead of a fixed `.part` name. Keep the dotfile prefix convention.
- Use `os.fdopen(fd, "wb")`, then `fchown` inside `contextlib.suppress(PermissionError)`, then `fchmod(stat.S_IMODE(...))`, then write bytes, `flush`, `fsync`.
- Replace with `tmp.replace(target)` (S-2). Map `errno.EBUSY` to `ConfigError` using `msg = ...` (S-1) and `raise ConfigError(msg) from exc`. This is not a secret path, so chaining is fine.
- Clean up with a `replaced` flag in `finally` (covers `KeyboardInterrupt`). The paperless analog catches only `OSError`, but this helper also raises `ConfigError`.
- Fsync the directory as a best effort after replace (`contextlib.suppress(OSError)`). The `contextlib.suppress` idiom already exists in `scanner/sane_backend.py` 1082.
- Write the docstring the way paperless.py 333-373 does: bullet the load-bearing details, and add `Raises:` for `ConfigError` and `OSError`.

---

### `src/saneless/auto_profiles.py` (service, file-I/O + transform)

**Analog:** itself, `write_profiles_to_config` 459-553. For the result type, copy `worker.py` `_OwedWrite` 95-117.

**Frozen result dataclass pattern** (worker.py 95-117). Use `@dataclass(frozen=True, slots=True)` with an `Attributes:` docstring section. Tuple fields default to `()`. Note that `slots=True` together with a `@property` (`persisted`) is fine.
```python
@dataclass(frozen=True, slots=True)
class _OwedWrite:
    """
    One terminal job-row write the worker still owes, exactly as it was meant.

    ... Frozen, so the flush's delete-if-unchanged check compares values.

    Attributes:
        state: The terminal state to record.
        result: What the scan produced, for a DONE or FALLBACK write.

    """

    state: JobState
    result: JobResult | None = None
```
`auto_profiles.py` imports nothing from `dataclasses` today. Add `from dataclasses import dataclass`. Move `Path` out of the TYPE_CHECKING block (22-23) only if it becomes a runtime field annotation. With `from __future__ import annotations` it can stay type-only.

**Read and parse to change** (494-497). Use `read_bytes().decode("utf-8")` to keep CRLF (Pitfall 4):
```python
    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text())
    else:
        doc = tomlkit.document()
```

**Container guard to keep** (508-512):
```python
    section = doc["profiles"]
    if not isinstance(section, Mapping):
        msg = f"[profiles] in {config_path} is not a table; refusing to overwrite it"
        raise ConfigError(msg)
    profiles_section = cast("dict[str, object]", section)
```

**Orphan prune to keep, and to collect into `removed`** (514-527):
```python
    orphans = [
        name
        for name, table in profiles_section.items()
        if name not in profiles
        and name not in _UNPRUNABLE
        and _is_auto_generated(table)
    ]
    for name in orphans:
        logger.info(
            "Removing auto-generated profile %r: the scanner's sources no "
            "longer produce that name.",
            name,
        )
        del profiles_section[name]
```

**Write loop to replace with a merge** (529-553). Keep the key order and the non-default-only comments (537-546) in a `_generated_values` helper. The final `write_text` becomes `tomllib.loads` guard, then `replace_file_atomically`:
```python
    written: list[str] = []
    for name, profile in profiles.items():
        if name in profiles_section and not force:
            continue
        profile_table = tomlkit.table()
        profile_table.add("source", profile.source)
        profile_table.add("resolution", profile.resolution)
        profile_table.add("mode", profile.mode)
        if profile.auto_source_mode != "flatbed":
            profile_table.add("auto_source_mode", profile.auto_source_mode)
        ...
        if profile.duplex != "none":
            profile_table.add("duplex", profile.duplex)
        auto_generated_flag = True
        profile_table.add("auto_generated", auto_generated_flag)
        profiles_section[name] = profile_table
        written.append(name)

    config_path.write_text(tomlkit.dumps(doc))
    return written
```
Notes:
- `auto_generated_flag = True` then `.add(..., auto_generated_flag)` avoids ruff FBT003 (boolean positional value in a call). Keep that trick for new `table[key] = True` assignments, or use a subscript assignment, which FBT does not flag.
- The comment at 446-456 claims "`cli()` loads settings before dispatching to any subcommand". Update it once CFG-10 makes loading lazy.
- The docstring at 471-478 says "pre-empting the general merge semantics owned by a later phase". That later phase is this one, so rewrite it.

---

### `src/saneless/logging_config.py` (config utility)

**Analog:** itself (18-65). Only lines 44-45 and 62-65 change:
```python
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))
    ...
    if verbose:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        root_logger.addHandler(stderr_handler)
```
Replace `getattr` with `logging.getLevelNamesMapping()[log_level]`. Add `logging.getLogger("saneless").setLevel(logging.DEBUG if verbose else logging.NOTSET)` (Pattern 8). Keep `verbose` keyword-only: `test_cli.py` 848-850 reads `kwargs.get("verbose")`. Update the module docstring (1-6) and the `Args:` text for `verbose`.

---

### `src/saneless/cli.py` (controller, request-response)

**Analog:** itself.

**Group callback to slim down** (162-198). The try/except, `configure_logging` call, and WR-05 call move into `_load_cli_settings(ctx)`:
```python
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, *, verbose: bool) -> None:
    """Saneless -- SANE scanner to paperless-ngx bridge."""
    ctx.ensure_object(dict)

    try:
        settings = load_settings(config_path)
        validate_settings_dirs(settings)
    except Exception as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(2)

    configure_logging(
        settings.output.log_file,
        settings.output.log_level,
        settings.output.log_max_bytes,
        settings.output.log_backup_count,
        verbose=verbose,
    )
    # WR-05: emitted only now, once the log file handler exists to receive it.
    warn_on_legacy_duplex_sources(settings)

    ctx.obj["settings"] = settings
    ctx.obj["verbose"] = verbose
```
Every command body opens with `settings = ctx.obj["settings"]` (215, 326 as `_settings`, 381, 439, 473). Each becomes `settings = _load_cli_settings(ctx)`. The helper must call `load_settings` and `configure_logging` **by module-global name**, because every CLI test monkeypatches `saneless.cli.load_settings` / `saneless.cli.configure_logging` (test_cli.py 92-96, 241-242, 853-856, 884-885).

**Private helper precedent:** `_stdin_is_interactive` (68-74) and `_echo_capabilities` (274-308) are module-level private helpers with a preceding "why" comment or a full docstring. `_echo_capabilities` 280-282 records the PLR0912 branch-limit rationale. Split `auto_profiles` output into an `_echo_write_result(result)` helper the same way if branches grow.

**Option definition style** (207-211), the `--title` target:
```python
@click.option(
    "--title",
    required=True,
    help="Document title.",
)
```

**Exit-code pattern** (217-219, 264-269): `click.echo(..., err=True)` then `sys.exit(N)`. Code 2 means config/usage, 1 means scan, 3 means Paperless.

**Token construction site** (234-238): `settings.paperless.token` becomes `.get_secret_value()`.

**Done echo** (240-245) must print the resolved title.

**uvicorn level** (458-465) is unchanged in shape: `log_level=settings.output.log_level.lower()`.

**auto-profiles write and output to replace** (486-500). Wrap the write in `except ConfigError` (and `OSError`), then echo and `sys.exit(2)`:
```python
    # The file that was loaded (including an explicit --config), else the
    # documented ./saneless.toml default. XDG placement is CFG-03 (Phase 27).
    config_path = settings.config_path or Path("./saneless.toml")
    written = write_profiles_to_config(config_path, profiles, force=force)

    if not written:
        click.echo("No new profiles written (use --force to overwrite).")
        return

    click.echo(f"Generated {len(written)} profile(s) in {config_path}:")
```

---

### `src/saneless/worker.py` (service, event-driven startup)

**Analog:** itself.

**`_profiles_after_persist` signature and set construction** (120-156). `written: list[str] | None` becomes `result: ProfileWriteResult | None`, and `persisted = set(written)` becomes `result.persisted`. Rewrite the docstring sentence at 128-131 ("skips a name the file already defines").

**Persist call and log** (825-859). Keep both except branches exactly. The existing `(OSError, ConfigError)` branch already covers EBUSY. Only the success log (853-858) and the return type change:
```python
        logger.info(
            "Auto-profiles: wrote %d profile(s) to %s: %s",
            len(written),
            config_path,
            ", ".join(written) or "(none new)",
        )
        return written
```
Build the grouped text with the same vocabulary as the CLI (D-04), still passed as a `%s` arg (S-3). If the planner puts the group formatting on `ProfileWriteResult` (for example a `describe()` method), both the CLI and the worker share it, satisfying "same vocabulary" with one implementation.

**Profile lookup under the lock** (675-690) is the web title source:
```python
    def get_profile(self, name: str) -> ProfileConfig | None:
        with self._profiles_lock:
            return self._settings.profiles.get(name)
```

---

### `src/saneless/web/app.py` (factory)

**Analog:** itself, 60-64. Change one line: `token=settings.paperless.token.get_secret_value(),`.

### `src/saneless/web/routes.py` (controller)

**Analog:** itself, 405-412:
```python
    state = request.app.state
    if not state.worker.has_profile(profile):
        raise RequestRejected(RequestRejection.UNKNOWN_PROFILE)
    if not title:
        title = f"Scan {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M')}"
    form = _ScanForm(
        profile=profile, title=title, tags=tags, correspondent=correspondent
    )
```
Replace it with `found = state.worker.get_profile(profile)`. If `found is None`, raise `RequestRejected(RequestRejection.UNKNOWN_PROFILE)`. Then set `title = resolve_job_title(title, found, now=datetime.now(tz=UTC))`. Update the `Args:` line at 397 ("auto-generated if empty"). Import `resolve_job_title` with the absolute `from saneless.config import ...` form (routes.py uses absolute imports, lines 13-26).

---

### `tests/conftest.py` (fixture)

**Analog:** `clean_env`, 130-135:
```python
@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all SANELESS_* env vars before each test."""
    for key in list(os.environ):
        if key.startswith("SANELESS_"):
            monkeypatch.delenv(key, raising=False)
```
Add `monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)` and the same for `XDG_STATE_HOME`, and update the docstring. `default_settings` (138-154) passes `token=auth` as a `str`. It stays valid for a `SecretStr` field (RESEARCH verified for ty and pyrefly).

---

### `tests/test_config.py` (test, unit)

**Analog:** itself.

**File header and imports** (1-28): `import saneless.config as config_mod` for private access (`config_mod._is_legacy_manual_duplex_source`, line 497). Tests of the new renderer helpers can use `config_mod._...` the same way.

**Class and fixture convention.** A class docstring cites decision IDs. A class-scoped fixture redirects cwd and HOME (145-165):
```python
class TestLoadedConfigPath:
    """
    Settings record the config file that was actually loaded (D-16, M-04).
    ...
    """

    @pytest.fixture
    def empty_cwd_and_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        """
        Run in an empty CWD with HOME redirected into tmp_path.

        A developer's real ``~/.config/saneless/config.toml`` must not leak in.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        return tmp_path
```
Reuse `empty_cwd_and_home` for the XDG tests. Add `monkeypatch.setenv("XDG_CONFIG_HOME", ...)` inside the test.

**TOML-in-test-body plus `pytest.raises(ConfigError, match=...)`** (305-311):
```python
    def test_wrong_section_name_gives_helpful_error(self, tmp_config_dir: Path) -> None:
        """TOML [default] instead of [profiles.default] gives a helpful ConfigError."""
        toml_content = '[default]\ntitle = "Test Doc"\n'
        config_file = tmp_config_dir / "wrong_section.toml"
        config_file.write_text(toml_content)
        with pytest.raises(ConfigError, match=r"profiles\.default"):
            load_settings(config_path=str(config_file))
```

**caplog helper filtered by logger name** (581-588). Copy it for the CFG-11 and never-echoes tests:
```python
    @staticmethod
    def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
        """Return the WARNING messages saneless.config emitted."""
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING and record.name == "saneless.config"
        ]
```
Use it with `with caplog.at_level(logging.WARNING, logger="saneless.config"):` (609-610).

**Secret-value naming convention** (39-40, 52-55): assign the token to a local (`expected_auth = "abc123"`) rather than inlining it. Keep this for the token-shaped value in D-14 tests.

**chmod plus restore pattern** (753-769) for the nearest-ancestor test:
```python
        parent = tmp_path / "readonly"
        parent.mkdir()
        parent.chmod(0o444)
        ...
        with pytest.raises(ConfigError, match="not writable"):
            validate_settings_dirs(settings)
        # Restore permissions for cleanup
        parent.chmod(0o755)
```
Existing tests to flip (verified lines): 40 and 55 (`.get_secret_value()`), 78 (`ValueError` becomes `ConfigError`, keep `match="default"`), 174-182 (missing explicit path becomes `ConfigError`), 203-216 (`SANELESS_CONFIG_PATH` becomes rejected), 229-235 (search order uses `xdg_config_home()`), 322-336 (`default_title`), and 442 (`ValidationError` becomes `ConfigError`, keep `match="flip_timeout_seconds"`). Line 253 is unchanged: a TOML syntax error keeps `ValueError` (Phase 28).

---

### `tests/test_atomic_write.py` (NEW test, file-I/O)

**Analog 1:** `tests/test_paperless.py` 742-775. Monkeypatch a primitive to fail, assert the exception, and assert that no leftover remains:
```python
    def test_a_failed_staged_write_leaves_the_directory_empty(
        self, sample_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A copy that dies part-way leaves nothing behind and still raises.

        Without the cleanup a retry could later find -- or paperless-ngx
        could consume -- a truncated remnant.
        """
        ...
        def half_a_file(fsrc: BinaryIO, fdst: BinaryIO, length: int = 0) -> None:
            """Write half the bytes, then fail the way a full disk would."""
            fdst.write(b"%PDF-1.4 trun")
            msg = "no space left on device"
            raise OSError(msg)

        monkeypatch.setattr(shutil, "copyfileobj", half_a_file)
        ...
        assert sorted(entry.name for entry in consume_dir.iterdir()) == []
```
**Analog 2:** the leftover-file helper, `tests/test_paperless.py` 148-161 (`_assert_no_staging_files`: dotfile names, with a message that explains why).
**Analog 3:** a permission test that skips under root, `tests/test_worker.py` 3832-3833 and 3850-3854:
```python
        if os.geteuid() == 0:
            pytest.skip("root bypasses file permissions")
        ...
        finally:
            ...
            locked.chmod(0o700)
            config_file.chmod(0o600)
```
The EBUSY fake must patch `Path.replace` (S-2). The fake needs a typed signature (`def busy(self: Path, target: object) -> Path:`) that raises `OSError(errno.EBUSY, os.strerror(errno.EBUSY))`. An optional `unshare` test uses `@pytest.mark.skipif`. Markers are strict (`--strict-markers`), so use only `skipif`, not a new marker.

---

### `tests/test_auto_profiles.py` (test, file-I/O)

**Analog:** itself.

**Fixture-string class pattern** (1010-1054). A class-level TOML constant with comments, plus `_generated()` and `_write()` helpers and a parametrized `force`:
```python
class TestOrphanedProfilePrune:
    """Auto-generated profiles absent from the new set are pruned (D-16)."""

    _EXISTING = """\
# saneless configuration -- hand written, keep this comment
[profiles.default]
source = "Flatbed"  # the one I actually use
...
"""

    def _write(self, tmp_path: Path, *, force: bool) -> Path:
        """Write the generated set over the existing config and return the path."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(self._EXISTING)
        write_profiles_to_config(config_file, self._generated(), force=force)
        return config_file

    @pytest.mark.parametrize("force", [False, True])
    def test_orphaned_auto_generated_profile_is_removed(
        self, tmp_path: Path, *, force: bool
    ) -> None:
```
For the new `TestForceMerge` class, copy this shape exactly: a flagged profile with `default_tags`, `title`, an inline comment, and a stale `duplex`, next to an unflagged same-name profile.

**Untouched-file assertion** (942-950), for the "byte-identical" D-01 test and the inline-table `tomllib` guard test:
```python
        with pytest.raises(ConfigError):
            write_profiles_to_config(config_file, self._generated())

        assert config_file.read_text() == self._MALFORMED
```
Use `read_bytes()` for byte identity and CRLF tests.

**Parse-back assertion idiom:** `tomllib.loads(config_file.read_text())["profiles"]` (1069-1084). Round-trip through `load_settings` (1163-1173).

Existing tests to flip (verified): 1098-1103 (`set(written)` becomes `result.added`), 1194-1199, 1217-1221 (`written == []` becomes the result fields), **1223-1243** (`auto_generated = false` under force is now skipped and the file is unchanged), and 1302-1304.

Imports (1-31): `Path` is under `TYPE_CHECKING`. Keep it there unless a test calls `Path(...)`.

---

### `tests/test_logging.py` (test, unit)

**Analog:** itself. Extend the cleanup helper (20-26) to reset the `saneless` logger (Pitfall 12):
```python
    def _cleanup_handlers(self) -> None:
        """Remove all handlers from root logger to prevent leaks."""
        root = logging.getLogger()
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)
        root.setLevel(logging.WARNING)
```
New `-v` tests follow the `try: configure_logging(...) ... finally: self._cleanup_handlers()` shape (44-52). Assert with `logging.getLogger("saneless.pipeline").getEffectiveLevel() == logging.DEBUG` and `logging.getLogger("httpx").getEffectiveLevel() == logging.INFO`. `test_default_log_file_is_xdg_compliant` (134-138) stays valid with XDG unset. Add a set-XDG sibling.

---

### `tests/test_cli.py` (test, CliRunner)

**Analog:** itself.

**Standard patching helper** (68-96). New CLI tests use `_patch_cli`. Tests that need a failing load patch `load_settings` directly, as 233-246 does:
```python
    def test_scan_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config loading fails -> exit code 2."""
        runner = CliRunner()

        def bad_load(*_args: object, **_kwargs: object) -> None:
            msg = "bad config"
            raise ValueError(msg)

        monkeypatch.setattr("saneless.cli.load_settings", bad_load)
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: None)

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 2
        assert "Configuration error" in result.output
```
For CFG-10, copy this and invoke `["serve", "--help"]`. Assert `exit_code == 0` and `"--host" in result.output`, and patch `configure_logging` with a recorder (the `capture_logging` closure at 848-850) to assert it was never called.

**Recorder closure pattern** (844-872):
```python
        captured: dict[str, object] = {}

        def capture_logging(*_args: object, **kwargs: object) -> None:
            """Record whether verbose was passed."""
            captured["verbose"] = kwargs.get("verbose", False)
```

**Real load, no patching, with root-logger restore** (904-951). This is the analog for "broken real config creates no log dir" (CFG-10) and the CFG-11 caplog test. Extend the `finally` block to reset `logging.getLogger("saneless").setLevel(logging.NOTSET)`:
```python
        root_logger = logging.getLogger()
        handlers_before = list(root_logger.handlers)
        level_before = root_logger.level
        try:
            result = CliRunner().invoke(
                cli, ["--config", str(config_file), "jobs", "--limit", "1"]
            )
        finally:
            # configure_logging adds to the ROOT logger; remove what this
            # invocation added so later tests do not write into tmp_path.
            for handler in list(root_logger.handlers):
                if handler not in handlers_before:
                    root_logger.removeHandler(handler)
                    handler.close()
            root_logger.setLevel(level_before)
```
That test builds its TOML with `tomlkit.document()`/`tomlkit.table()` (917-930). Reuse the same approach for configs with `log_file` under `tmp_path`.

**auto-profiles tests** (1259-1408). `_make_auto_scanner` (1262-1305) is the scanner stub. Tests to flip:
- **1360-1384 `test_auto_profiles_force_flag`** pre-populates an *unflagged* `flatbed` table and expects it overwritten. Under D-01 it is skipped. RESEARCH Pitfall 11 does not list this test. Rewrite it with `existing.add("auto_generated", flag)` for the refresh case, and add a sibling for the unflagged skip.
- 1386-1408 asserts `"No new profiles written"`, which becomes the grouped wording.
- 1307-1322 asserts `"Generated"` and `"  flatbed: source=Flatbed"`. The planner decides whether the per-profile detail lines survive under `Added:`.
- 212-216 `test_scan_requires_title` becomes exit 0 with a fallback title. 218-223 shows the `Done: <title>` assertion shape.
- `TestServeCommand._capture_uvicorn` (1172-1192) is the analog for any uvicorn level test. `test_serve_log_level` (1219-1227) is unchanged.

---

### `tests/test_worker.py` (test, threaded)

**Analog:** itself, `TestStartupProfileGeneration` 3663-3862.

Pattern: `_mock_caps_scanner(mock_scanner)`, set `default_settings._config_path = config_file`, then run `worker.start()` / `_wait_until(...)` inside `try/finally: worker.stop(); store.close()`. Assert on `tomllib.loads(config_file.read_text())` and on `_worker_records(caplog, level, text)` (1983-1993).

The EBUSY worker test copies 3823-3862 (`test_startup_generation_keeps_profiles_when_the_file_is_unwritable`) and swaps the chmod setup for `monkeypatch.setattr(Path, "replace", busy)`. Assert `"ConfigError" in message` and `"will not survive a restart"`. The existing test's `"PermissionError" in message` assertion (3862) stays green only if `atomic_write` keeps the `os.access(W_OK)` pre-check that raises `PermissionError` (Pitfall 6).

---

### `tests/test_web.py` (test, TestClient)

**Analog:** itself. The fixture chain is `test_settings` (91-109), then `app` (118-121), then `client` (137-142). The profile dict is set in `test_settings` at 105-108. A title test adds `ProfileConfig(title="Receipt")` there, or builds its own settings fixture. Submit and read back (264-270, plus the store access at 282):
```python
def test_scan_form_submit(client: TestClient) -> None:
    """POST /api/scan creates job and returns status partial (PLSS-04, UI-07)."""
    response = client.post(
        "/api/scan", data={"profile": "default", "title": "Test Scan"}
    )
    assert response.status_code == 200
```
`job_store: JobStore = _app(client).state.job_store` (282). Then read the newest job's `.title`, as 404-408 does (`newest.title == "Refused Scan"`). Tests here are module-level functions, not classes. Follow that.

---

### `tests/test_outcomes_e2e.py` (test, lines 663, 734)

Mechanical: `token=settings.paperless.token` becomes `token=settings.paperless.token.get_secret_value()`. It mirrors the `web/app.py` 62 change.

---

### `tests/test_deployment_config.py` (NEW test, static text)

**Analog:** `tests/test_vendor_assets.py`. It uses a module docstring that explains the contract (1-14), module-level path constants (28-30), module-level `test_` functions, and `read_text(encoding="utf-8")` with failure messages that name the file (129-136):
```python
def test_templates_reference_no_external_url() -> None:
    """No template contains an http:// or https:// URL, so no internet is needed."""
    templates = sorted(TEMPLATE_DIR.rglob("*.html"))
    assert templates
    for template in templates:
        text = template.read_text(encoding="utf-8")
        assert "http://" not in text, f"{template} references http://"
        assert "https://" not in text, f"{template} references https://"
```
Delta: there is no existing repo-root constant, so define `REPO_ROOT = Path(__file__).resolve().parents[1]`, then `COMPOSE = REPO_ROOT / "docker-compose.yml"` and `DOCS_DIR = REPO_ROOT / "docs"`. Keep the `assert templates` non-empty guard (for example `assert docs`) so a moved directory cannot pass vacuously. Use plain-text assertions, no PyYAML (RESEARCH). Current offenders that the test must see disappear (grep verified): `docker-compose.yml:19`, `docs/getting-started/quick-start.md:19`, `docs/how-to/deploy-docker-compose.md:28,52,71`, `docs/reference/docker.md:97,121,137,155`.

---

### `docker-compose.yml` and `saneless.toml.example` (config)

**Analog:** themselves. Compose comment style is a top-of-file bullet list (lines 1-8) plus inline `# WARNING:` comments above the volume line (16-19):
```yaml
    volumes:
      # WARNING: config.toml must exist on the host before first run.
      # Create it with at minimum: [profiles.default] and [paperless] sections.
      # See: https://github.com/kris-knigga/saneless#configuration
      - ./config.toml:/etc/saneless/config.toml:ro
```
Change only lines 4 and 16-19. Leave line 12 (image), 18's URL owner (Phase 31), and 23-28 (environment) alone. `saneless.toml.example` uses trailing inline comments (`host = ""       # SANE network host...`, line 2) and commented-out keys (`# title = ""`, line 21). Keep that style for the title comment.

### Docs (`docs/**/*.md`)

**Analog:** `docs/how-to/deploy-docker-compose.md`. It uses MkDocs-Material admonitions (27-28, `!!! warning` plus 4-space indented body), fenced `yaml`/`toml` blocks (16-25, 34-65), and a "Key details:" bullet list with a bold lead-in term (67-74). Edit the listed lines in place (RESEARCH "Docs and Compose Change List" has every file:line). Add the migration note next to the existing upgrade section (~180-215) in the same bullet style.

---

## No Analog Found

| File | Role | Data Flow | Reason |
|---|---|---|---|
| (none fully unmatched) | | | `tests/test_deployment_config.py` has only a partial analog (package-relative files, not repo root). Its repo-root constant is new; everything else copies `test_vendor_assets.py`. |

Behaviour with no in-repo precedent, where the planner should use RESEARCH patterns:
- `EnvSettingsSource(Settings)()` as the env-contribution oracle (RESEARCH Pattern 3). Nothing in `src/` instantiates a settings source outside `settings_customise_sources` (config.py 245-271).
- `difflib.get_close_matches` did-you-mean (RESEARCH Pattern 2). There is no existing use.
- tomlkit in-place `table[key] = value` / `del table[key]` merge and the `InlineTable` guard (RESEARCH Pattern 5). Today's writer only assigns fresh tables.
- `fchown`/`fchmod`/directory fsync (RESEARCH Pattern 4). `paperless.py` deliberately skips directory fsync (359-361). The config file is a system of record, so this phase does it best effort.

## Metadata

**Analog search scope:** `src/saneless/**/*.py`, `tests/*.py`, `docker-compose.yml`, `saneless.toml.example`, `docs/**/*.md`, `pyproject.toml` (ruff rule set)
**Files read or scanned:** 20
**Pattern extraction date:** 2026-09-15
