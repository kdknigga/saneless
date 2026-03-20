# Coding Conventions

**Analysis Date:** 2026-03-20

## Naming Patterns

**Files:**
- Lowercase with underscores: `__init__.py`
- Package naming: lowercase with no underscores for top-level packages (`saneless/`)
- Module files: lowercase with underscores for multi-word modules

**Functions:**
- Lowercase with underscores (snake_case): `def main() -> None:`
- Exported functions must have type hints on all parameters and return types

**Variables:**
- Lowercase with underscores (snake_case) for standard variables and private attributes
- Underscore prefix for internal/private variables: `_private_var`
- UPPER_CASE for module-level constants (where applicable)
- Unused variables must follow the dummy variable pattern: `^(_+|(_+[a-zA-Z0-9_]*[a-zA-Z0-9]+?))$` (can be named `_`, `__`, `_var`, etc.)

**Types:**
- PascalCase for class names and type aliases
- Type hints on all functions required by Ruff strict mode (ANN - all annotations rule enabled)

## Code Style

**Formatting:**
- Tool: Ruff (with Black-compatible settings)
- Line length: 88 characters
- Indentation: 4 spaces (not tabs)
- Quote style: Double quotes (`"string"`) for all string literals
- Trailing commas: Respect magic trailing commas in lists/dicts (skip-magic-trailing-comma = false)
- Line endings: Auto-detected (`\n` on Unix/Linux)

**Example formatting:**
```python
def process_data(items: list[str]) -> dict[str, int]:
    """Process a list of items and return counts."""
    result = {}
    for item in items:
        result[item] = len(item)
    return result
```

**Linting:**
- Tool: Ruff with comprehensive rule set
- Enabled rules: E, F, B, G, W, YTT, ANN, ASYNC, S, FBT, A, COM, C4, EM, ICN, LOG, PIE, Q, RSE, RET, SIM, TID, TCH, ARG, PTH, FLY, PERF, FURB, RUF, UP, D, I, PL, PT, DTZ, T20
- Ignored rules: E501 (long lines), D203 (blank line before class docstring), D212 (docstring summary on next line), W505 (doc line too long), PLR2004 (magic values), COM812 (trailing commas in multi-line)
- All fixable rules enabled (use `ruff check --fix` to auto-fix violations)
- Type checking via `ty` and `pyrefly` pre-commit hooks (see pre-commit config)

**Per-file exceptions:**
- `tests/**/*.py`: Relaxed annotations (ANN), type checking (TCH), print statements (T201), module-level imports (PLC0415), long functions (PLR0915, PLR0912, PLR0913), and assert statements (S101)
- `src/saneless/__init__.py`: Module-level imports allowed (PLC0415)

## Import Organization

**Order:**
1. Standard library imports (`import os`, `import sys`)
2. Third-party imports (`import pytest`, `import playwright`)
3. Local/relative imports (`from .module import function`, `from saneless.core import Handler`)

**Within each group:** Alphabetically sorted

**Path Aliases:**
- No aliases configured in tsconfig (Python project)
- Use relative imports within `saneless/` package: `from .sibling import Function`
- Use absolute imports from outside: `from saneless.core import Handler`

**Example:**
```python
import sys
from pathlib import Path

import playwright

from .config import Settings
from saneless.scanner import ScannerConnection
```

## Error Handling

**Patterns:**
- Explicit exception handling preferred over bare except
- Use built-in exception types where possible
- Custom exceptions inherit from `Exception` or appropriate base class
- Include descriptive error messages with context
- Do not swallow exceptions silently without logging

**Example (expected pattern):**
```python
def connect_scanner(host: str) -> ScannerConnection:
    """Connect to scanner or raise ValueError if unreachable."""
    try:
        conn = ScannerConnection(host)
        return conn
    except ConnectionError as e:
        raise ValueError(f"Cannot connect to scanner at {host}") from e
```

## Logging

**Framework:** Python's built-in `logging` module (import with `import logging`)

**Patterns:**
- Use `logger = logging.getLogger(__name__)` at module level
- Log levels: DEBUG (detailed), INFO (workflow), WARNING (recoverable issues), ERROR (failures), CRITICAL (system failure)
- Include context in log messages (e.g., scanner ID, user action)
- Use structured logging where helpful (logging with extra dicts or f-strings with context)

**Example (expected pattern):**
```python
import logging

logger = logging.getLogger(__name__)

def scan_document(device_id: str) -> bytes:
    logger.info(f"Starting scan on device {device_id}")
    # ... scan logic ...
    logger.info(f"Completed scan on device {device_id}")
```

## Comments

**When to Comment:**
- Document **why**, not **what** (code should show what, comments explain why)
- Use comments for non-obvious business logic or workarounds
- Mark temporary/experimental code with `# TODO:` or `# FIXME:` (followed by issue number if available)

**JSDoc/TSDoc Style (Docstrings):**
- Use Google-style docstrings for all public functions, classes, and modules
- Single-line docstring: `"""Brief description."""`
- Multi-line docstring: Summary line, blank line, then detailed description
- Include Args, Returns, and Raises sections for functions
- Ruff docstring rule (D) enforces docstring presence on public symbols

**Example:**
```python
def process_scan(device_id: str, pages: int) -> bytes:
    """Assemble multi-page scan into single PDF.

    Args:
        device_id: SANE device identifier (e.g., "epson:libusb:001:002").
        pages: Number of pages to scan.

    Returns:
        PDF binary data.

    Raises:
        ConnectionError: If device becomes unavailable during scan.
        ValueError: If pages <= 0.
    """
    if pages <= 0:
        raise ValueError("Pages must be positive")
    # ... implementation ...
```

## Function Design

**Size:**
- Keep functions small and focused (typically under 50 lines)
- If a function exceeds 50 lines, consider breaking it into smaller pieces
- Ruff may warn on complex functions (PLR0915, PLR0912, PLR0913 soft limits exist)

**Parameters:**
- Prefer explicit parameters over *args/**kwargs
- Maximum 3-5 parameters; use dataclass or typed dict for more
- All parameters must have type hints (ANN rule enforced)
- Default parameters are acceptable but keep them simple/immutable

**Return Values:**
- Always include return type hint (ANN rule enforced)
- Return None explicitly or not at all (not `return None`)
- Consider using dataclasses or typed dicts for complex return structures

**Example:**
```python
def get_scanner_status(device_id: str, timeout: int = 5) -> dict[str, str]:
    """Fetch current scanner status with timeout."""
    # ... implementation ...
    return {"status": "idle", "pages": 0}
```

## Module Design

**Exports:**
- Use `__all__` list at module level to define public API: `__all__ = ["Function", "Class"]`
- Private/internal symbols prefixed with underscore: `_helper_function()`
- All public symbols should be documented with docstrings

**Barrel Files:**
- Use `__init__.py` for package-level exports when appropriate
- Example: `saneless/__init__.py` can import and re-export from submodules for convenience
- Keep barrel files minimal; avoid circular imports

**Example:**
```python
# src/saneless/scanner.py
"""Scanner device communication."""

__all__ = ["ScannerConnection", "ScannerError"]


class ScannerConnection:
    """Wrapper for SANE device connection."""
    pass


class ScannerError(Exception):
    """Scanner operation failed."""
    pass


def _internal_helper() -> None:
    """Private utility not exported."""
    pass
```

---

*Convention analysis: 2026-03-20*
