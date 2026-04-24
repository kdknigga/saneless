# Phase 1: Core Pipeline - Research

**Researched:** 2026-03-20
**Domain:** CLI-driven flatbed scan pipeline (SANE scanner -> PDF -> paperless-ngx)
**Confidence:** HIGH

## Summary

Phase 1 builds the foundational pipeline: configuration loading (pydantic-settings with TOML), scanner abstraction layer (ABC wrapping python-sane), single-page flatbed scanning, PDF assembly via img2pdf, paperless-ngx REST API upload with task polling, and CLI commands (`saneless scan`, `saneless devices`). All components are well-understood with verified library versions.

The primary technical risk is python-sane's Python 3.14 compatibility (C extension, sdist-only). The abstraction layer mitigates this by isolating python-sane behind a clean interface. All other libraries (click 8.3.1, pydantic-settings 2.13.1, img2pdf 0.6.3, httpx 0.28.1, Pillow 12.1.1) are mature with confirmed current versions.

The build order is strictly bottom-up: config first (everything depends on it), then scanner abstraction, then PDF assembly, then paperless-ngx client, then worker thread + job store, then CLI layer on top.

**Primary recommendation:** Build config -> scanner abstraction -> paperless client -> worker pipeline -> CLI, testing each layer independently before integration.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Minimal status lines by default: "Scanning...", "Assembling PDF...", "Uploading to paperless-ngx...", "Done: [title]"
- `-v` flag for verbose/debug output (maps to DEBUG log level)
- Errors printed to stderr with actionable messages, not raw library errors
- Exit codes: 0 success, 1 scan error, 2 config error, 3 paperless error
- `saneless devices` outputs human-readable table (device name, vendor, model, type); `--json` flag for programmatic use
- No progress bar for v1 -- simple text status transitions
- Abstract base class (ABC/Protocol) with methods: `get_devices()`, `get_capabilities(device_id)`, `scan_pages(device_id, settings) -> Iterator[Image]`
- Single `SaneBackend` implementation wrapping python-sane
- Designed for testability -- mock backend possible for unit tests
- Profile source values (`Flatbed`, `ADF`, `ADF Duplex`) mapped to device-reported option values at runtime
- Fail with clear error if device doesn't support requested source
- `saneless devices --capabilities` dumps raw SANE options for debugging
- `sane.init()` called once at startup, never per-scan (fd leak prevention per Pitfall #1)
- Device opened at job start, closed in context manager; `sane.cancel()` before `close()` on all paths
- No progress callbacks to python-sane (segfault prevention per Pitfall #2)
- Fail fast at startup on invalid config: clear parse errors, missing required fields listed
- Invalid profile definitions -> error identifying the bad profile
- Optional fields have sensible defaults
- saned unreachable at startup -> log warning but start anyway; device discovery retried on each scan attempt
- Config file location: `--config` flag for explicit path; fallback search `./saneless.toml`, `~/.config/saneless/config.toml`, `/etc/saneless/config.toml`
- Env vars override any file value (pydantic-settings standard behavior)
- SANELESS_ prefix for env vars
- Retry 3 times with exponential backoff (1s, 2s, 4s) on network errors only; no retry on 4xx
- After retries exhausted, fall back to consume directory if configured
- Task polling: exponential backoff 1s->2s->4s->8s->16s->30s (capped), total timeout 5 minutes (configurable)
- Handle task-not-found on first poll with retry (race condition per Pitfall #8)
- Title required (from `--title` flag or profile default template); tags, correspondent, created date optional
- Created date defaults to scan timestamp (timezone-aware)

### Claude's Discretion
- Exact module/file organization within `src/saneless/`
- SQLite schema details for job persistence
- Exact pydantic model structure for config
- Worker thread implementation details (queue size, shutdown behavior)
- Logging format string and rotation settings
- Temp directory structure within configured tmp_dir

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| SCAN-01 | Discover available SANE devices at startup and on demand | python-sane `get_devices()` API documented; device caching pattern for slow network discovery |
| SCAN-02 | Pin target device by name in config or select interactively | pydantic-settings config model with `scanner.device` field; validated against discovered devices |
| SCAN-03 | Perform a flatbed single-page scan | python-sane `snap()` returns PIL Image; scanner abstraction `scan_pages()` yields single image for flatbed |
| PROF-01 | Define scan profiles in TOML config | pydantic-settings nested model pattern for `[profiles.X]` TOML sections |
| PROF-02 | Default profile must always be present | Pydantic validator on profiles dict; fail at config load if missing |
| PDF-01 | Assemble pages into PDF using img2pdf | img2pdf lossless encoding; write temp files first (seekable requirement) |
| PDF-02 | Temp files cleaned up after success or on error | `tempfile.TemporaryDirectory` as context manager per job |
| PLSS-01 | Upload PDF to paperless-ngx via REST API with metadata | `POST /api/documents/post_document/` multipart form; tags as repeated fields |
| PLSS-02 | Poll task endpoint until terminal state | `GET /api/tasks/?task_id={uuid}` with exponential backoff |
| PLSS-03 | Connection test distinguishing three failure modes | `GET /api/` root endpoint; catch ConnectionError vs 401/403 vs 200 |
| CONF-01 | pydantic-settings reading TOML with env var overrides | `SettingsConfigDict(toml_file=..., env_prefix='SANELESS_', env_nested_delimiter='__')` |
| CONF-02 | All settings configurable (scanner host, device, paperless URL/token, etc.) | Nested pydantic models: ScannerConfig, PaperlessConfig, OutputConfig, ProfileConfig |
| CONF-03 | No hardcoded credentials; token via config or env var | pydantic-settings env override + `SANELESS_PAPERLESS__TOKEN` |
| ARCH-01 | Scanner access behind abstraction layer | ABC with `get_devices()`, `get_capabilities()`, `scan_pages()` |
| ARCH-02 | Background worker thread with queue.Queue | stdlib `threading.Thread` + `queue.Queue`; SQLite for job state |
| ARCH-03 | SANE initialized once at startup | Singleton pattern in SaneBackend; `sane.init()` in `__init__` |
| LOG-01 | Rotating log file at configurable path | `logging.handlers.RotatingFileHandler` with configurable maxBytes/backupCount |
| LOG-02 | Log level configurable | Config field maps to logging constants |
| LOG-04 | Temp files cleaned up on error | `tempfile.TemporaryDirectory` context manager handles cleanup |
| CLI-01 | `saneless scan [--profile] [--title]` triggers scan | click command with options; loads config, runs pipeline synchronously |
| CLI-02 | `saneless devices` lists available SANE devices | click command; calls scanner abstraction `get_devices()` |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| click | 8.3.1 | CLI framework | Composable command groups, clean help formatting, battle-tested for multi-command CLIs |
| pydantic-settings[toml] | 2.13.1 | Config management | TOML + env var override native; Pydantic v2 validation; nested model support |
| python-sane | 2.9.2 | SANE scanner access | Only maintained Python SANE binding; C extension wrapping libsane |
| Pillow | 12.1.1 | Image handling | Python 3.14 wheels confirmed; used by python-sane for scan output |
| img2pdf | 0.6.3 | Lossless PDF assembly | Embeds raster images without re-compression; purpose-built |
| httpx | 0.28.1 | HTTP client for paperless-ngx | Sync + async APIs; native multipart upload; modern requests alternative |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| sqlite3 | stdlib | Job persistence | Job state tracking, history storage |
| logging | stdlib | Structured logging | RotatingFileHandler for log rotation |
| threading | stdlib | Worker thread | Background scan pipeline execution |
| queue | stdlib | Job queue | Thread-safe job coordination |
| tempfile | stdlib | Temp file management | TemporaryDirectory per scan job |
| uuid | stdlib | Job IDs | Unique job identifiers |
| datetime | stdlib | Timestamps | Timezone-aware scan timestamps |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| click | typer | Adds abstraction layer over click; unnecessary for 3 commands |
| click | argparse | Verbose, poor help formatting, no composable groups |
| httpx | requests | No native async; blocking in async context requires thread pool |
| img2pdf | fpdf2/reportlab | Re-encode images, losing lossless quality |
| pydantic-settings | dynaconf | Loses type-safety integration with Pydantic models |
| sqlite3 | SQLAlchemy | ORM overkill for single table with simple CRUD |

**Installation:**
```bash
uv add click pydantic-settings[toml] python-sane img2pdf Pillow httpx
```

**Version verification:** All versions confirmed against PyPI on 2026-03-20. python-sane 2.9.2 is sdist-only (requires libsane-dev at build time).

## Architecture Patterns

### Recommended Project Structure
```
src/saneless/
    __init__.py          # Package init, main() entry point
    cli.py               # Click CLI group and commands (scan, devices)
    config.py            # Pydantic Settings models, config loading, validation
    scanner/
        __init__.py      # Export ScannerBackend ABC, DeviceInfo, ScanSettings
        base.py          # ABC definition: ScannerBackend protocol
        sane_backend.py  # SaneBackend implementation wrapping python-sane
    pipeline.py          # Scan pipeline orchestration (scan -> PDF -> upload)
    pdf.py               # PDF assembly via img2pdf
    paperless.py         # Paperless-ngx REST API client (upload, poll, test)
    worker.py            # Background worker thread + queue.Queue
    job.py               # Job model, state machine, SQLite persistence
    logging_config.py    # Logging setup (RotatingFileHandler, format)
    exceptions.py        # Custom exception hierarchy
```

### Pattern 1: pydantic-settings TOML Configuration with Nested Models
**What:** Use pydantic-settings `SettingsConfigDict` with TOML file loading, env prefix, and nested delimiter.
**When to use:** Config loading at application startup.
**Example:**
```python
# Source: https://docs.pydantic.dev/latest/concepts/pydantic_settings/
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScannerConfig(BaseModel):
    host: str = ""
    device: str = ""


class PaperlessConfig(BaseModel):
    url: str = ""
    token: str = ""
    consume_dir: str = ""


class ProfileConfig(BaseModel):
    source: str = "Flatbed"
    resolution: int = 300
    mode: str = "color"
    default_tags: list[int] = []
    default_correspondent: int | None = None
    default_title_template: str = ""


class OutputConfig(BaseModel):
    tmp_dir: str = "/tmp/saneless"
    log_file: str = "/var/log/saneless/saneless.log"
    log_level: str = "INFO"
    history_retention_days: int = 7
    history_max_rows: int = 500
    paperless_task_timeout: int = 300


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        toml_file=[
            "/etc/saneless/config.toml",
            "~/.config/saneless/config.toml",
            "./saneless.toml",
        ],
        env_prefix="SANELESS_",
        env_nested_delimiter="__",
    )

    scanner: ScannerConfig = ScannerConfig()
    paperless: PaperlessConfig = PaperlessConfig()
    output: OutputConfig = OutputConfig()
    profiles: dict[str, ProfileConfig] = {"default": ProfileConfig()}
```

**Env var mapping:** `SANELESS_SCANNER__HOST=192.168.1.10` maps to `settings.scanner.host`. `SANELESS_PAPERLESS__TOKEN=abc123` maps to `settings.paperless.token`.

**Config file search:** Files listed in order; later files override earlier. `--config` CLI flag should prepend to the list (or replace it entirely).

### Pattern 2: Scanner Abstraction Layer (ABC)
**What:** Abstract base class isolating python-sane behind clean interface.
**When to use:** All scanner operations.
**Example:**
```python
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass

from PIL import Image


@dataclass
class DeviceInfo:
    name: str
    vendor: str
    model: str
    device_type: str


@dataclass
class DeviceCapabilities:
    sources: list[str]
    resolutions: list[int]
    modes: list[str]
    raw_options: list[tuple]  # Raw SANE option tuples


@dataclass
class ScanSettings:
    source: str
    resolution: int
    mode: str


class ScannerBackend(ABC):
    @abstractmethod
    def get_devices(self) -> list[DeviceInfo]:
        """Enumerate available scanning devices."""

    @abstractmethod
    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """Query device capabilities and available options."""

    @abstractmethod
    def scan_pages(
        self, device_id: str, settings: ScanSettings
    ) -> Iterator[Image.Image]:
        """Acquire pages from scanner. Yields PIL Images."""
```

### Pattern 3: Context-Managed Scanner Sessions
**What:** Open device at job start, close on all paths including errors.
**When to use:** Every scanner operation in SaneBackend.
**Example:**
```python
import contextlib
from collections.abc import Iterator

import sane
from PIL import Image


class SaneBackend(ScannerBackend):
    def __init__(self) -> None:
        self._sane_version = sane.init()  # Called ONCE at startup

    @contextlib.contextmanager
    def _open_device(self, device_id: str):
        """Context manager for SANE device lifecycle."""
        dev = sane.open(device_id)
        try:
            yield dev
        finally:
            try:
                dev.cancel()
            except Exception:
                pass
            dev.close()

    def scan_pages(
        self, device_id: str, settings: ScanSettings
    ) -> Iterator[Image.Image]:
        with self._open_device(device_id) as dev:
            # Validate option against device capabilities
            opts = dev.get_options()
            # Set options through validated path
            dev.mode = settings.mode
            dev.resolution = settings.resolution
            if hasattr(dev, "source"):
                dev.source = settings.source
            yield dev.snap(no_cancel=True)
```

### Pattern 4: Paperless-ngx Client with Retry and Fallback
**What:** HTTP client with exponential backoff on network errors, consume dir fallback.
**When to use:** All paperless-ngx API interactions.
**Example:**
```python
import httpx
import time


class PaperlessClient:
    def __init__(self, url: str, token: str, consume_dir: str = "") -> None:
        self._client = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Token {token}"},
            timeout=30.0,
        )
        self._consume_dir = consume_dir

    def upload_document(
        self,
        pdf_path: str,
        title: str,
        tags: list[int] | None = None,
        correspondent: int | None = None,
        created: str | None = None,
    ) -> str:
        """Upload PDF and return task UUID."""
        files = {"document": open(pdf_path, "rb")}
        data = {"title": title}
        if created:
            data["created"] = created
        if correspondent:
            data["correspondent"] = str(correspondent)
        # Tags submitted as repeated form fields
        fields = list(data.items())
        for tag_id in (tags or []):
            fields.append(("tags", str(tag_id)))
        response = self._client.post(
            "/api/documents/post_document/",
            files=files,
            data=fields,
        )
        response.raise_for_status()
        return response.json()  # Returns task UUID string

    def poll_task(
        self, task_id: str, timeout: int = 300
    ) -> dict:
        """Poll task until terminal state with exponential backoff."""
        delay = 1.0
        elapsed = 0.0
        while elapsed < timeout:
            resp = self._client.get(
                "/api/tasks/",
                params={"task_id": task_id},
            )
            if resp.status_code == 200:
                tasks = resp.json()
                if tasks:
                    task = tasks[0] if isinstance(tasks, list) else tasks
                    status = task.get("status")
                    if status in ("SUCCESS", "FAILURE"):
                        return task
            time.sleep(delay)
            elapsed += delay
            delay = min(delay * 2, 30.0)
        return {"status": "TIMEOUT", "task_id": task_id}

    def test_connection(self) -> str:
        """Test paperless-ngx connection. Returns 'connected', 'token_rejected', or 'unreachable'."""
        try:
            resp = self._client.get("/api/")
            if resp.status_code in (401, 403):
                return "token_rejected"
            resp.raise_for_status()
            return "connected"
        except httpx.ConnectError:
            return "unreachable"
```

### Pattern 5: Click CLI with Config Loading
**What:** Click group with scan and devices commands.
**When to use:** CLI entry points.
**Example:**
```python
# Source: https://click.palletsprojects.com/en/stable/commands-and-groups/
import click
import sys


@click.group()
@click.option("--config", "config_path", default=None, help="Path to config file")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug output")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, verbose: bool) -> None:
    """saneless -- SANE scanner to paperless-ngx bridge."""
    ctx.ensure_object(dict)
    try:
        settings = load_settings(config_path)
    except ValidationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(2)
    ctx.obj["settings"] = settings
    ctx.obj["verbose"] = verbose


@cli.command()
@click.option("--profile", default="default", help="Scan profile name")
@click.option("--title", required=True, help="Document title")
@click.pass_context
def scan(ctx: click.Context, profile: str, title: str) -> None:
    """Trigger a flatbed scan and upload to paperless-ngx."""
    settings = ctx.obj["settings"]
    # ... pipeline execution ...


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--capabilities", is_flag=True, help="Show raw SANE options")
@click.pass_context
def devices(ctx: click.Context, as_json: bool, capabilities: bool) -> None:
    """List available SANE scanning devices."""
    settings = ctx.obj["settings"]
    # ... device enumeration ...
```

### Anti-Patterns to Avoid
- **Running python-sane in asyncio event loop:** Blocks entire event loop during scan; always use dedicated thread.
- **Re-initializing SANE per scan:** Causes fd leaks (Pitfall #1); init once at process startup.
- **Using progress callbacks with python-sane:** Causes segfaults (Pitfall #2); never pass callbacks.
- **Setting scanner options without validation:** Typos in option names silently create Python attributes (Pitfall #12); validate against `get_options()`.
- **Storing page images in SQLite:** Database bloat; use temp directory for images, only base64 thumbnail in SQLite.
- **Hardcoding SANE option values:** Option names/values vary between scanner backends (Pitfall #5); always discover at runtime.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Config loading + env override | Custom TOML parser + env reader | pydantic-settings[toml] | Handles TOML + env + validation + nested models + type coercion |
| PDF assembly | Manual PDF byte construction | img2pdf | Lossless encoding, handles color spaces, EXIF, page sizing |
| CLI framework | Custom argparse wrapper | click | Help formatting, command groups, context passing, type conversion |
| HTTP retry logic | Custom retry loop | tenacity or manual (see code example) | Exponential backoff is tricky to get right; but for 3-retry simple case, manual loop is acceptable |
| Log rotation | Custom file rotation | logging.handlers.RotatingFileHandler | stdlib handles rotation, backup count, thread safety |
| Temp file cleanup | Manual try/finally cleanup | tempfile.TemporaryDirectory | Context manager guarantees cleanup on all exit paths |

**Key insight:** The Phase 1 stack is almost entirely well-known libraries with straightforward usage. The only area requiring careful custom code is the scanner abstraction layer and SANE device lifecycle management.

## Common Pitfalls

### Pitfall 1: python-sane FD Leak on Repeated init/exit
**What goes wrong:** Each `sane.init()`/`sane.exit()` cycle leaks file descriptors. Long-running service exhausts fds.
**Why it happens:** Open bug in python-sane (#93).
**How to avoid:** Call `sane.init()` exactly once in `SaneBackend.__init__()`. Never call `sane.exit()` except at shutdown.
**Warning signs:** Monotonically growing fd count in `/proc/self/fd`.

### Pitfall 2: python-sane Segfaults from Progress Callbacks
**What goes wrong:** Wrong callback signature or callback exceptions cause SIGSEGV.
**Why it happens:** C extension doesn't validate callbacks (bugs #81, #103).
**How to avoid:** Never pass progress callbacks to `snap()` or `arr_snap()`. Use `dev.snap(no_cancel=False)` with no progress arg.
**Warning signs:** Exit code 139.

### Pitfall 3: Scanner Option Names Vary Between Backends
**What goes wrong:** Hardcoded option values like `source="ADF"` fail on backends that use different names.
**Why it happens:** SANE standardizes protocol, not option semantics.
**How to avoid:** Enumerate options via `dev.get_options()` at device open. Map user-facing names to discovered values. Validate before setting.
**Warning signs:** Errors setting scan options; scans with wrong settings.

### Pitfall 4: Device Locking on Incomplete Close
**What goes wrong:** If `dev.close()` is skipped due to exception, scanner stays "busy" until saned timeout.
**Why it happens:** Python exceptions bypass cleanup; GC timing is unpredictable.
**How to avoid:** Always use context manager. Call `dev.cancel()` before `dev.close()` on every path.
**Warning signs:** "Device busy" errors on subsequent scans.

### Pitfall 5: paperless-ngx Task Not Found on First Poll
**What goes wrong:** Upload returns task UUID, but polling immediately returns empty/404.
**Why it happens:** Race condition -- task not yet created in paperless-ngx task store.
**How to avoid:** First poll delay of 1s; retry on empty result before declaring failure.
**Warning signs:** Jobs stuck in "uploading" state.

### Pitfall 6: img2pdf Requires Seekable File Input
**What goes wrong:** Passing in-memory bytes or non-seekable streams to img2pdf fails.
**Why it happens:** img2pdf needs to read image headers, then rewind.
**How to avoid:** Always save scanned images to temp files on disk before passing to img2pdf.
**Warning signs:** ValueError from img2pdf about seeking.

### Pitfall 7: Pillow Decompression Bomb for High-DPI Scans
**What goes wrong:** 600 DPI A4 color scan is ~34.8M pixels. 1200 DPI = ~139M pixels. Pillow default limit is 89.5M.
**Why it happens:** Pillow's `DecompressionBombError` safety check.
**How to avoid:** Set `PIL.Image.MAX_IMAGE_PIXELS` to a suitable value (e.g., 200_000_000) at startup.
**Warning signs:** `DecompressionBombError` on high-resolution scans.

### Pitfall 8: pydantic-settings TOML File Not Found
**What goes wrong:** pydantic-settings raises error if specified TOML file doesn't exist.
**Why it happens:** Default behavior is to require the file.
**How to avoid:** Use multiple file paths (list); files that don't exist are silently skipped. Or implement custom settings source that searches fallback paths.
**Warning signs:** Config validation error at startup on fresh install.

### Pitfall 9: Slow Device Discovery Blocking CLI
**What goes wrong:** `sane.get_devices()` with network backends takes 10-30 seconds.
**Why it happens:** Probes all configured net backends and waits for timeouts.
**How to avoid:** For `saneless devices` -- show a "Discovering..." message. For `saneless scan` with a pinned device -- skip discovery, open device directly by name.
**Warning signs:** CLI appears hung for 10+ seconds.

## Code Examples

### Complete Config Loading with CLI Override
```python
# Source: pydantic-settings docs + click docs
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        toml_file="saneless.toml",  # Overridden at runtime
        env_prefix="SANELESS_",
        env_nested_delimiter="__",
    )

    # ... nested models ...

    @field_validator("profiles")
    @classmethod
    def validate_default_profile(cls, v: dict) -> dict:
        if "default" not in v:
            msg = "A 'default' profile must be defined in config"
            raise ValueError(msg)
        return v


def load_settings(config_path: str | None = None) -> Settings:
    """Load settings with config file search path."""
    search_paths = [
        Path("/etc/saneless/config.toml"),
        Path.home() / ".config" / "saneless" / "config.toml",
        Path("./saneless.toml"),
    ]
    if config_path:
        return Settings(_toml_file=Path(config_path))
    # Find first existing config file
    for path in search_paths:
        if path.exists():
            return Settings(_toml_file=path)
    # No config file found -- use defaults + env vars only
    return Settings(_toml_file=None)
```

### python-sane Device Enumeration
```python
# Source: https://github.com/python-pillow/Sane
import sane


def enumerate_devices() -> list[DeviceInfo]:
    """Get available SANE devices."""
    # sane.init() already called once at startup
    raw_devices = sane.get_devices()
    # Returns list of (name, vendor, model, type) tuples
    return [
        DeviceInfo(name=d[0], vendor=d[1], model=d[2], device_type=d[3])
        for d in raw_devices
    ]
```

### Flatbed Single-Page Scan
```python
# Source: python-sane example.py
import sane
from PIL import Image


def flatbed_scan(device_id: str, resolution: int, mode: str) -> Image.Image:
    """Perform a single flatbed scan."""
    dev = sane.open(device_id)
    try:
        dev.resolution = resolution
        dev.mode = mode
        # No progress callback -- segfault risk (Pitfall #2)
        image = dev.snap()
        return image
    finally:
        try:
            dev.cancel()
        except Exception:
            pass
        dev.close()
```

### PDF Assembly with img2pdf
```python
# Source: https://pypi.org/project/img2pdf/
import img2pdf
import tempfile
from pathlib import Path
from PIL import Image


def assemble_pdf(images: list[Image.Image], tmp_dir: Path) -> Path:
    """Assemble PIL Images into a PDF using img2pdf."""
    image_paths = []
    for i, img in enumerate(images):
        img_path = tmp_dir / f"page_{i:04d}.png"
        img.save(str(img_path), format="PNG")
        image_paths.append(str(img_path))

    pdf_path = tmp_dir / "output.pdf"
    with open(pdf_path, "wb") as f:
        f.write(img2pdf.convert(image_paths))
    return pdf_path
```

### paperless-ngx Upload with Tag Submission
```python
# Source: paperless-ngx API docs (GitHub)
import httpx


def upload_to_paperless(
    client: httpx.Client,
    pdf_path: str,
    title: str,
    tags: list[int] | None = None,
    correspondent: int | None = None,
    created: str | None = None,
) -> str:
    """Upload document to paperless-ngx. Returns task UUID."""
    with open(pdf_path, "rb") as f:
        files = {"document": ("scan.pdf", f, "application/pdf")}
        # Build data dict -- tags must be repeated form fields
        data: list[tuple[str, str]] = [("title", title)]
        if created:
            data.append(("created", created))
        if correspondent is not None:
            data.append(("correspondent", str(correspondent)))
        for tag_id in (tags or []):
            data.append(("tags", str(tag_id)))

        resp = client.post(
            "/api/documents/post_document/",
            files=files,
            data=data,
        )
        resp.raise_for_status()
        return resp.json()  # task UUID
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `requests` for HTTP | `httpx` | 2023+ | Native async/sync, HTTP/2, better API |
| `configparser` / `dotenv` | `pydantic-settings` with TOML | 2023+ (Pydantic v2) | Type-safe config with validation |
| `argparse` for CLI | `click` 8.x | Stable since 2022 | Composable groups, better UX |
| Manual PDF construction | `img2pdf` | Stable | Lossless image embedding |
| `tomllib` (stdlib 3.11+) | `pydantic-settings[toml]` | 2024+ | Integrated with validation layer |

**Deprecated/outdated:**
- python-sane `progress` callbacks: Documented but dangerous (segfault risk); do not use.
- `sane.exit()` in long-running processes: Leaks fds; call only at process termination.

## Open Questions

1. **python-sane Python 3.14 Compatibility**
   - What we know: Package ships as sdist only; C extension compiles against libsane. No `python_requires` metadata. Last release July 2025.
   - What's unclear: Whether Python 3.14 C API changes break compilation.
   - Recommendation: Test compilation early in Phase 1 (first task). If broken, evaluate patching or alternative approaches. The abstraction layer isolates impact.

2. **paperless-ngx Task Polling Response Format**
   - What we know: `GET /api/tasks/?task_id={uuid}` returns task state. Docs confirm SUCCESS/FAILURE terminal states.
   - What's unclear: Exact JSON response schema (docs were 403; relying on GitHub source and community discussions).
   - Recommendation: Implement defensively; log full response body during development; handle both list and dict response formats.

3. **pydantic-settings Behavior with Missing TOML Files in List**
   - What we know: Single missing file raises error. Multiple files in list should skip missing ones.
   - What's unclear: Exact behavior when ALL files in the list are missing.
   - Recommendation: Test this early. May need custom settings source class for fallback path searching.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest >= 9.0.2 |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/ -x --tb=short` |
| Full suite command | `uv run pytest tests/ -v` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CONF-01 | TOML config loads with env overrides | unit | `uv run pytest tests/test_config.py -x` | No -- Wave 0 |
| CONF-02 | All settings configurable | unit | `uv run pytest tests/test_config.py -x` | No -- Wave 0 |
| CONF-03 | No hardcoded credentials | unit | `uv run pytest tests/test_config.py::test_env_override -x` | No -- Wave 0 |
| PROF-01 | Profile definitions in TOML | unit | `uv run pytest tests/test_config.py::test_profiles -x` | No -- Wave 0 |
| PROF-02 | Default profile required | unit | `uv run pytest tests/test_config.py::test_default_profile_required -x` | No -- Wave 0 |
| ARCH-01 | Scanner abstraction layer | unit | `uv run pytest tests/test_scanner.py -x` | No -- Wave 0 |
| ARCH-03 | SANE init once | unit | `uv run pytest tests/test_scanner.py::test_init_once -x` | No -- Wave 0 |
| SCAN-01 | Device discovery | unit | `uv run pytest tests/test_scanner.py::test_get_devices -x` | No -- Wave 0 |
| SCAN-02 | Pin device by config | unit | `uv run pytest tests/test_scanner.py::test_device_selection -x` | No -- Wave 0 |
| SCAN-03 | Flatbed scan | unit | `uv run pytest tests/test_scanner.py::test_flatbed_scan -x` | No -- Wave 0 |
| PDF-01 | img2pdf assembly | unit | `uv run pytest tests/test_pdf.py -x` | No -- Wave 0 |
| PDF-02 | Temp file cleanup | unit | `uv run pytest tests/test_pdf.py::test_cleanup -x` | No -- Wave 0 |
| PLSS-01 | Upload to paperless-ngx | unit | `uv run pytest tests/test_paperless.py::test_upload -x` | No -- Wave 0 |
| PLSS-02 | Task polling | unit | `uv run pytest tests/test_paperless.py::test_poll -x` | No -- Wave 0 |
| PLSS-03 | Connection test 3 modes | unit | `uv run pytest tests/test_paperless.py::test_connection -x` | No -- Wave 0 |
| ARCH-02 | Worker thread + queue | unit | `uv run pytest tests/test_worker.py -x` | No -- Wave 0 |
| LOG-01 | Rotating log file | unit | `uv run pytest tests/test_logging.py -x` | No -- Wave 0 |
| LOG-02 | Log level configurable | unit | `uv run pytest tests/test_logging.py::test_log_level -x` | No -- Wave 0 |
| LOG-04 | Temp cleanup on error | unit | `uv run pytest tests/test_pipeline.py::test_cleanup_on_error -x` | No -- Wave 0 |
| CLI-01 | `saneless scan` command | integration | `uv run pytest tests/test_cli.py::test_scan_command -x` | No -- Wave 0 |
| CLI-02 | `saneless devices` command | integration | `uv run pytest tests/test_cli.py::test_devices_command -x` | No -- Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/ -x --tb=short`
- **Per wave merge:** `uv run pytest tests/ -v`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/` directory -- does not exist yet, needs creation
- [ ] `tests/conftest.py` -- shared fixtures (mock scanner backend, temp config files, httpx mock transport)
- [ ] `tests/test_config.py` -- config loading, validation, env overrides
- [ ] `tests/test_scanner.py` -- scanner abstraction, mock backend, device enumeration
- [ ] `tests/test_pdf.py` -- PDF assembly, temp file cleanup
- [ ] `tests/test_paperless.py` -- paperless client, upload, polling, connection test
- [ ] `tests/test_worker.py` -- worker thread, job queue, state transitions
- [ ] `tests/test_pipeline.py` -- end-to-end pipeline with mock scanner
- [ ] `tests/test_cli.py` -- CLI commands via click test runner
- [ ] `tests/test_logging.py` -- log configuration, rotation
- [ ] Framework install: `uv add --dev pytest` (already in dependency-groups)

## Sources

### Primary (HIGH confidence)
- pydantic-settings official docs: https://docs.pydantic.dev/latest/concepts/pydantic_settings/ -- TOML config, env prefix, nested delimiter
- click official docs: https://click.palletsprojects.com/en/stable/commands-and-groups/ -- command groups, context passing
- python-sane GitHub: https://github.com/python-pillow/Sane -- API (sane.py, example.py), known issues
- img2pdf PyPI: https://pypi.org/project/img2pdf/ -- usage, seekable requirement, EXIF pitfalls
- httpx docs: https://www.python-httpx.org/ -- sync client, multipart uploads
- paperless-ngx API (GitHub source): https://github.com/paperless-ngx/paperless-ngx/blob/dev/docs/api.md -- upload endpoint, task polling, auth

### Secondary (MEDIUM confidence)
- paperless-ngx task polling race condition: community discussions on GitHub (#8564, #6431)
- python-sane Python 3.14 compatibility: inferred from sdist-only distribution and C extension nature

### Tertiary (LOW confidence)
- pydantic-settings behavior with all-missing TOML file list: needs validation during implementation

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all versions verified against PyPI 2026-03-20; well-established libraries
- Architecture: HIGH -- patterns from PRD, existing research docs, and validated against similar tools (scanservjs)
- Pitfalls: HIGH -- documented in python-sane issues, confirmed across multiple projects
- Paperless API: MEDIUM -- docs returned 403; validated against GitHub source and community discussions

**Research date:** 2026-03-20
**Valid until:** 2026-04-20 (stable domain, 30-day validity)
