# Phase 31: Delivery, Identity, and Documentation Accuracy - Pattern Map

**Mapped:** 2026-09-18
**Files analyzed:** 30 (1 new source-adjacent file, 1 new doc page, 1 new planning artifact, 1 move, 26 modifications)
**Analogs found:** 27 / 30

This phase is config, workflow, packaging and documentation work plus **one narrow
behaviour change** (the `serve` logging mode split). Almost every "new" thing is a new
*section* in an existing file, so most analogs are **in-file conventions** rather than
sibling files. Three items have no prior art in this repository and are called out
plainly in "No Analog Found".

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `tests/test_deployment_config.py` (MODIFY — 6 new sections, 3 deletions) | test (doc-truth harness) | static text-over-files | itself (`tests/test_deployment_config.py:38-106, 211-214, 798-806, 980-1039`) | exact (in-file) |
| `tests/test_deployment_config.py` — naming guard subsection | test | subprocess → text scan | `tests/test_scanner.py:2623-2634`, `tests/test_atomic_write.py:684-702` | role-match (subprocess shape) |
| `tests/test_cli.py` (MODIFY — `TestServeLogging`, `--version`) | test | CliRunner request-response | `TestServeCommand` (`tests/test_cli.py:2025-2075`), `_patch_cli` (`:119-164`), `_restored_root_logging`, `test_stderr_log_fallback_*` (`:3125-3190`) | exact |
| `tests/test_logging.py` (MODIFY — stream-mode cases) | test | direct-call unit + global state | `TestConfigureLogging._cleanup_handlers` + `test_stderr_fallback_renders_no_traceback` (`tests/test_logging.py:~40-50, 278-300`) | exact |
| `src/saneless/logging_config.py` (MODIFY — `stream:` keyword) | config/bootstrap utility | process-global side effect, returns `attached` | itself (`configure_logging`, `_TracebackFreeFormatter`) | exact (in-file) |
| `src/saneless/cli.py` (MODIFY — `version_option`, `stream_logs=`, `serve`, line 917 string) | controller (CLI entry) | request-response | itself (`cli` group `:482-505`, `_load_cli_settings` `:506-556`, `serve` `:848-920`) | exact (in-file) |
| `src/saneless/config.py` (MODIFY — docstring scope note only) | model | none (declarative) | `OutputConfig` field docstrings | exact (in-file) |
| `pyproject.toml` (MODIFY — version, license, classifiers, dev dep) | config | declarative | itself (`[project]` `:1-44`, `[dependency-groups]`) | exact (in-file) |
| `uv.lock` (REGENERATE) | lockfile | n/a | n/a — tool output, never hand-edited | n/a |
| `.github/workflows/ci.yml` (MODIFY — `workflow_call`, `permissions:`, `persist-credentials`, zizmor step) | config/workflow | CI pipeline | itself — **it is the style canon** | exact (in-file) |
| `.github/workflows/release.yml` (REWRITE) | config/workflow | tag-triggered pipeline | `.github/workflows/ci.yml` | role-match |
| `.github/workflows/docs.yml` (REWRITE) | config/workflow | push-triggered pipeline | `.github/workflows/ci.yml` | role-match |
| `.github/dependabot.yml` (MODIFY — 2nd ecosystem + 2 cooldowns) | config | declarative | itself (existing `github-actions` entry) | exact (in-file) |
| `.pre-commit-config.yaml` (MODIFY — zizmor hook, optional pytest hook) | config | hook pipeline | `repo: local` block (`:93-158`) | exact (in-file) |
| `Dockerfile` (MODIFY — digest pins, `FROM … AS uv`, explicit `COPY`, user 1000, `WORKDIR`) | config/build | image build | itself (`ENV`/`VOLUME` comment convention `:19-29`) | exact (in-file) |
| `.dockerignore` (NEW) | config/build | build-context filter | **none** | no analog |
| `docker-compose.yml` (MODIFY — image ref, anchor link, commented `user:`) | config | declarative deployment | itself (commented-with-rationale convention throughout) | exact (in-file) |
| `saneless.toml.example` (MODIFY — `web_port` commented out) | config example | declarative | itself (`# tmp_dir = …`, `# log_level = …` at `:12-13`) | exact (in-file) |
| `.gitignore` (MODIFY — drop `*.png`, add `/test-results/`) | config | declarative | itself (`:207 /site`, `:310 .playwright-mcp/`) | exact (in-file) |
| `mkdocs.yml` (MODIFY — 3 name refs + nav insert) | config | declarative | itself (`nav:` `:38-60`) | exact (in-file) |
| `README.md` (MODIFY — rows 16/17/18/19) | documentation | prose + examples | itself | exact (in-file) |
| `docs/getting-started/which-setup.md` (NEW) | documentation (decision list) | prose + copy-paste blocks | `docs/getting-started/quick-start.md` (tabbed `=== "Docker"` blocks), `docs/how-to/scanner-host-discovery.md` | role-match |
| `docs/getting-started/quick-start.md` (MODIFY — rows 19, 31; DOCS-04 link; DOCS-05 sentence) | documentation | prose | itself | exact (in-file) |
| `docs/getting-started/first-cli-scan.md` (MODIFY — rows 19, 29 @ `:9, :67`) | documentation | prose | `docs/how-to/scanner-host-discovery.md:13` (the canonical USB sentence) | role-match |
| `docs/how-to/install-bare-metal.md` (MODIFY — row 19 only; `:9` already correct) | documentation | prose | itself | exact (in-file) |
| `docs/how-to/deploy-docker-compose.md` (MODIFY — rows 19, 32) | documentation | prose | `docker-compose.yml`'s own "commented out ON PURPOSE" rationale block | partial |
| `docs/how-to/scanner-host-discovery.md` (MODIFY — row 19 only) | documentation | prose | itself | exact (in-file) |
| `docs/how-to/cli-scripting.md` (MODIFY — row 30 @ `:45`) | documentation | JSON example | `docs/how-to/cli-scripting.md:49` (the `created_at` field that *was* corrected) | exact (in-file) |
| `docs/reference/docker.md` (MODIFY — rows 19, 21; **delete** USB section `:154-170`) | documentation | reference table | its own env-var table (`:105-113`) | exact (in-file) |
| `docs/reference/configuration.md` (MODIFY — `:38` USB caveat; log-key mode scope) | documentation | reference table | its own `[web]`/`[output]` rows | exact (in-file) |
| `docs/PRD.md` → `.planning/milestones/v1.0-PRD.md` (MOVE) | documentation → planning artifact | n/a | `.planning/milestones/v1.0-ROADMAP.md` (destination sibling) | exact |
| `.planning/phases/31-…/31-AUDIT.md` (NEW) | planning artifact | row-disposition table | `.planning/v1.0-MILESTONE-AUDIT.md` | role-match |

---

## Pattern Assignments

### `tests/test_deployment_config.py` (test, static text-over-files) — THE central analog

**Analog:** itself. Six new sections join a 1085-line harness that already defines every
helper they need. Do not invent a new file, new helpers, or a new failure-message style.

**Imports + module constants** (`tests/test_deployment_config.py:38-71`):

```python
from __future__ import annotations

import re
from pathlib import Path

from saneless.config import WebConfig, is_placeholder_token
from saneless.vocabulary import (
    ExitCode,
    RequestRejection,
    rejection_message,
    rejection_status_code,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCS_DIR = REPO_ROOT / "docs"

DIRECTORY_MOUNT = "./config:/etc/saneless"
SINGLE_FILE_MOUNT = "config.toml:/etc/saneless/config.toml"

DEPLOY_HOWTO = DOCS_DIR / "how-to" / "deploy-docker-compose.md"
DOCKER_REFERENCE = DOCS_DIR / "reference" / "docker.md"
QUICK_START = DOCS_DIR / "getting-started" / "quick-start.md"
...
TOML_EXAMPLE = REPO_ROOT / "saneless.toml.example"
```

> **Copy this for:** the new path constants this phase needs — `DOCKERFILE = REPO_ROOT /
> "Dockerfile"`, `DOCKERIGNORE = REPO_ROOT / ".dockerignore"`, `GITIGNORE = REPO_ROOT /
> ".gitignore"`, `PYPROJECT = REPO_ROOT / "pyproject.toml"`, `README = REPO_ROOT /
> "README.md"`, `WHICH_SETUP = DOCS_DIR / "getting-started" / "which-setup.md"`.
> All-caps module constants at the top, **not** inside the test bodies.

**File-set helpers** (`:73-83`) — every "no page anywhere says X" test iterates these:

```python
def _doc_pages() -> list[Path]:
    """Return every Markdown page under ``docs/``, asserting there is at least one."""
    pages = sorted(DOCS_DIR.rglob("*.md"))
    assert pages, f"no Markdown pages found under {DOCS_DIR}"
    return pages


def _deployment_files() -> list[Path]:
    """Return the compose example plus every doc page."""
    return [COMPOSE, *_doc_pages()]
```

**Read helper** (`:211-214`) — used by every single-file assertion, and the reason failure
messages carry a repo-relative name instead of an absolute path:

```python
def _read(path: Path) -> tuple[str, Path]:
    """Return a file's text and its repo-relative name for failure messages."""
    return path.read_text(encoding="utf-8"), path.relative_to(REPO_ROOT)
```

**Line-numbering helpers** (`:798-806`) — the naming guard and the `.dockerignore` test
both want `file:line`:

```python
def _is_comment(line: str) -> bool:
    """Say whether a YAML (or fenced-YAML) line is commented out."""
    return line.lstrip().startswith("#")


def _numbered(path: Path) -> list[tuple[int, str]]:
    """Return ``(line number, line)`` pairs for a file, 1-based."""
    return list(enumerate(path.read_text(encoding="utf-8").splitlines(), start=1))
```

**Offender-list pattern — the exact failure-message convention** (`:93-106`). The naming
guard is this shape with a different needle and a different file set:

```python
def test_no_single_file_config_mount_anywhere() -> None:
    """No compose file or doc page bind-mounts ``config.toml`` as a single file."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in _deployment_files()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if SINGLE_FILE_MOUNT in line
    ]
    assert not offenders, "single-file config.toml mount found:\n" + "\n".join(
        offenders
    )
```

> Note the three invariants: (1) a comprehension building `f"{relpath}:{lineno}: {line}"`,
> (2) `assert not offenders, "<what went wrong>:\n" + "\n".join(offenders)`, (3) a
> one-line docstring stating the contract in the present tense.

**Expectation-derived-from-source pattern** (`:980-1007`) — the model for every audit row
whose truth is "a documented value matches a source constant":

```python
def _declared_routes() -> list[tuple[str, str]]:
    """Return ``(METHOD, path)`` for every route declared in ``routes.py``."""
    source = ROUTES_MODULE.read_text(encoding="utf-8")
    routes = [
        (method.upper(), path) for method, path in _ROUTE_DECORATOR.findall(source)
    ]
    assert routes, f"no route decorators found in {ROUTES_MODULE.name}"
    return routes


def test_every_route_is_documented_in_the_web_api_reference() -> None:
    """
    Every route in ``routes.py`` has its own heading in ``web-api.md``.

    The expectation is derived from the decorators rather than a hard-coded
    list, so a route added in a later phase cannot ship undocumented: adding it
    fails this test until the reference gains its section (T-30-89).
    """
    text, name = _read(WEB_API_REFERENCE)
    missing = [
        f"{method} {path}"
        for method, path in _declared_routes()
        if f"`{method} {path}`" not in text
    ]
    assert not missing, (
        f"{name} has no `METHOD /path` heading for these routes:\n" + "\n".join(missing)
    )
```

And the model-field variant (`:1023-1039`), which imports the model rather than reading
its file:

```python
def test_every_web_config_field_is_documented() -> None:
    """
    Each ``[web]`` key is in the config reference with a ``SANELESS_WEB__`` row.

    Derived from ``WebConfig.model_fields``: a key added to the section later
    cannot ship without both references gaining it (APPL-10, T-30-89).
    """
    config_text, config_name = _read(CONFIG_REFERENCE)
    env_text, env_name = _read(ENV_REFERENCE)
    ...
    for field in WebConfig.model_fields:
        assert field in config_text, f"{config_name} does not document [web] {field}"
        variable = f"SANELESS_WEB__{field.upper()}"
        assert variable in env_text, f"{env_name} has no {variable} row"
```

> **Apply to:** D-45 assertion 2 (README's `source = "…"` must be a real SANE spelling —
> derive from `saneless.config.ProfileConfig.model_fields["source"].default`, which is
> `"Flatbed"` at `src/saneless/config.py:362`, rather than hard-coding the word);
> DLVR-05's port assertions (derive `8080` from `OutputConfig`'s `web_port` default and
> compare `Dockerfile`'s `EXPOSE`/`HEALTHCHECK` and every doc page against it).

**Section-banner convention** (`:779-786`) — new phase sections get a banner comment plus a
rationale comment above any pinned constant:

```python
# ---------------------------------------------------------------------------
# Phase 30: the shipped deployment template (D-17, APPL-07, APPL-11, APPL-12)
# ---------------------------------------------------------------------------

# The number of ``kris-knigga`` strings in ``docker-compose.yml`` before this
# phase: ... if this fires because the rename really happened, the fix is to
# update this constant in that phase, not to weaken the assertion (T-30-88).
COMPOSE_KRIS_KNIGGA_COUNT = 2
```

**The three deletions D-12 requires** (all in this file, all must go in the same commit as
the rename):

| What | Where | Why |
|------|-------|-----|
| Module-docstring sentence *"They also pin the `kris-knigga` occurrence count…"* | `tests/test_deployment_config.py:28-31` | otherwise the new guard fires on the guard file's own docstring |
| `COMPOSE_KRIS_KNIGGA_COUNT = 2` and its 6-line rationale comment | `:781-787` | the constant's purpose is gone |
| `test_phase_30_does_not_perform_the_dlvr_01_rename` (body `:920-928`) | `:920-928` | it asserts the compose file **still** says the old name |

Current text of the test being deleted, for the executor to match exactly:

```python
def test_phase_30_does_not_perform_the_dlvr_01_rename() -> None:
    """``kris-knigga`` appears in the compose template exactly as often as before."""
    count = COMPOSE.read_text(encoding="utf-8").count("kris-knigga")
    assert count == COMPOSE_KRIS_KNIGGA_COUNT, (
        f"{COMPOSE.name} has {count} 'kris-knigga' strings, expected "
        f"{COMPOSE_KRIS_KNIGGA_COUNT}. Renaming them is DLVR-01 in Phase 31; "
        "Phase 30 must leave the image reference and its link alone (T-30-88)"
    )
```

---

### `tests/test_deployment_config.py` — the naming guard subsection (test, subprocess + text scan)

**Analog:** `tests/test_scanner.py:2623-2634` and `tests/test_atomic_write.py:684-702`.
Both pass ruff `S603` because **every argv element is a string literal**. Copy the shape,
not just the idea.

`tests/test_scanner.py:2623-2634` — including the comment that documents the rule:

```python
        # Every argv element is a literal and the per-run paths travel in the
        # environment, quoted so they are never re-split -- the shape
        # test_atomic_write.py established for the one other child-process
        # test in this suite.
        result = subprocess.run(
            ["/bin/sh", "-c", 'exec "$SANELESS_TEST_PYTHON" "$SANELESS_TEST_CHILD"'],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=_CHILD_EXIT_SECONDS,
        )
```

`tests/test_atomic_write.py:711-717` — the shortest literal-argv example:

```python
    probe = subprocess.run(
        ["/usr/bin/unshare", "--user", "--map-root-user", "--mount", "/bin/true"],
        capture_output=True,
        check=False,
        timeout=30,
    )
```

> **Apply to the guard:** `["/usr/bin/git", "ls-files", "-z"]` with `cwd=REPO_ROOT`,
> `check=True`, `timeout=30`. `cwd=` is a keyword, not an argv element — putting
> `str(REPO_ROOT)` in the list via `git -C <path>` trips `S603` and suppression is
> forbidden (RESEARCH §8 Blocker A, Pitfall 7).

**The D-11 runtime string assembly** must be an f-string over named module constants.
`"-".join(("kris", "knigga"))` is rewritten back into the literal by ruff `FLY002`, which
would put the forbidden string into the guard's own source and make the guard fail on
itself (RESEARCH §8 Blocker B, Pitfall 6). The comment explaining *why* is required by
D-11 — follow the in-file precedent of `test_scanner.py`'s subprocess comment and
`COMPOSE_KRIS_KNIGGA_COUNT`'s rationale block: explain the trap, name the rule.

---

### `tests/test_cli.py` (test, CliRunner request-response)

**Analog:** `TestServeCommand` (`tests/test_cli.py:2025-2075`) for anything invoking
`serve`; `test_stderr_log_fallback_prints_no_traceback_without_verbose`
(`:3125-3160`) for anything asserting on real logging output.

**Class + static-helper pattern** (`:2025-2053`):

```python
class TestServeCommand:
    """Serve command tests."""

    @staticmethod
    def _mock_socket(monkeypatch: pytest.MonkeyPatch) -> None:
        """Bypass the port-availability check in serve()."""
        mock_sock = MagicMock()
        monkeypatch.setattr("saneless.cli.socket.socket", lambda *_a, **_kw: mock_sock)

    @staticmethod
    def _capture_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        """Patch uvicorn.run to capture args and close the app's job_store."""
        captured: dict[str, object] = {}

        def mock_uvicorn_run(app: object, **kwargs: object) -> None:
            captured["app"] = app
            captured.update(kwargs)
            ...
        monkeypatch.setattr("saneless.cli.uvicorn.run", mock_uvicorn_run)
        return captured
```

**The stub that absorbs a new keyword** (`_patch_cli`, `tests/test_cli.py:145-148`) — this
is why a defaulted `stream_logs=` keyword leaves all 7 `TestServeCommand` tests green:

```python
    monkeypatch.setattr(
        "saneless.cli.configure_logging",
        lambda *_args, **_kwargs: None,
    )
```

**Testing the REAL `configure_logging` through the CLI** (`:3125-3160`) — this is the
pattern `TestServeLogging` must use, because a stub that attaches nothing cannot prove a
handler was or was not attached:

```python
        runner, _ = _patch_cli(
            monkeypatch, settings=settings, scanner_cls=_raising_scanner(exc)
        )
        monkeypatch.setattr("saneless.cli.configure_logging", configure_logging)

        with _restored_root_logging():
            result = runner.invoke(cli, ["scan"])

        assert result.exit_code == code
        assert "Traceback" not in result.stderr
```

**Root-logger restoration contextmanager** (`tests/test_cli.py`, `_restored_root_logging`)
— mandatory around any test that lets the real `configure_logging` run; it removes only
handlers added inside the block so pytest's capture handlers survive:

```python
def _restored_root_logging() -> Generator[None]:
    """
    Undo what a real ``configure_logging`` call does to the root logger.

    Only the handlers added inside the block are removed, so pytest's own
    capture handlers survive; a stderr handler left behind would write to the
    runner's closed stream in every later test.
    """
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            if handler not in handlers:
                handler.close()
                root.removeHandler(handler)
        root.setLevel(level)
        logging.getLogger("saneless").setLevel(logging.NOTSET)
```

**Existing assertion that D-37 must keep true** (`:2056-2069`) — do not weaken it:

```python
        assert captured["log_config"] is None
        assert captured["access_log"] is True
```

**For `--version` (DLVR-10):** there is no existing `--version` test. Follow the plain
`runner.invoke(cli, [...])` shape and heed RESEARCH Pitfall 10 — pass
`prog_name="saneless"`, or the output reads `cli, version 0.2.0` — and Pitfall 9 —
assert against `importlib.metadata.version("saneless")`, not against `pyproject.toml`'s
declared string (pin that separately in the deployment harness).

---

### `tests/test_logging.py` (test, direct-call unit with global state)

**Analog:** `TestConfigureLogging` in the same file. Two conventions matter.

**Mandatory handler cleanup in `finally`** (`tests/test_logging.py:~44-75`):

```python
    def _cleanup_handlers(self) -> None:
        root = logging.getLogger()
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)
        root.setLevel(logging.WARNING)
        logging.getLogger("saneless").setLevel(logging.NOTSET)

    def test_configure_logging_creates_file_handler(self, tmp_path: Path) -> None:
        """configure_logging adds a RotatingFileHandler to the root logger."""
        log_file = tmp_path / "logs" / "test.log"
        try:
            configure_logging(log_file=str(log_file))
            root = logging.getLogger()
            file_handlers = [
                h for h in root.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert len(file_handlers) >= 1
        finally:
            self._cleanup_handlers()
```

**Traceback presence/absence via `capsys`** (`:278-300`) — the exact shape the amended
D-36 stream tests need, inverted:

```python
    def test_stderr_fallback_renders_no_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ...
            configure_logging(str(blocker / "logs" / "saneless.log"))
            try:
                _raise_runtime_error("kaboom-4c1d")
            except RuntimeError:
                logging.getLogger("saneless.test").exception("it failed")
            err = capsys.readouterr().err
            assert "it failed" in err
            assert "Traceback" not in err
            assert "kaboom-4c1d" not in err
```

> All 16 existing `TestConfigureLogging` tests call `configure_logging` with today's
> positional/keyword signature. A **keyword-only parameter with a default** leaves every
> one of them untouched — which is the requirement, not an accident.

---

### `src/saneless/logging_config.py` (config/bootstrap utility, process-global side effect)

**Analog:** itself. The mode split adds a branch beside the existing fallback branch, not
a second module.

**The existing three-handler structure to extend** (`src/saneless/logging_config.py:85-124`):

```python
    formatter = logging.Formatter(_FORMAT)

    root_logger = logging.getLogger()
    # The level-name mapping rather than an attribute lookup on the module,
    # which would also "resolve" non-level names such as BASIC_FORMAT (CFG-04).
    root_logger.setLevel(logging.getLevelNamesMapping()[log_level.upper()])

    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(...)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        attached = True
    except OSError:
        stderr_handler = logging.StreamHandler(sys.stderr)
        # stderr is the user's terminal now, so this handler never renders a
        # traceback (CR-01). With -v the mirror handler below renders it, once,
        # as D-06 promises.
        stderr_handler.setFormatter(_TracebackFreeFormatter(_FORMAT))
        root_logger.addHandler(stderr_handler)
        root_logger.warning("Cannot write to %s, logging to stderr only", log_file)
        attached = False

    if verbose:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        root_logger.addHandler(stderr_handler)

    # -v is saneless's own detail. The root logger keeps the configured level so
    # httpx, multipart and uvicorn do not flood the log -- httpx's DEBUG output
    # can include the Paperless Authorization header (T-27-23). ...
    logging.getLogger("saneless").setLevel(logging.DEBUG if verbose else logging.NOTSET)
    return attached
```

> **Copy for the stream branch:** the same `StreamHandler(sys.stderr)` +
> `setFormatter(...)` + `addHandler(...)` triple; return `False` so the caller's existing
> `settings.output.log_file if attached else None` line evaluates to `None` with no edit.
> **Do not touch** the final `logging.getLogger("saneless").setLevel(...)` line — D-38 and
> T-27-23 depend on it exactly as written.
>
> Per the amended D-36, serve mode uses a **plain `Formatter(_FORMAT)`** (tracebacks
> render with or without `-v`), which is the *opposite* of the fallback branch. The
> difference must be explained in a comment in the style of the existing one — the
> justification is "the stream *is* the log; `docker logs` is nobody's terminal".

**Keyword-only signature convention already in place** (`:52-59`):

```python
def configure_logging(
    log_file: str,
    log_level: str = "INFO",
    max_bytes: int = 10_485_760,
    backup_count: int = 5,
    *,
    verbose: bool = False,
) -> bool:
```

> The new `stream:` parameter goes **after the `*`**, beside `verbose`, with a default of
> `False`. Google-style `Args:` / `Returns:` docstring sections are mandatory (ruff `D`).

---

### `src/saneless/cli.py` (controller, request-response)

**Analog:** itself, in three places.

**1. Group decorator stack — where `@click.version_option` goes** (`:482-496`):

```python
@click.group(cls=_GuardedGroup)
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to config file.",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Log saneless's own debug detail to the log file and stderr.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, *, verbose: bool) -> None:
    """Saneless -- SANE scanner to paperless-ngx bridge."""
```

> D-03/D-04: add `@click.version_option(package_name="saneless")` into this stack.
> Note `-v` is already taken by `--verbose`, so the version option must not claim a short
> flag. The `-v` help text here also says *"to the log file and stderr"* — that sentence
> becomes mode-dependent and is one of the doc-truth surfaces the audit touches.

**2. The single `configure_logging` call site — the whole seam** (`:506-556`):

```python
def _load_cli_settings(ctx: click.Context) -> Settings:
    ...
    cached = ctx.obj.get("settings")
    if isinstance(cached, Settings):
        return cached

    settings = load_settings(ctx.obj.get("config_path"))
    validate_settings_dirs(settings)
    attached = configure_logging(
        settings.output.log_file,
        settings.output.log_level,
        settings.output.log_max_bytes,
        settings.output.log_backup_count,
        verbose=bool(ctx.obj.get("verbose")),
    )
    ctx.obj["logging_configured"] = True
    ctx.obj["log_file"] = settings.output.log_file if attached else None

    # WR-05: emitted only now, once the log file handler exists to receive it.
    warn_on_legacy_duplex_sources(settings)
    # CFG-11: which file and which environment keys, names only, once.
    log_config_sources(settings)

    ctx.obj["settings"] = settings
    return settings
```

> **This is the only place `configure_logging` is called in the whole codebase.** Add a
> keyword-only `stream_logs: bool = False` to `_load_cli_settings` and pass it through.
> The `ctx.obj["log_file"] = … if attached else None` line needs **no edit** — a stream
> mode that returns `False` makes it `None`, satisfying the discretion constraint that
> nothing prints `"Full details in …"`. The two calls after it
> (`warn_on_legacy_duplex_sources`, `log_config_sources`) keep working because a handler
> is attached either way. The docstring's paragraph about `log_file` being recorded "only
> if the file handler really attached" must gain the mode sentence.

**3. `serve` — the one caller that opts in** (`:848-920`). Two touch points:

```python
    require_sane()
    settings = _load_cli_settings(ctx)          # → _load_cli_settings(ctx, stream_logs=True)
```

and the message at `:915-919` (Claude's discretion, D-36 makes it worth revisiting):

```python
        msg = (
            f"The web server could not start on {actual_host}:{actual_port} "
            f"(uvicorn exit status {exc.code}); the cause is in the log"
        )
```

> `tests/test_cli.py::test_serve_uvicorn_startup_failure_exits_2_not_3` asserts this
> line — read it before editing the string.

**The `uvicorn.run` call needs no change** (`:904-911`) — `log_config=None` already means
uvicorn attaches no handlers and propagates to saneless's root handlers, so D-37 costs
zero code:

```python
        uvicorn.run(
            app,
            host=actual_host,
            port=actual_port,
            log_config=None,
            log_level=settings.output.log_level.lower(),
            access_log=True,
        )
```

**Every other command is textually unchanged** (D-34 is literal): `scan`, `devices`,
`jobs`, `doctor`, `auto-profiles` keep calling `_load_cli_settings(ctx)` positionally.

---

### `.github/workflows/ci.yml` (config/workflow) — THE workflow style canon

**Analog:** itself. `release.yml` and `docs.yml` are brought up to this file's conventions.

**Header comment recording SHA provenance** (`ci.yml:1`) — every workflow this phase
touches must carry an equivalent line with the new resolution date:

```yaml
# SHAs resolved 2026-09-09 via gh api; comments must carry the full version.
name: CI
```

**SHA-pin convention** — `uses: owner/repo@<40-hex> # vX.Y.Z`, full version in the comment:

```yaml
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
```

**Job shape** — `name:`, `runs-on: ubuntu-latest`, `timeout-minutes:`, then checkout →
apt libsane-dev → setup-uv → `uv sync --locked` → the checks:

```yaml
  lint:
    name: lint
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Install SANE development headers
        run: |
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends libsane-dev
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
      - run: uv sync --locked
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run ty check
      - run: uv run pyrefly check src tests
```

> **D-14's zizmor step is a seventh `- run: uv run zizmor .` line at the bottom of this
> exact list** — same shape as its four neighbours, no `continue-on-error`, no
> `--min-severity`, no `|| true` (exit 14 on findings, exit 0 clean).

**Prose comments explaining non-obvious job choices** (`ci.yml:44-47, 52-55`) — this repo
comments its workflows heavily; the new `permissions:` blocks, `persist-credentials:
false`, and the `workflow_call` trigger each deserve a sentence:

```yaml
      # Drives the real SANE `test` backend. It ships with the SANE runtime
      # pulled in transitively by the headers installed above, so this step
      # needs no extra apt package.
      - run: uv run pytest -m sane_hardware
```

**Existing top-level blocks to preserve** (`:6-14`):

```yaml
on:
  push:
    branches: [master]
  pull_request:

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

> Note `branches: [master]` — `docs.yml` currently says `main`, which is why the docs site
> has never deployed. `ci.yml` is the file that has it right.

---

### `.github/workflows/release.yml` and `docs.yml` (config/workflow)

**Analog:** `.github/workflows/ci.yml` (above). Both files are currently pre-Phase-20 and
use floating major tags. Current state, for the diff:

```yaml
# release.yml — every `uses:` is a floating tag, no permissions on `test`
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v7
      - run: uv sync                      # unlocked
      - run: uv run pytest                # collects browser tests, no Chromium
                                          # and no libsane-dev → python-sane cannot build
```

```yaml
# docs.yml — trigger on a branch that does not exist in this repo
on:
  push:
    branches: [main]
permissions:
  contents: write
      - run: pip install mkdocs-material  # not the version uv.lock pins
      - run: mkdocs gh-deploy --force
```

**The one line in `release.yml` D-23's constraint applies to** (`publish-docker`'s
metadata-action):

```yaml
          tags: |
            type=semver,pattern={{version}}
            type=raw,value=latest        # ← unconditional; would tag an RC as `latest`
```

> **No repo-internal analog exists** for the PyPI-publish job, the Pages-artifact flow, or
> a reusable-workflow call — those come from RESEARCH §4 and §11.1, which contains a
> verified-zizmor-clean set. Copy the *style* from `ci.yml` (SHA pins with version
> comments, per-job `permissions:`, `timeout-minutes:`, explanatory comments) and the
> *content* from RESEARCH.

---

### `.github/dependabot.yml` (config, declarative)

**Analog:** itself. Current file in full:

```yaml
# Set update schedule for GitHub Actions
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
```

> D-27 adds a `docker` entry. Per RESEARCH Pitfall 11 **both** entries need a `cooldown:`
> block or zizmor raises `dependabot-cooldown`, failing the new lint step. Keep the
> existing expanded `schedule:` mapping style (the research excerpt uses flow style
> `{ interval: "weekly" }` — prefer the file's existing block style for consistency).

---

### `.pre-commit-config.yaml` (config, hook pipeline)

**Analog:** the `repo: local` block at `.pre-commit-config.yaml:93`, whose six hooks all
use `language: system` + `uv run …`.

**Commit-stage hook shape** (`:104-110`) — D-14's zizmor hook is this, with a different
`entry`:

```yaml
      - id: ty-checker
        name: ty type checker (src)
        entry: uv run ty check src
        language: system
        pass_filenames: false
        always_run: true
```

**Push/merge-stage hook shape** (`:143-150`) — use this variant if the planner adds the
pytest hook that D-08's (corrected) third enforcement point would need:

```yaml
      - id: ruff-check-full
        name: ruff (no fix)
        entry: uv run ruff check --no-fix .
        language: system
        pass_filenames: false
        always_run: true
        stages: [pre-merge-commit, pre-push]
```

**The block-level convention comment** (`:94-102`) explains why no hook carries `types:`;
a new hook that breaks that rule needs its own justification:

```yaml
      # No `types:` on any hook in this section, deliberately. Each one pairs
      # `always_run: true` with `pass_filenames: false`, so it runs whatever
      # changed and is handed no filenames -- a `types:` filter would have
      # nothing to act on ...
```

Top-of-file context: `default_install_hook_types: [pre-commit, pre-merge-commit,
pre-push]` (`:4`) and `default_stages: [pre-commit]` (`:23`).

---

### `Dockerfile` (config/build)

**Analog:** itself. The file already establishes "bake the right thing into the image and
explain why in a comment above it" — the D-25/D-28 additions land in the same style.

**Current runtime stage, lines 8-33:**

```dockerfile
# Stage 1: Build
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY . .
RUN uv build --wheel --out-dir /dist
...
# The container runs as root, so an unset data_dir would resolve to
# /root/.local/state/saneless -- covered by no volume and destroyed on every
# container recreation, taking preserved scans in failed/ with it. Baking the
# path into the image makes `docker run -v ...:/var/lib/saneless` correct
# without the operator having to know this variable exists.
ENV SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless
# Declared so a bare `docker run` with no -v gets an anonymous volume instead
# of writing the job database and preserved scans into the container's
# writable layer, where an image commit or export could carry them off-host.
VOLUME ["/var/lib/saneless"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1
ENTRYPOINT ["saneless"]
CMD ["serve"]
```

> Three things to note for the edit. (a) `COPY --from=ghcr.io/astral-sh/uv:latest` is the
> mutable tag D-27 kills and the line Dependabot cannot see — promote to
> `FROM … AS uv` (RESEARCH §6.3, Pitfall 5). (b) `COPY . .` at line 12 is what D-29
> replaces with explicit `COPY pyproject.toml uv.lock README.md LICENSE ./` +
> `COPY src ./src`. (c) The `ENV` comment above says *"The container runs as root"* — that
> sentence becomes false under D-25 and must be rewritten, not just appended to. The
> `mkdir` + `chown` must go **strictly before** `VOLUME` (Pitfall 4).

---

### `docker-compose.yml` (config, declarative)

**Analog:** itself. The file's whole personality is "every non-obvious line carries a
multi-sentence rationale comment above it". D-26's commented `# user: "${UID}:${GID}"`
must match:

```yaml
    volumes:
      # ./config holds config.toml. Mount the directory, not the file, and keep
      # it writable: saneless rewrites profiles by writing a temp file beside
      # config.toml and renaming it over the original, and a single-file bind
      # mount makes that rename fail with EBUSY.
      ...
      - ./config:/etc/saneless
```

```yaml
      # The paperless-ngx connection is commented out ON PURPOSE.
      # ./config/config.toml is the one place for the URL and the token. A
      # variable set here OVERRIDES that file silently ...
      # - SANELESS_PAPERLESS__URL=http://paperless:8000
```

**The two DLVR-01 lines in this file** are `:15` (`image: ghcr.io/kris-knigga/saneless:latest`)
and `:25` (`# See: https://github.com/kris-knigga/saneless#configuration`).

---

### `saneless.toml.example` (config example)

**Analog:** itself, lines 11-13 — the file already shows both a live setting and the
commented-with-guidance form D-32 wants:

```toml
[output]
web_port = 8081          # change if 8080 is taken
# tmp_dir = "/tmp/saneless"
# log_level = "INFO"     # DEBUG, INFO, WARNING, ERROR or CRITICAL
```

> D-32: `web_port = 8081` becomes `# web_port = 8080  # bare metal only; in Docker remap
> with -p`, i.e. it joins the commented pair below it. Note `source = "Flatbed"` at `:16`
> is already the correct SANE spelling — it is the counter-example that makes README row
> 17 provably false.

---

### `.gitignore` (config, declarative)

**Analog:** itself. The tail of the file already uses bare-directory entries with no
comment; D-33's replacement joins them.

```
# .gitignore:206-207
# mkdocs documentation
/site

# .gitignore:307-312
*.png            ← DLVR-09 deletes this line

.serena/
.playwright-mcp/

saneless.toml
```

> Per RESEARCH §6.5 (`git check-ignore -v` executed on every candidate), the **only** path
> that depends on the blanket glob is `test-results/*.png` — pytest-playwright's default
> artifact directory, which also holds `.webm` and `.zip`. The replacement is
> `/test-results/` (the directory), matching the `/site` style above. No tracked `.png`
> exists anywhere in the repo.

---

### `pyproject.toml` (config, declarative)

**Analog:** itself. Current `[project]` block, lines 1-44:

```toml
[project]
name = "saneless"
version = "0.1.0"
readme = "README.md"
license = {text = "MIT"}          ← D-05: → license = "MIT" + license-files = ["LICENSE"]
classifiers = [
    "Development Status :: 4 - Beta",              ← D-02: → "3 - Alpha"
    ...
    "License :: OSI Approved :: MIT License",      ← D-05: removed in the same change
    ...
]
[project.urls]
Homepage = "https://github.com/kris-knigga/saneless"    ← :42-44, DLVR-01
```

**Dev dependency list** (`[dependency-groups] dev`) — zizmor is appended here:

```toml
[dependency-groups]
dev = [
    "httpx>=0.28.1",
    "playwright>=1.58.0",
    ...
    "pytest-timeout>=2.4.0",
]
```

> The list is unsorted (grouped roughly by arrival). Append rather than re-sorting.
> `uv add --dev zizmor` does this and regenerates `uv.lock` — do not hand-edit either.
> Per D-05 AMENDED: nothing in the toolchain errors if the license classifier is left
> behind, so its **absence needs a static assertion** in `tests/test_deployment_config.py`
> using the `_read` + `not in text` shape.

---

### `docs/getting-started/which-setup.md` (NEW — documentation, decision list)

**Analog:** `docs/getting-started/quick-start.md` for the tabbed copy-paste blocks;
`docs/how-to/scanner-host-discovery.md:13` for the canonical USB sentence this page must
agree with.

**Page opening + prerequisite-list convention** (`docs/getting-started/quick-start.md:1-9`):

```markdown
# Quick Start

Get from zero to your first scanned document in paperless-ngx in under five minutes.

## Prerequisites

- A **SANE-compatible scanner** with `saned` running on the machine that has the scanner attached
- A **running paperless-ngx instance** with an API token (generate one under Settings > API Tokens)
- **Docker** (recommended) or **Python 3.14**
```

**Tabbed alternative-install blocks** (`:12-30`) — `pymdownx.tabbed` with
`alternate_style: true` is already enabled in `mkdocs.yml:32-33`:

```markdown
=== "Docker"

    Run the saneless container, pointing it at your scanner host:

    ```bash
    docker run -p 8080:8080 \
      -v ./config:/etc/saneless \
      -e SANELESS_SCANNER__HOST=192.168.1.50 \
      ghcr.io/kris-knigga/saneless:latest
    ```

    !!! tip "Network scanners"
        The `SANELESS_SCANNER__HOST` environment variable tells saneless where to find
        your scanner over the network. ...

=== "Bare metal"
```

> Two traps in this very excerpt that the phase fixes: `-v ./config:…` is **row 31**
> (needs `$(pwd)/config`), and `ghcr.io/kris-knigga/…` is **row 19**. The new page must
> not reproduce either. RESEARCH §10 supplies the three shapes and their exact YAML.

**Nav insertion point** (`mkdocs.yml:38-43`):

```yaml
nav:
  - Home: index.md
  - Getting Started:
    - Quick Start: getting-started/quick-start.md
    - First CLI Scan: getting-started/first-cli-scan.md
    - First Web UI Scan: getting-started/first-web-ui-scan.md
```

---

### Documentation corrections (rows 16-18, 21, 29-32)

**Analog:** the corrected rows themselves — Phases 21-30 fixed 23 of the 34, in place, in
the same file, without ceremony. Two concrete in-repo examples of "what a corrected row
looks like":

- **Row 30's sibling, already corrected** — `docs/how-to/cli-scripting.md:49` gained
  `+00:00` on `created_at` while `:45` still shows `"id": "a1b2c3d4"`. The corrected
  neighbour is the template.
- **Row 21's counter-example** — `saneless.toml.example:16` already uses `"Flatbed"`
  while `README.md:63` uses `"flatbed"` (row 17). The correct value already exists in the
  repo.

**The four USB statements** (row 29) and their D-48 dispositions — `scanner-host-
discovery.md:13` is the **canonical** sentence the other three are brought to agree with:

```
docs/getting-started/first-cli-scan.md:9    → rewrite
docs/getting-started/first-cli-scan.md:67   → rewrite (container-scoped)
docs/how-to/install-bare-metal.md:9         → already correct, no edit
docs/reference/configuration.md:38          → add the Docker caveat
docs/reference/docker.md:154-170            → DELETE the whole "USB Scanner Access" section
docs/how-to/scanner-host-discovery.md:13    → canonical, no edit
```

Section to delete verbatim (`docs/reference/docker.md:154-170`):

```markdown
## USB Scanner Access

For USB-connected scanners managed by a local `saned`, pass the USB bus:

```yaml
services:
  saneless:
    image: ghcr.io/kris-knigga/saneless:latest
    devices:
      - /dev/bus/usb:/dev/bus/usb
    ...
```

This is **not** required for network scanners. Use `SANELESS_SCANNER__HOST` instead.
```

**Reference-table row convention** (`docs/reference/docker.md:105-113`) — the shape
row 21's `SANELESS_OUTPUT__WEB_PORT` caveat joins, and the style the log-key mode-scope
note (D-39) should use in `configuration.md`:

```markdown
| Variable | Typical Value | Purpose |
|----------|---------------|---------|
| `SANELESS_OUTPUT__WEB_PORT` | `8080` | Override web server port |
| `SANELESS_OUTPUT__DATA_DIR` | `/var/lib/saneless` | Durable state directory. **Already set by the image** -- override it only if you mount the volume somewhere else |
| `TZ` | `America/Chicago` | Standard container variable, **not** a saneless setting. ... |
```

> The `DATA_DIR` and `TZ` rows show the house style for a caveat: bold the surprising
> part, then one sentence of consequence. Copy it for "`web_port` is a bare-metal setting;
> in Docker the port is fixed at 8080 — remap on the host with `-p 8888:8080`" and for
> "`log_file` / `log_max_bytes` / `log_backup_count` apply to one-shot CLI commands only;
> `serve` streams to stderr".

**README's five dead deep links** (`README.md:72-77`) — row 18 and row 19 overlap here:

```markdown
Full documentation is available at **[saneless.github.io](https://kris-knigga.github.io/saneless/)**.

- [Scan Your First Document](https://kris-knigga.github.io/saneless/tutorials/scan-your-first-document/) -- step-by-step tutorial
- [How-To Guides](https://kris-knigga.github.io/saneless/how-to/install-bare-metal/) ...
```

> Row 18 is the `tutorials/scan-your-first-document/` path, which has no corresponding
> file — the real page is `getting-started/first-cli-scan.md`. D-45 assertion 3
> generalises: assert every `<owner>.github.io/saneless/<path>/` link in `README.md` maps
> to an existing `docs/<path>.md`. Same offender-list shape as above.

---

### `.planning/phases/31-…/31-AUDIT.md` (NEW — planning artifact, row-disposition table)

**Analog:** `.planning/v1.0-MILESTONE-AUDIT.md`. It is the repo's one prior "audit
artifact": YAML frontmatter carrying machine-readable status, then a per-row
cross-reference table.

**Frontmatter shape** (`.planning/v1.0-MILESTONE-AUDIT.md:1-35`):

```yaml
---
milestone: v1.0
audited: 2026-03-21T23:00:00Z
status: passed
scores:
  requirements: 49/49
gaps:
  requirements: []
---
```

**Row table shape** (`:47-52`) — one row per item, a column per evidence source, a bolded
final disposition:

```markdown
| REQ-ID | REQUIREMENTS.md | VERIFICATION.md | SUMMARY Frontmatter | Final Status |
|--------|----------------|-----------------|---------------------|--------------|
| SCAN-01 | [x] Complete | Phase 1: SATISFIED | 01-02 | **satisfied** |
| SCAN-02 | [x] Complete | Phase 1: SATISFIED | 01-02 | **satisfied** (config-pin; web interactive deferred) |
```

**Source-table shape to mirror** — review §8's own table
(`.planning/reviews/2026-09-09-code-review.md:982-1027`), so row numbers line up
one-to-one:

```markdown
| # | Where | Claim | Reality |
|---|---|---|---|
| 1 | `docs/how-to/set-up-adf-duplex.md:52, 69` | "press Enter (CLI)" | No prompt exists; pass B starts immediately (C-02) |
```

> D-42 needs: row number, claim, disposition, evidence (test name **or** `file:line`), and
> — per D-41 — an explicit column or marker saying whether the row is *permanently
> defended by a test* or a *point-in-time verified read*. D-44's ~20 re-checked "correct"
> claims go in a second table with the same columns; RESEARCH §9 already supplies both
> tables' content, re-verified 2026-09-18.

---

## Shared Patterns

### 1. Offender-list failure messages (`file:line`)

**Source:** `tests/test_deployment_config.py:93-106`
**Apply to:** the naming guard, the `.dockerignore` contract, the `.gitignore` contract,
the `web_port` agreement test, the README-link test — every new "nothing anywhere says X"
assertion.

```python
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in <file set>
        for number, line in _numbered(path)
        if <needle test>
    ]
    assert not offenders, "<what is wrong>:\n" + "\n".join(offenders)
```

### 2. Expectations derived from source, never hard-coded

**Source:** `tests/test_deployment_config.py:980-1007` (route decorators),
`:1023-1039` (`WebConfig.model_fields`), `:893-918` (`is_placeholder_token`)
**Apply to:** every audit row whose truth is "a documented value equals a source
constant". Concrete sources available to this phase:

| Expectation | Derive from |
|-------------|-------------|
| the released version | `importlib.metadata.version("saneless")` (never `pyproject.toml`'s string — Pitfall 9) |
| the default scan source spelling | `saneless.config.ProfileConfig.model_fields["source"].default` → `"Flatbed"` (`src/saneless/config.py:362`) |
| the web port | `OutputConfig`'s `web_port` default, compared against `Dockerfile`'s `EXPOSE`/`HEALTHCHECK` and every doc page |
| SANE source-name validity | `saneless.scanner.base.classify_source` / `SourceKind` — the ONLY classification rule in the codebase |
| exit codes (already pinned) | `saneless.vocabulary.ExitCode` |

Each such test carries a docstring sentence naming *why* it is derived, in the house style:
*"…rather than a hard-coded list, so a route added in a later phase cannot ship
undocumented."*

### 3. `S603`-clean subprocess invocation from a test

**Source:** `tests/test_scanner.py:2623-2634`, `tests/test_atomic_write.py:684-717`,
`tests/test_pdf.py:726`
**Apply to:** the `git ls-files` call in the naming guard.

Rules, all four load-bearing: absolute interpreter/binary path as argv[0]; **every** argv
element a string literal; per-run values travel via `cwd=`, `env=` or another keyword;
always `timeout=`. Suppression with `# noqa: S603` is forbidden by CLAUDE.md.

### 4. Rationale comments above every non-obvious line

**Source:** `Dockerfile:19-29`, `docker-compose.yml:16-25` and `:43-53`,
`.pre-commit-config.yaml:94-102`, `tests/test_deployment_config.py:781-786`,
`src/saneless/logging_config.py:104-106`
**Apply to:** the D-11 string-assembly comment, the `.dockerignore` allow-list, the
commented `user:` line in compose, the `permissions:`/`persist-credentials:` additions in
the workflows, the serve-mode formatter choice in `logging_config.py`.

The house form is: *what the reader would otherwise assume* → *why it is wrong* → *what
happens if you change it back*. Comments cite requirement/decision IDs (`D-06`, `CFG-04`,
`T-30-88`, `CR-01`) — new comments should cite this phase's (`DLVR-xx`, `D-xx`, `T-31-xx`).

### 5. No suppression, anywhere

**Source:** `CLAUDE.md` § Code Quality Standards; extended to zizmor by D-15.
**Apply to:** all files. No `# noqa`, no `# type: ignore`, no `# zizmor: ignore`, no
`|| true`, no `continue-on-error`, no rule disabling. Ruff `FLY002` and `S603` both have
clean designed-around solutions documented in RESEARCH §8; use those.

### 6. Google-style docstrings with `Args:` / `Returns:` / `Yields:`

**Source:** `src/saneless/logging_config.py:52-84`, `src/saneless/cli.py:507-533`,
`tests/conftest.py:223-252`
**Apply to:** the new `stream` parameter on `configure_logging`, the `stream_logs`
parameter on `_load_cli_settings`, `_shipped_files()` in the guard, and every new test
helper. Ruff's `D` rules require a docstring on every public module, class and function —
including test functions, which in this repo carry a one-line present-tense contract
statement.

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `.dockerignore` (NEW) | config/build | build-context filter | **This repo has never had one.** There is no prior art for the allow-list syntax, the `*` + `!` re-include ordering, or its comment style. Use RESEARCH §6.4's measured five-entry allow-list verbatim (it was built and the received context enumerated). The test that pins it, however, *does* have an analog — pattern 1 above. |
| `.github/workflows/release.yml` publish jobs + `docs.yml` Pages flow | config/workflow | OIDC publish / Pages artifact | No trusted-publishing, environment-gated, or Pages-artifact job exists anywhere in the repo, and the current `release.yml`/`docs.yml` are pre-Phase-20 files that are themselves the thing being fixed. `ci.yml` supplies the *style*; RESEARCH §4 and §11.1 supply the *content* (a zizmor-clean set, exit 0, executed 2026-09-18). |
| `uv.lock` | lockfile | n/a | Tool output. `uv add --dev zizmor` regenerates it; never hand-edited, no pattern to copy. |

**Partial-analog caveat:** `.planning/v1.0-MILESTONE-AUDIT.md` is the only prior audit
artifact and it audits *requirements against phases*, not *documented claims against
code*. Its frontmatter + row-table structure transfers; its column names do not. D-41's
"permanently defended vs. point-in-time" distinction has no precedent at all and is new
column design.

---

## Sequencing Note for the Planner

`tests/test_deployment_config.py` is simultaneously (a) an offender containing the old
name five times, (b) the home of the Phase-30 test that *requires* the compose file to
still carry the old name, and (c) where the new guard lives. **The rename, the three D-12
deletions, and the guard must land in one commit** — that is the only ordering where the
suite is green at every commit. Commit-stage prek type-checks `src/` only, so a TDD RED
commit is permitted, but the full check at pre-push means the RED/GREEN pair must be
squashed or pushed only after GREEN. (RESEARCH §8, "Sequencing".)

---

## Metadata

**Analog search scope:** `tests/`, `src/saneless/`, `.github/`, `docs/`, `.planning/`,
repo root config files
**Files read for pattern extraction:** `tests/test_deployment_config.py`,
`tests/test_cli.py`, `tests/test_logging.py`, `tests/test_scanner.py`,
`tests/test_atomic_write.py`, `tests/conftest.py`, `src/saneless/logging_config.py`,
`src/saneless/cli.py`, `src/saneless/scanner/base.py`, `src/saneless/auto_profiles.py`,
`.github/workflows/{ci,release,docs}.yml`, `.github/dependabot.yml`,
`.pre-commit-config.yaml`, `Dockerfile`, `docker-compose.yml`, `pyproject.toml`,
`.gitignore`, `mkdocs.yml`, `saneless.toml.example`, `README.md`,
`docs/getting-started/quick-start.md`, `docs/reference/docker.md`,
`.planning/v1.0-MILESTONE-AUDIT.md`, `.planning/reviews/2026-09-09-code-review.md` § 8
**Pattern extraction date:** 2026-09-18
