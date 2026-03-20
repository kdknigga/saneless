# Project Rules

## Linting and Type Checking

All linting and type checking failures must be addressed by fixing the reported issue instead of changing the rules or by adding ignores.

Specifically:
- Do not add new entries to `ignore` or `per-file-ignores` in ruff config
- Do not add `# noqa` comments to suppress warnings
- Do not add `# type: ignore` comments
- Do not modify ty or pyrefly configuration to suppress errors
- Fix the underlying code issue that the linter or type checker is reporting

## Pre-commit

Run `uv run prek run --all-files` before committing to verify all hooks pass.

## Testing

Run `uv run pytest` to execute the test suite.
