---
status: diagnosed
trigger: "Config TOML Loading — extra_forbidden error when loading [default] section from config.toml"
created: 2026-03-21T00:00:00Z
updated: 2026-03-21T00:00:00Z
---

## Current Focus

hypothesis: CONFIRMED — Two compounding issues cause the error
test: n/a (root cause confirmed)
expecting: n/a
next_action: return diagnosis

## Symptoms

expected: Settings loads cleanly from ~/.config/saneless/config.toml containing [default] title = "Test Doc"
actual: ValidationError — extra_forbidden on the "default" key
errors: "1 validation error for Settings default Extra inputs are not permitted [type=extra_forbidden, input_value={'title': 'Test Doc'}, input_type=dict]"
reproduction: Create ~/.config/saneless/config.toml with [default] section, run `uv run saneless scan`
started: always broken for this TOML structure

## Eliminated

(none — root cause found on first hypothesis)

## Evidence

- timestamp: 2026-03-21
  checked: Settings.model_config in config.py
  found: SettingsConfigDict only sets env_prefix and env_nested_delimiter. No explicit `extra` setting.
  implication: pydantic-settings BaseSettings defaults `extra` to "forbid" (confirmed via runtime check).

- timestamp: 2026-03-21
  checked: What TOML `[default]` parses to
  found: tomllib parses `[default] title = "Test Doc"` as `{"default": {"title": "Test Doc"}}` — a top-level key "default"
  implication: "default" is passed as a top-level field to Settings, which only accepts scanner/paperless/output/profiles. With extra=forbid, this raises the error.

- timestamp: 2026-03-21
  checked: ProfileConfig fields
  found: ProfileConfig accepts source, resolution, mode, default_tags, default_correspondent, default_title_template, empty_page_* thresholds. There is NO field called "title".
  implication: Even if the TOML were restructured to [profiles.default], "title" would STILL be rejected because BaseModel also forbids extra fields by default when the parent Settings has extra=forbid... Actually BaseModel defaults to extra="ignore". But "title" is still not a valid ProfileConfig field and would be silently dropped.

- timestamp: 2026-03-21
  checked: pydantic-settings BaseSettings default for extra
  found: `BaseSettings` with empty `SettingsConfigDict()` sets `extra` to `"forbid"` by default (confirmed at runtime).
  implication: This is the mechanism. Any unrecognized top-level key in the TOML triggers the error.

## Resolution

root_cause: |
  Two compounding issues:

  1. **TOML structure mismatch (primary cause of the error):** The user's TOML uses `[default]` as a
     top-level section. `tomllib` parses this as `{"default": {...}}`, injecting "default" as a
     top-level key into Settings. Settings only declares four fields (scanner, paperless, output,
     profiles), and pydantic-settings BaseSettings defaults to `extra = "forbid"`. Any unrecognized
     top-level key triggers `extra_forbidden`.

  2. **No "title" field on ProfileConfig:** Even with the correct TOML structure `[profiles.default]`,
     the field `title` does not exist on ProfileConfig. The closest field is `default_title_template`.
     Since ProfileConfig inherits from BaseModel (which defaults to extra="ignore"), the unknown
     "title" key would be silently dropped rather than error — but it still would not do what the
     user expects.

fix: (not yet applied)
verification: (not yet verified)
files_changed: []
