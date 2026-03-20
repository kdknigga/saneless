# Technology Stack

**Project:** saneless
**Researched:** 2026-03-20

## Recommended Stack

### Core Framework

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| FastAPI | >=0.135.1 | Web framework (API + HTML) | Async-capable, native Pydantic integration, WebSocket support built-in, excellent for API-first design with HTML templating via Jinja2. The PRD already specifies this. | HIGH |
| uvicorn | >=0.42.0 | ASGI server | Standard FastAPI deployment server; lightweight, production-ready. Use `uvicorn[standard]` extra for uvloop + httptools. | HIGH |
| Jinja2 | >=3.1.6 | HTML templating | FastAPI's built-in template engine via `Starlette.templating`. Needed for server-rendered web UI pages. | HIGH |

### Scanner Interface

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| python-sane | >=2.9.2 | SANE scanner access | The only maintained Python binding for libsane. C extension wrapping `_sane.c` against `libsane`. Maintained under python-pillow org by Sandro Mani. Latest release July 2025. | MEDIUM |

**CRITICAL WARNING on python-sane:**
- This is a **C extension** that compiles `_sane.c` against `libsane`. Build requires `libsane-dev` (or equivalent) system package.
- **Python 3.14 compatibility is unverified.** The package has no `python_requires` metadata and ships only as sdist (no wheels). It will need to compile from source. C API changes in Python 3.14 may require patches. Test this early.
- Only 205 commits total, 9 open issues. Low bus-factor project.
- The scanner abstraction layer in the PRD is essential -- it isolates the rest of the codebase from python-sane's fragility.

### Image Processing & PDF

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| Pillow | >=12.1.1 | Image manipulation | Thumbnail generation, empty page detection (luminance/stddev analysis), image format conversion. Confirmed Python 3.14 support with wheels available. | HIGH |
| img2pdf | >=0.6.3 | Lossless PDF assembly | Embeds raster images into PDF without re-compression. This is the right tool -- alternatives like reportlab or fpdf re-encode images, losing quality. | HIGH |

### HTTP Client

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| httpx | >=0.28.1 | Paperless-ngx API client | Async + sync APIs, HTTP/2 support, `multipart/form-data` file uploads native. Use async client in FastAPI route handlers for non-blocking paperless-ngx calls. Prefer over `requests` because httpx has native async and the app is async. | HIGH |

### Configuration & Validation

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| pydantic-settings | >=2.13.1 | Config management | TOML file loading + env var overrides + validation. Install with `[toml]` extra. Validates config at startup with clear error messages. Native Pydantic v2 integration. | HIGH |

### CLI

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| click | >=8.3.1 | CLI commands | `saneless scan`, `saneless devices`, `saneless jobs`. Mature, composable, good help formatting. Typer was considered but adds unnecessary abstraction for 3 simple commands. | HIGH |

### Database

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| sqlite3 | (stdlib) | Job history persistence | Zero-dependency, stdlib, single-file database. Perfect for single-process model with pruning by age + count. No ORM needed -- use raw SQL with context managers. | HIGH |

### Real-time Updates

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| Server-Sent Events | (built-in) | Live scan status to web UI | SSE via FastAPI `StreamingResponse` is simpler than WebSockets for unidirectional server-to-client updates (scan progress, job status). No extra dependency. Falls back gracefully. | HIGH |

**Why SSE over WebSockets:** The UI only needs to receive status updates, not send data back. SSE is simpler, auto-reconnects natively in browsers, works through more proxies, and needs no additional library. Use WebSockets only if bidirectional communication becomes necessary (it will not for v1).

### Dev & Testing

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| pytest | >=9.0.2 | Test framework | Already in scaffolding. | HIGH |
| playwright | >=1.58.0 | E2E browser tests | Already in scaffolding. Web UI testing. | HIGH |
| ruff | >=0.15.5 | Linter + formatter | Already in scaffolding. | HIGH |
| httpx (test client) | >=0.28.1 | API testing | FastAPI's `TestClient` uses httpx under the hood. Use `from starlette.testclient import TestClient`. | HIGH |

## Alternatives Considered

| Category | Recommended | Alternative | Why Not |
|----------|-------------|-------------|---------|
| Web framework | FastAPI | Flask | Flask lacks native async, Pydantic integration, and auto-generated API docs. FastAPI is the modern standard for API-first Python apps. |
| Web framework | FastAPI | Django | Massive overkill for a single-purpose scanner tool. Django's ORM, admin, auth are all unnecessary here. |
| HTTP client | httpx | requests | No native async support. Since the app is async (FastAPI + uvicorn), blocking `requests` calls would need thread pool executors. httpx async is cleaner. |
| HTTP client | httpx | aiohttp | httpx has a simpler API, built-in sync+async, better maintained ecosystem integration with FastAPI. |
| PDF assembly | img2pdf | reportlab | reportlab re-encodes images, losing lossless quality. img2pdf preserves original image data bit-for-bit. |
| PDF assembly | img2pdf | fpdf2 | Same re-encoding issue. img2pdf is purpose-built for lossless image-to-PDF. |
| CLI | click | typer | Typer adds a dependency layer over click for type hints -- unnecessary complexity for 3 commands. click is battle-tested and sufficient. |
| CLI | click | argparse | Verbose, poor help formatting, no composable command groups. click is strictly better for multi-command CLIs. |
| Config | pydantic-settings | dynaconf | pydantic-settings integrates natively with Pydantic models used throughout the app. Dynaconf is framework-agnostic but loses type-safety integration. |
| Database | sqlite3 | SQLAlchemy | ORM is overkill for a single table (jobs) with simple CRUD + pruning. Raw SQL is clearer and has zero dependencies. |
| Real-time | SSE | WebSockets | Unidirectional updates only. SSE is simpler, auto-reconnects, no extra library needed. |
| Task queue | queue.Queue | Celery/rq | Single scanner = single concurrent job. In-process queue with a worker thread is the right complexity level. Redis would be an unnecessary infrastructure dependency. |

## System Dependencies

These are **not** Python packages but are required at the OS/container level:

| Dependency | Purpose | Container Note |
|------------|---------|----------------|
| `libsane` (or `libsane1`) | SANE runtime library | Required. `apt-get install libsane` in Dockerfile. |
| `libsane-dev` (or `libsane-dev`) | SANE headers for python-sane compilation | Required at build time only. Can be removed in multi-stage build. |
| `sane-utils` | `scanimage` CLI for debugging | Optional. Useful for container troubleshooting. |

## Installation

```bash
# Core application dependencies
uv add fastapi uvicorn[standard] jinja2 python-sane img2pdf Pillow httpx pydantic-settings[toml] click

# Dev dependencies (already present in pyproject.toml)
# pytest, playwright, ruff, ty, pyrefly, prek
```

## Version Pinning Strategy

Use `>=` lower bounds in pyproject.toml (library-style), not exact pins. The `uv.lock` file handles reproducible installs. This is already the pattern established in the scaffolding's `dependency-groups`.

## Key Integration Points

### Paperless-ngx REST API

- **Upload:** `POST /api/documents/post_document/` with multipart form (document file + metadata fields: title, tags, correspondent, created, document_type)
- **Auth:** Token authentication via `Authorization: Token <token>` header. Token stored in config, never baked into image.
- **Task polling:** `GET /api/tasks/?task_id={uuid}` -- returns consumption state and created document ID on success.
- **Metadata endpoints:** `GET /api/tags/`, `GET /api/correspondents/` for dropdown population with TTL cache.
- **Connection test:** Hit `/api/` root -- distinguish between connection refused, auth failure, and success.

### SANE/saned Protocol

- python-sane connects to saned network daemon (not direct USB)
- Device discovery: `sane.get_devices()` returns list of `(name, vendor, model, type)` tuples
- Device open: `sane.open(device_name)` returns `SaneDev` object
- Scan: `device.snap()` returns PIL Image, or `device.multi_scan()` for ADF
- Options set via `device.resolution = 300`, `device.mode = 'Color'`, `device.source = 'ADF'`

## Sources

- FastAPI 0.135.1: https://pypi.org/project/fastapi/ (verified via PyPI, March 2026)
- uvicorn 0.42.0: https://pypi.org/project/uvicorn/ (verified via PyPI, March 2026)
- python-sane 2.9.2: https://pypi.org/simple/python-sane/ + https://github.com/python-pillow/Sane (verified July 2025 release)
- img2pdf 0.6.3: https://pypi.org/project/img2pdf/ (verified via PyPI, November 2025)
- Pillow 12.1.1: https://pypi.org/project/Pillow/ (verified via PyPI, February 2026, Python 3.14 wheels confirmed)
- httpx 0.28.1: https://pypi.org/project/httpx/ (verified via PyPI, December 2024)
- pydantic-settings 2.13.1: https://pypi.org/project/pydantic-settings/ (verified via PyPI, February 2026, TOML extra confirmed)
- click 8.3.1: https://pypi.org/project/click/ (verified via PyPI, November 2025)
- Jinja2 3.1.6: https://pypi.org/project/Jinja2/ (verified via PyPI, March 2025)
- websockets 16.0: https://pypi.org/project/websockets/ (verified via PyPI, January 2026 -- noted but not recommended)
- Paperless-ngx API: https://github.com/paperless-ngx/paperless-ngx/blob/main/docs/api.md (verified March 2026)
