# Codebase Structure

**Analysis Date:** 2026-03-20

## Directory Layout

```
saneless/
├── src/
│   └── saneless/
│       ├── __init__.py              # Package entry point; contains main() CLI dispatch
│       ├── app.py                   # FastAPI application, worker thread setup
│       ├── cli.py                   # CLI command handlers (scan, devices, jobs)
│       ├── config.py                # Pydantic settings, profile definitions
│       ├── models.py                # Job, profile, and API response models
│       ├── constants.py             # Job state constants, error messages
│       ├── scanner/
│       │   ├── __init__.py
│       │   ├── backend.py           # Abstract ScannerBackend interface
│       │   └── sane.py              # SANE implementation using python-sane
│       ├── pipeline/
│       │   ├── __init__.py
│       │   ├── pages.py             # Page processing (empty detection, thumbnail)
│       │   ├── pdf.py               # PDF assembly using img2pdf
│       │   └── worker.py            # Job queue consumer loop
│       ├── paperless/
│       │   ├── __init__.py
│       │   ├── client.py            # HTTP client for paperless-ngx API
│       │   └── models.py            # Paperless API response models
│       ├── db/
│       │   ├── __init__.py
│       │   ├── schema.py            # SQLite schema creation
│       │   └── queries.py           # Job history CRUD operations
│       ├── handlers/
│       │   ├── __init__.py
│       │   ├── scan.py              # POST /api/scan endpoint handler
│       │   ├── jobs.py              # GET /api/jobs/* endpoint handlers
│       │   ├── health.py            # GET /health endpoint
│       │   ├── metadata.py          # GET /api/metadata/* endpoints (tags, correspondents)
│       │   └── cache.py             # POST /api/cache/invalidate endpoint
│       ├── ui/
│       │   ├── __init__.py
│       │   ├── static/
│       │   │   ├── index.html       # Main web UI
│       │   │   ├── styles.css       # Styling
│       │   │   └── app.js           # Client-side logic, polling
│       │   └── templates/           # Jinja2 templates (if used)
│       └── utils/
│           ├── __init__.py
│           ├── logging.py           # Logging setup and handlers
│           ├── validation.py        # Input validation helpers
│           └── errors.py            # Custom exception classes
├── docs/
│   └── PRD.md                       # Product requirements document
├── tests/                           # Test directory (if present)
│   ├── conftest.py                  # Pytest fixtures and configuration
│   ├── test_*.py                    # Test modules
│   └── fixtures/
│       └── sample_images/           # Sample test images
├── pyproject.toml                   # Project metadata, dependencies, tool config
├── uv.lock                          # Lock file for uv dependency manager
├── .pre-commit-config.yaml          # Pre-commit hooks (ruff, ty, pyrefly)
├── .python-version                  # Python version for pyenv/uv
├── .gitignore                       # VCS ignore rules
├── LICENSE                          # License file
└── README.md                        # Project readme
```

## Directory Purposes

**`src/saneless/`:**
- Purpose: Main Python package containing all application code
- Contains: Core logic, web layer, worker thread, scanner abstraction, database, API handlers
- Key files: `__init__.py` (entry point), `app.py` (FastAPI setup), `config.py` (settings)

**`src/saneless/scanner/`:**
- Purpose: Scanner abstraction layer for device enumeration and page acquisition
- Contains: Abstract interface and concrete SANE implementation
- Key files: `backend.py` (interface), `sane.py` (implementation)

**`src/saneless/pipeline/`:**
- Purpose: Job processing pipeline (page acquisition, assembly, ingestion)
- Contains: Empty page detection, thumbnail generation, PDF assembly, worker thread loop
- Key files: `pages.py` (image processing), `pdf.py` (PDF creation), `worker.py` (job queue consumer)

**`src/saneless/paperless/`:**
- Purpose: Paperless-ngx integration via REST API
- Contains: HTTP client, authentication, document upload, task polling, response models
- Key files: `client.py` (API client), `models.py` (request/response types)

**`src/saneless/db/`:**
- Purpose: SQLite database for job history and state persistence
- Contains: Schema definition, job record CRUD operations
- Key files: `schema.py` (table definitions), `queries.py` (query methods)

**`src/saneless/handlers/`:**
- Purpose: FastAPI endpoint handlers for all REST API routes
- Contains: Request validation, response formatting, status polling
- Key files: `scan.py` (job submission), `jobs.py` (status retrieval), `metadata.py` (tag/correspondent lists)

**`src/saneless/ui/`:**
- Purpose: Web UI assets and templates
- Contains: HTML, CSS, JavaScript for browser UI
- Key files: `static/index.html` (main page), `static/app.js` (client logic)

**`src/saneless/utils/`:**
- Purpose: Shared utility functions and custom exceptions
- Contains: Logging setup, validation helpers, error classes
- Key files: `logging.py` (log configuration), `errors.py` (exception definitions)

**`docs/`:**
- Purpose: Project documentation
- Contains: Product requirements, deployment guides, architecture diagrams
- Key files: `PRD.md` (requirements)

**`tests/`:**
- Purpose: Test suite for unit, integration, and end-to-end tests
- Contains: Test modules, fixtures, sample data
- Key files: `conftest.py` (pytest setup), `test_*.py` (test modules)

## Key File Locations

**Entry Points:**
- `src/saneless/__init__.py`: Package entry point; `main()` function dispatches to CLI or web app based on arguments
- `src/saneless/app.py`: FastAPI application factory; creates app, sets up routes, starts worker thread

**Configuration:**
- `pyproject.toml`: Project metadata, dependencies, tool configurations (ruff, pytest, ty, pyrefly)
- `src/saneless/config.py`: Pydantic settings models for TOML + env var configuration

**Core Logic:**
- `src/saneless/models.py`: Job, Profile, and API response Pydantic models
- `src/saneless/scanner/backend.py`: Abstract interface for scanner backends
- `src/saneless/scanner/sane.py`: Concrete SANE implementation
- `src/saneless/pipeline/worker.py`: Job queue consumer loop
- `src/saneless/paperless/client.py`: HTTP client for paperless-ngx API

**Testing:**
- `tests/conftest.py`: Pytest fixtures and shared configuration
- `tests/test_*.py`: Test modules organized by feature

## Naming Conventions

**Files:**
- Core modules: `snake_case.py` (e.g., `app.py`, `cli.py`, `config.py`)
- Test files: `test_*.py` or `*_test.py` (e.g., `test_scanner.py`)
- Package init: `__init__.py` in each directory

**Directories:**
- Feature packages: `snake_case/` (e.g., `scanner/`, `pipeline/`, `paperless/`)
- Utility directories: `snake_case/` (e.g., `handlers/`, `utils/`, `db/`)
- Static assets: `static/` for web files

**Functions/Classes:**
- Classes: `PascalCase` (e.g., `ScannerBackend`, `JobModel`, `PaperlessClient`)
- Functions: `snake_case` (e.g., `get_devices()`, `assemble_pdf()`)
- Constants: `UPPER_SNAKE_CASE` (e.g., `JOB_STATE_SCANNING`, `MAX_RETRIES`)

**Database/Files:**
- Database file: `saneless.db` (in configurable directory, default `/var/lib/saneless/`)
- Log file: `saneless.log` (in configurable directory, default `/var/log/saneless/`)
- Temporary files: `tmp_dir/` (configurable, default `/tmp/saneless/`)

## Where to Add New Code

**New Feature (e.g., new scan mode):**
- Primary code: `src/saneless/scanner/backend.py` (add interface method), `src/saneless/scanner/sane.py` (implement)
- Pipeline updates: `src/saneless/pipeline/pages.py` if page processing differs
- Config: `src/saneless/config.py` add Profile field
- Tests: `tests/test_scanner.py`, `tests/test_pipeline.py`

**New Component/Module (e.g., new metadata type):**
- Implementation: Create new module in appropriate subdirectory (e.g., `src/saneless/handlers/new_handler.py`)
- Models: Add Pydantic models to `src/saneless/models.py`
- Tests: Create `tests/test_new_handler.py`
- Integration: Register routes/handlers in `src/saneless/app.py`

**Utilities:**
- Shared helpers: `src/saneless/utils/helpers.py` or subsection of existing util module
- Custom exceptions: `src/saneless/utils/errors.py`
- Logging/debugging: `src/saneless/utils/logging.py`

**Web UI Elements:**
- New HTML: `src/saneless/ui/static/index.html` (extend existing)
- New CSS: `src/saneless/ui/static/styles.css` (extend existing)
- New JavaScript: `src/saneless/ui/static/app.js` (extend existing)
- API endpoints: `src/saneless/handlers/` (new handler module or extend existing)

**Database Queries:**
- Schema changes: `src/saneless/db/schema.py`
- New CRUD operations: `src/saneless/db/queries.py` (add methods to Query class)
- Migrations: Run in `src/saneless/db/schema.py` on app startup (schema-first approach)

## Special Directories

**`.planning/codebase/`:**
- Purpose: GSD (Get Shit Done) planning documents
- Generated: Yes (created by `/gsd:map-codebase`)
- Committed: Yes (read-only in normal development; updated only by GSD orchestrator)
- Contents: ARCHITECTURE.md, STRUCTURE.md, CONVENTIONS.md, TESTING.md, CONCERNS.md

**`.venv/`:**
- Purpose: Python virtual environment created by `uv`
- Generated: Yes
- Committed: No (in `.gitignore`)

**`.serena/`:**
- Purpose: GSD orchestrator cache and memory
- Generated: Yes (created by Serena GSD system)
- Committed: No (in `.gitignore`)

**`dist/`, `build/`, `.egg-info/`:**
- Purpose: Python package build artifacts
- Generated: Yes (created by `uv build` or `pip install -e .`)
- Committed: No (in `.gitignore`)

---

*Structure analysis: 2026-03-20*
