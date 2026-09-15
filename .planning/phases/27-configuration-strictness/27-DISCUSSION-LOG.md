# Phase 27: Configuration Strictness - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-09-15
**Phase:** 27-configuration-strictness
**Areas discussed:** --force merge rules, Rewrites under Docker, Bad-config messages, Profile title key

---

## --force merge rules

### Hand-written same-name profile under --force — what does the command say?

| Option | Description | Selected |
|--------|-------------|----------|
| Skip and name it | Output lists each skipped profile and why; no new flag | ✓ |
| Skip + a --replace flag | Same report plus a flag that merges into hand-written profiles | |
| Skip silently | Not listed in the generated output | |

**User's choice:** Skip and name it

### Hand-edited generated key (resolution 300 → 600) in an auto_generated profile

| Option | Description | Selected |
|--------|-------------|----------|
| Overwrite it | Generated keys are tool-owned while flagged; user keys and comments survive | ✓ |
| Keep hand edits | Only fill missing generated keys; needs a shadow record of what was written | |
| Overwrite and report | Overwrite and print each changed key old → new | |

**User's choice:** Overwrite it

### Stale generated key omitted by regeneration (duplex hardware → none)

| Option | Description | Selected |
|--------|-------------|----------|
| Delete omitted owned keys | Fixed owned-key set; keys the new generation doesn't write are removed | ✓ |
| Write the default explicitly | Write duplex = "none" / auto_source_mode = "flatbed" under --force | |

**User's choice:** Delete omitted owned keys

### Reporting

| Option | Description | Selected |
|--------|-------------|----------|
| Grouped by action | Added / Refreshed / Skipped / Removed lines; worker log same vocabulary | ✓ |
| Written + skipped only | One written list plus skipped; pruning log-only | |
| You decide | Claude picks format | |

**User's choice:** Grouped by action

---

## Rewrites under Docker

Research: rename over a bind-mounted file fails with EBUSY; `-v` on a missing host path creates a
directory; `os.replace` gives the file a new inode (writer's owner) and replaces symlinks.

### Owner after atomic rewrite as root

| Option | Description | Selected |
|--------|-------------|----------|
| Copy the old owner | fchown temp file to original uid/gid when permitted; mode always copied | ✓ |
| Mode only | File becomes root-owned in the container; docs warn | |

**User's choice:** Copy the old owner

### Symlinked config path

| Option | Description | Selected |
|--------|-------------|----------|
| Write through to target | Resolve symlink, replace the real file, keep the link | ✓ |
| Refuse with ConfigError | Refuse to replace a symlink | |

**User's choice:** Write through to target

### Legacy single-file mount → EBUSY

| Option | Description | Selected |
|--------|-------------|----------|
| Fail, name the fix | ConfigError naming the directory-mount fix; no non-atomic fallback | ✓ |
| Fall back to in-place write | Truncate-write with a WARNING | |
| Fail + startup warning | Same plus a startup mount-point probe | |

**User's choice:** Fail, name the fix

### Compose config mount shape

| Option | Description | Selected |
|--------|-------------|----------|
| ./config:/etc/saneless rw | Read-write directory mount; required by success criterion 3 | ✓ |
| Read-only, opt-in rw | Ship :ro, document removing it | |

**User's choice:** ./config:/etc/saneless rw

---

## Bad-config messages

Research (executed locally): a nested env typo `SANELESS_SCANNER__HOSTNAME` yields the same
`extra_forbidden loc=('scanner','hostname')` as a TOML typo; unknown top-level `SANELESS_BOGUS` is
silently ignored; pydantic errors carry the raw `input` value (token leak risk).

### All errors or first?

| Option | Description | Selected |
|--------|-------------|----------|
| All of them, one per line | Header naming the file, one line per problem, exit 2 | ✓ |
| First error only | Single line | |

**User's choice:** All of them, one per line

### Did-you-mean suggestions

| Option | Description | Selected |
|--------|-------------|----------|
| Close match + valid list | difflib suggestion, valid keys, and wrong-section hint | ✓ |
| Valid list only | CFG-01 literal | |
| Close match + wrong section, no list | Shorter lines | |

**User's choice:** Close match + valid list

### Env vs file source attribution

| Option | Description | Selected |
|--------|-------------|----------|
| Name the env var when set | Check the matching SANELESS_<SECTION>__<KEY> in the environment | ✓ |
| Section and key only | No source attribution | |

**User's choice:** Name the env var when set

### Unknown top-level SANELESS_* env vars

| Option | Description | Selected |
|--------|-------------|----------|
| Reject like a TOML typo | Same error list, exit 2, close-match hint | ✓ |
| Warn at startup | WARNING, keep running | |
| Leave as-is | Out of CFG-01's literal scope | |

**User's choice:** Reject like a TOML typo

---

## Profile title key

Research: paperless-ngx workflows already provide `{created}`-style title placeholders applied after
consumption; timezone rendering belongs to APPL-12 (Phase 30).

### Title semantics

| Option | Description | Selected |
|--------|-------------|----------|
| Literal string | Exactly "Receipt"; rename field to default_title, keep title alias | ✓ |
| Literal + timestamp suffix | "Receipt 2026-09-15 14:30" | |
| Template with placeholders | {date}, {time}, {profile} | |

**User's choice:** Literal string

### CLI --title

| Option | Description | Selected |
|--------|-------------|----------|
| Make --title optional | --title > profile title > Scan <timestamp>; "" counts as blank | ✓ |
| Keep --title required | Profile title applies to web UI only | |

**User's choice:** Make --title optional

### Web UI hint

| Option | Description | Selected |
|--------|-------------|----------|
| Server-side only | Resolve on submit; hint deferred to Phase 30 APPL-05 | ✓ |
| Placeholder hint | Input placeholder follows profile dropdown | |

**User's choice:** Server-side only

---

## Claude's Discretion

Presented as stated defaults at the wrap-up prompt; user chose "I'm ready for context" without
changing them:
- `-v` sets DEBUG on the `saneless` logger hierarchy, not root; stderr mirror kept
- XDG read by hand (no platformdirs); search order `./saneless.toml` → `$XDG_CONFIG_HOME/saneless/config.toml` → `/etc/saneless/config.toml`
- `--help` works via lazy settings loading
- CFG-11 startup line logs env-sourced key names only, never values
- `log_level` Literal + upper-casing validator; `~` expansion validator; nearest-existing-ancestor dir check; SecretStr unwrap at construction sites

## Deferred Ideas

- Web form per-profile title hint — Phase 30 (APPL-05)
- Title placeholders — rejected for now
- Stronger `--force` flag for hand-written profiles — rejected
- Startup mount-point probe — rejected
- Log file mode 0600 — candidate for Phase 32 sweep
- Real `--log-level` CLI option — not planned
