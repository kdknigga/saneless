# Technology Stack

**Analysis Date:** 2026-03-20

## Languages

**Primary:**
- Python 3.14 - Primary language for the project

## Runtime

**Environment:**
- Python 3.14 - Specified in `.python-version`

**Package Manager:**
- UV - Python package manager
- Lockfile: `uv.lock` (present, locked dependencies)

## Frameworks

**Core:**
- None detected - Minimal application structure

**Testing:**
- Pytest 9.0.2 - Test runner configured in `pyproject.toml`
- Playwright 1.58.0 - Browser automation testing library

**Build/Dev:**
- Ruff 0.15.5 - Code formatter and linter
- UV Build 0.10.3+ - Build backend for packaging

## Key Dependencies

**Critical:**
- Playwright 1.58.0 - Browser automation and testing library with comprehensive multi-platform support

**Development:**
- Pytest 9.0.2 - Testing framework
- Ruff 0.15.5 - Python linter and code formatter
- prek 0.3.6 - Development tool
- ty 0.0.21 - Type checking companion tool
- pyrefly 0.55.0 - Type checking and analysis tool
- pygments - Syntax highlighting library
- typing-extensions - Backported typing features
- pluggy 1.6.0 - Plugin system for pytest
- packaging 26.0 - Package version parsing utilities
- iniconfig 2.3.0 - INI file parsing for configuration
- colorama 0.4.6 - Cross-platform colored terminal text
- greenlet 3.3.2 - Lightweight concurrency primitive
- pyee - Event emitter library (required by Playwright)

## Configuration

**Environment:**
- Configuration stored in `pyproject.toml`
- No `.env` file required (none detected in project)

**Build:**
- `pyproject.toml` - Primary project configuration
  - Package name: `saneless`
  - Version: `0.1.0`
  - Entry point: `saneless = "saneless:main"`
  - Python requirement: `>=3.14`

**Code Quality:**
- `pyproject.toml` `[tool.ruff]` section:
  - Line length: 88 characters
  - Target version: Python 3.14
  - Quote style: Double quotes
  - Extensive lint rules enabled: `["E", "F", "B", "G", "W", "YTT", "ANN", "ASYNC", "S", "FBT", "A", "COM", "C4", "EM", "ICN", "LOG", "PIE", "Q", "RSE", "RET", "SIM", "TID", "TCH", "ARG", "PTH", "FLY", "PERF", "FURB", "RUF", "UP", "D", "I", "PL", "PT", "DTZ", "T20"]`
  - Ignored rules: `["E501", "D203", "D212", "W505", "PLR2004", "COM812"]`

**Testing:**
- `pyproject.toml` `[tool.pytest.ini_options]` section:
  - Test path: `tests` directory
  - Configuration: Minimal pytest setup

## Development Tools

**Pre-commit Hooks:**
- `.pre-commit-config.yaml` configured with:
  - Standard file checks (JSON, YAML, TOML, XML validation)
  - AWS credential detection
  - Private key detection
  - Ruff formatting and linting with auto-fix
  - UV sync validation
  - Type checking via `ty` and `pyrefly`

**Type Checking:**
- `ty` - Primary type checker
- `pyrefly` - Secondary type checker for additional analysis

## Platform Requirements

**Development:**
- Python 3.14+
- UV package manager
- Git for version control
- Pre-commit framework (for development hooks)

**Production:**
- Python 3.14 runtime
- No external API keys or services required for core functionality
- Playwright optional for browser testing scenarios

---

*Stack analysis: 2026-03-20*
