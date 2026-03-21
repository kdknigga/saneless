# Project Rules

## Tech Stack

- **Language:** Python 3.14
- **Package/env management:** `uv` (not pip, not poetry, not conda)
- **Pre-commit hooks:** `prek` (a `pre-commit` drop-in replacement)

## Commands

```bash
uv run saneless              # Run the application
uv run ruff check .        # Lint
uv run ruff check --fix .  # Lint with auto-fix
uv run ruff format .       # Format code
uv run ty check            # Type check (ty)
uv run pyrefly check       # Type check (pyrefly)
uv run prek run                   # Run all pre-commit hooks
uv sync                    # Sync dependencies from uv.lock
uv add <package>           # Add a dependency
uv add --dev <package>     # Add a dev dependency
```

## Code Quality Standards

All code must pass these checks with zero errors or warnings:

1. **Ruff linting** — extensive rule set configured in `pyproject.toml`
2. **Ruff formatting** — Black-compatible, double quotes, 88 char line length, spaces
3. **ty type checking** — all issues must be fixed, not ignored
4. **pyrefly type checking** — all issues must be fixed, not ignored
5. **No mypy or pyright** — only ty and pyrefly are used

Fix reported issues properly. Do not suppress errors with `# type: ignore`, `# noqa`, or by disabling rules.

## Dependencies

Prefer external packages over reimplementing solved problems. Favor libraries available in Context7.

## MCP Servers Available

Use these tools during development:

- **Serena** — Semantic code analysis and editing. Use for token-efficient code navigation via symbols and references instead of reading entire files.
- **Context7** — Fetch current library/framework documentation. Always use this instead of relying on training data when working with external packages.
- **Tavily** — Web search, crawling, and content extraction for research.
- **Playwright** — Browser automation for all browser-based validation. Never mark browser checks as "manual-only" or "needs human." Use Playwright MCP to automate visual layout verification, HTMX interactivity, responsive design, chart rendering, and accessibility checks.

## Browser-Based Validation

All web application development must be validated via Playwright MCP before asking the human to test. Never mark browser-based checks as "manual-only", "needs human", or "human verification required." Use Playwright MCP to automate:
- Visual layout and CSS rendering (navigate + screenshot + DOM assertions)
- HTMX interactivity and live polling (trigger actions, wait for DOM updates)
- Responsive design (resize viewport, assert layout changes)
- Form interactivity (fill fields, submit, verify results)
- Dark/light mode rendering
- Component visibility and state (buttons disabled, prompts shown/hidden)

The only legitimate "human verification" items are those requiring physical hardware (e.g., real scanner attached) or external network services that cannot be stubbed.

## Skills available

Consider the use of the following skills that are available to you:
- claude-automation-recommender
- claude-md-improver
- frontend-design
- astral:ty
- astral:uv
- astral:ruff

## Gotchas

- `ty` and `pyrefly` may report different issues for the same code — both must pass clean
- Ruff's `D` rules require docstrings on all public modules, classes, and functions
- Ruff ignores `D203`/`D212`: use no-blank-line-before-class and multi-line-summary-second-line style
- `prek` is a drop-in replacement for `pre-commit` — use `uv run prek run` not `pre-commit run`
