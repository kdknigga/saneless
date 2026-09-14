# Configure Scan Profiles

Scan profiles define how saneless scans documents -- the paper source, resolution, color mode, and default metadata. Each profile is a named section in your configuration file.

## What you'll need

- saneless installed and working ([Install on Bare Metal](install-bare-metal.md) or [Deploy with Docker Compose](deploy-docker-compose.md))
- A text editor to modify `saneless.toml` or `config.toml`

## Profile basics

Profiles live under `[profiles.NAME]` in your configuration file. The `default` profile is required and is used when no profile is specified:

```toml
[profiles.default]
source = "Flatbed"
resolution = 300
mode = "Color"
```

## Example: Multiple profiles

A typical setup includes profiles for different scanning scenarios:

```toml
[profiles.default]
source = "Flatbed"
resolution = 300
mode = "Color"

[profiles.fast]
source = "ADF"
resolution = 150
mode = "Gray"

[profiles.duplex]
source = "ADF Duplex"
resolution = 300
mode = "Color"
```

## Using profiles

Specify a profile when scanning from the CLI:

```bash
saneless scan --profile duplex --title "Invoice March 2026"
```

In the web UI, select the profile from the dropdown before clicking Scan.

## Profile field reference

| Field | Type | Default | Description |
|---|---|---|---|
| `source` | string | `"Flatbed"` | Paper source: `"Flatbed"`, `"ADF"`, or `"ADF Duplex"` |
| `duplex` | string | `"none"` | How both sides of a sheet are scanned: `"none"`, `"hardware"` or `"manual"`. `"manual"` runs the two-pass flip workflow; `"hardware"` only records that the source scans both sides and does not change the scan |
| `resolution` | integer | `300` | Scan resolution in DPI |
| `mode` | string | `"color"` | Color mode: `"Color"`, `"Gray"`, or `"Lineart"` |
| `title` | string | `""` | Default title template for scanned documents |
| `default_tags` | list of int | `[]` | Paperless-ngx tag IDs to apply automatically |
| `default_correspondent` | int or null | `null` | Paperless-ngx correspondent ID |
| `enable_empty_page_detection` | bool | `true` | Remove blank pages from scans |
| `empty_page_mean_threshold` | float | `250.0` | Mean luminance threshold for blank detection (higher = stricter) |
| `empty_page_stddev_threshold` | float | `5.0` | Standard deviation threshold for blank detection |
| `auto_source_mode` | string | `"flatbed"` | When source is `"Auto"`: `"flatbed"` for single-page or `"adf"` for multi-page feeder |
| `paper_size` | string | `"full"` | Constrain scan area: `"full"`, `"a3"`, `"a4"`, `"a5"`, `"letter"`, `"legal"` |
| `auto_generated` | bool | `false` | Set by `auto-profiles`; marks machine-generated profiles |

## Source values

The `source` field determines how pages are fed to the scanner:

- **`"Flatbed"`** -- Single-page flatbed scanning. Place the document on the glass.
- **`"ADF"`** -- Automatic Document Feeder, one side per page. Load a stack of pages.
- **`"ADF Duplex"`** -- Hardware duplex via ADF. The scanner scans both sides of each page automatically (requires hardware support).

For manual two-pass duplex scanning on a scanner without hardware duplex, keep `source` set to a feeder source your scanner reports and add `duplex = "manual"`:

```toml
[profiles.manual-duplex]
source = "ADF"
duplex = "manual"
resolution = 300
mode = "Color"
```

See [Set Up ADF Duplex Scanning](set-up-adf-duplex.md#manual-duplex) for the full flow.

## Auto source

Some scanners expose only an `Auto` source instead of separate `Flatbed` and `ADF` entries.
When you set `source = "Auto"`, saneless needs to know whether to treat it as a single-page
flatbed scan or a multi-page ADF scan. The `auto_source_mode` field controls this:

| `auto_source_mode` | Behavior |
|---|---|
| `"flatbed"` (default) | Single page, like Flatbed |
| `"adf"` | Multi-page feeder, like ADF |

```toml
[profiles.auto]
source = "Auto"
auto_source_mode = "adf"
resolution = 300
mode = "Color"
```

See [Configuration reference](../reference/configuration.md) for all profile fields.

## Paper size

By default, saneless scans the entire scanner bed. If your documents are a standard size,
set `paper_size` to crop the scan area automatically:

```toml
[profiles.letters]
source = "ADF"
resolution = 300
mode = "Color"
paper_size = "letter"
```

Available presets: `full` (default -- entire bed), `a3`, `a4`, `a5`, `letter`, `legal`.

saneless checks the scan-area options your scanner reports, sets them using the unit the
scanner asks for, and then reads the area back to confirm the scanner kept it.

Three things can prevent the scan area being set at the hardware level: your scanner may not
report all four scan-area options, it may report them in a unit saneless cannot convert to a
length, or it may silently shrink the area to something smaller than you asked for. In any of
those cases saneless crops the image after scanning instead, and logs which of the three
happened.

See [Configuration reference](../reference/configuration.md) for all profile fields.

## Auto-generated profiles

If you are unsure what sources and modes your scanner supports, saneless can generate profiles automatically from your scanner's reported capabilities:

```bash
saneless auto-profiles
```

Profile names come from your scanner's own source names, lowercased and reduced to letters, digits and hyphens. A scanner reporting `Flatbed` and `Automatic Document Feeder` gets profiles named `flatbed` and `automatic-document-feeder`. Use `--force` to overwrite existing auto-generated profiles:

```bash
saneless auto-profiles --force
```

Regenerating can rename profiles, so if you pass `--profile` in a script or a cron entry, check the name still matches.

`auto-profiles` always writes a `default` profile -- backed by your scanner's flatbed if it has one, and otherwise by the first source the scanner reports -- and regenerating never removes it. saneless requires that profile, and a config without it is one saneless refuses to load. Every other auto-generated profile a new run no longer produces is removed, so a rename does not leave a stale duplicate behind. Profiles you wrote yourself are never touched.

See [CLI Commands](../reference/cli-commands.md) for full `auto-profiles` documentation.

### Generation at server startup

`saneless serve` also generates profiles, once at startup, when the config holds only the untouched `default` profile -- a single profile named `default` with every field at its default value. Any profile you have written or changed turns this off. Generation runs in the background after the server starts, so a page loaded in the first moments may list only `default` until you reload it.

Where the generated profiles go depends on the config file saneless loaded:

- **A config file was loaded** (the `--config` path, or the first file found in the [search path](../reference/configuration.md#config-file-search-path)): the profiles are added to that file, as `saneless auto-profiles` would add them, and used straight away.
- **No config file was loaded:** the profiles are used for this run only and nothing is written. The log names the locations where a config file would be picked up.
- **The config file cannot be written** -- for example, because it is mounted read-only, as in the Docker Compose examples: the profiles are used for this run only, and a warning is logged.

Generation is tried once per start. If the scanner was not reachable, saneless keeps the bare `default` profile and logs why; connect the scanner, then restart saneless or run `saneless auto-profiles` to try again.

## Setting default metadata

Profiles can include default paperless-ngx metadata so you do not need to specify it on every scan:

```toml
[profiles.receipts]
source = "ADF"
resolution = 300
mode = "Color"
title = "Receipt"
default_tags = [3, 7]
default_correspondent = 12
```

Tag and correspondent IDs match the IDs in your paperless-ngx instance. Find them in the paperless-ngx admin interface or via its API.

## Empty page detection tuning

Empty page detection removes blank sides from duplex scans. It is enabled by default. To disable it for a specific profile:

```toml
[profiles.photos]
source = "Flatbed"
resolution = 600
mode = "Color"
enable_empty_page_detection = false
```

To adjust sensitivity, lower the thresholds to detect pages with faint content as non-empty:

```toml
[profiles.pencil-notes]
source = "ADF"
resolution = 300
mode = "Gray"
empty_page_mean_threshold = 240.0
empty_page_stddev_threshold = 3.0
```
