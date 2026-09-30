# Configure Scan Profiles

Scan profiles define how saneless scans documents -- the paper source, resolution, color mode, and default metadata. Each profile is a named section in your configuration file.

## What you'll need

- saneless installed and working ([Install on Bare Metal](install-bare-metal.md) or [Deploy with Docker Compose](deploy-docker-compose.md))
- A text editor to modify `saneless.toml`

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
| `label` | string | `""` | The name shown in the web UI's profile dropdown. `auto-profiles` fills this in -- `Feeder, single-sided`, `Feeder, double-sided`, `Feeder, front side only`, `Glass (flatbed)` or, on a scanner with no choice of source, `Standard scan` -- and owns it; see [Auto-generated profiles](#auto-generated-profiles). A profile with an empty label is listed under its profile name. When two profiles on the scan page would show the same label, each is followed by its profile name in brackets |
| `description` | string | `""` | The sentence shown beneath the profile dropdown, such as `Scans both sides of every page using the document feeder.`. Also filled in and owned by `auto-profiles` |
| `source` | string | `"Flatbed"` | Paper source: `"Flatbed"`, `"ADF"`, or `"ADF Duplex"` |
| `duplex` | string | `"none"` | How both sides of a sheet are scanned: `"none"`, `"hardware"` or `"manual"`. `"manual"` runs the two-pass flip workflow. `"hardware"` scans both sides through the source: most scanners do that because of the source's name, and on scanners with a separate ADF mode option (`adf-mode`) saneless sets it to `Duplex`; see [ADF Hardware Duplex](set-up-adf-duplex.md#adf-hardware-duplex) |
| `resolution` | integer | `300` | Scan resolution in DPI |
| `mode` | string | `"color"` | Color mode: `"Color"`, `"Gray"`, or `"Lineart"` |
| `title` | string | `""` | Default document title, used as written when you leave the title blank in the web UI or omit `--title` on the CLI. A title you type always wins; with neither, the title is `Scan <date time>`, rendered in the server's local timezone with the zone named -- for example `Scan 2026-03-22 09:30 CDT`. At most 118 characters; a longer one stops the config from loading |
| `default_tags` | list of int | `[]` | Paperless-ngx tag IDs to apply automatically, each from 1 to 2147483647. The web form shows them pre-ticked; see [Setting default metadata](#setting-default-metadata) |
| `default_correspondent` | int or null | `null` | Paperless-ngx correspondent ID, from 1 to 2147483647. The web form shows it pre-selected |
| `enable_empty_page_detection` | bool | `true` | Remove blank pages from scans |
| `empty_page_coverage_threshold` | float | `0.001` | The most ink a page may carry and still be removed as blank, as a percentage (0 to 100) of the page inside a 3% margin. Lower keeps more pages; `0` removes only pages with no ink at all |
| `auto_source_mode` | string | `"flatbed"` | When source is `"Auto"`: `"flatbed"` for single-page or `"adf"` for multi-page feeder |
| `paper_size` | string | `"full"` | Constrain scan area: `"full"`, `"a3"`, `"a4"`, `"a5"`, `"letter"`, `"legal"`. Applied on the flatbed, and on a feeder only when the scanner reports `page-width` and `page-height`; see [Paper size](#paper-size) |
| `auto_generated` | bool | `false` | Set by `auto-profiles`; marks machine-generated profiles |

## Source values

The `source` field determines how pages are fed to the scanner:

- **`"Flatbed"`** -- Flatbed scanning. Place the page on the glass; each scan takes one page. For a document of several pages, tick **Multiple pages** on the scan form (or pass `saneless scan --multi-page`) and saneless asks after each page whether there is another, then puts every page in one document. See [Scan a Multi-Page Document](scan-a-multi-page-document.md).
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

The names above are common, but the scanner decides what its sources are called: run `saneless devices --capabilities` to list them. A profile's `source` is matched against that list ignoring case and surrounding spaces, and the scan uses the scanner's own spelling, so `source = "adf"` finds `ADF`. A name the scanner does not list is refused before any paper moves, with an error naming the sources it does list. The one exception is a flatbed name on a scanner that has no flatbed source, under any name, but has `Auto`: the scan falls back to `Auto`, and when the profile's `auto_source_mode` then sends it through the feeder, the job finishes with a warning saying so. A scanner that lists its flatbed under another name, such as `Flatbed Scanner`, does not get `Auto`: the scan is refused, and the error lists that name.

**Multiple pages** is a choice made for each scan, not a profile setting, so no profile key turns it on. It works with every source above, and on a feeder it lets you hand-feed a document a sheet or a few sheets at a time. It is not available with a `duplex = "manual"` profile.

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

### What `auto-profiles` guesses, and when to override it

`saneless auto-profiles` has to pick one of the two values for you, and an `Auto`
source does not say which it should be. It writes `"adf"` only when it can find no
evidence that the scanner has a glass at all -- neither a source named `Flatbed`
nor a device type that has one. Anything else gets the `"flatbed"` default.

Some backends report a scanner less completely than others. HP's `hpaio`, for
example, names only `Auto` and `ADF` for an all-in-one that does have a glass. If
your generated `Auto` profile guessed wrong, set the field yourself:

```toml
[profiles.auto]
source = "Auto"
auto_source_mode = "flatbed"   # or "adf"
```

A scan that guessed `"adf"` on a flatbed reports a scanner error after the first
page, because saneless asks the scanner for a second sheet the glass cannot
supply. Some scanners also show a panel message such as "Memory is low" when this
happens. Set `auto_source_mode = "flatbed"`, or re-run `saneless auto-profiles
--force` to regenerate the value.

Other scanners scan their glass again for every sheet asked for, as if it were a
feeder that never runs out. saneless stops an `Auto` source sent through the feeder
after 50 sheets, uploads what it kept, and finishes the job with a warning that says
to set `auto_source_mode = "flatbed"` if the feeder was already empty. A source named
as a feeder stops after 500 sheets instead.

See [Configuration reference](../reference/configuration.md) for all profile fields.

## Paper size

By default, saneless scans the entire scanner bed. If your documents are a standard size,
set `paper_size` to limit the scan area to that size:

```toml
[profiles.letters]
source = "Flatbed"
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

The scan area and the crop are measured from the top-left corner of the bed, which is where a
sheet lies on the glass. A feeder may guide the sheet against one side or centre it, and
saneless cannot see which, so on a scan through the feeder:

- If the scanner reports `page-width` and `page-height` for the selected source, saneless sets
  them to the paper size, the scanner places its own window over the sheet, and the scan area
  is set inside that window as on the glass.
- Otherwise the paper size is not applied. The page is scanned at the full window and not
  cropped, so a centred sheet keeps both edges, and the log says at INFO that `paper_size` was
  not applied and why.

See [Paper size on a feeder](set-up-adf-duplex.md#paper-size-on-a-feeder).

See [Configuration reference](../reference/configuration.md) for all profile fields.

## Auto-generated profiles

If you are unsure what sources and modes your scanner supports, saneless can generate profiles automatically from your scanner's reported capabilities:

```bash
saneless auto-profiles
```

Profile names come from your scanner's own source names, lowercased and reduced to letters, digits and hyphens. A scanner reporting `Flatbed` and `Automatic Document Feeder` gets profiles named `flatbed` and `automatic-document-feeder`.

The labels shown in the web UI are saneless's own wording, chosen from the kind of source, and never repeat the scanner's source name:

- A feeder reads `Feeder, single-sided`, or `Feeder, double-sided` when it scans both sides. A feeder source that names one side -- such as `ADF Front` or `ADF Back` -- reads `Feeder, front side only` or `Feeder, back side only`.
- The glass reads `Glass (flatbed)`, an `Auto` source `Automatic`, and a source saneless cannot place `Scanner source`.
- When two sources would get the same label, the second gets a number: `Feeder, single-sided`, then `Feeder, single-sided 2`. The description beneath the dropdown stays the same for both.
- A scanner with no choice of source at all gets a single `default` profile labelled `Standard scan`, with no `source` key. Each scan with it takes one page, even on a sheet-fed scanner, because saneless has no source to tell a feeder from the glass. Tick **Multiple pages** in the web UI, or pass `--multi-page` to `saneless scan`, to put several pages in one document.

If `auto-profiles` creates the config file from scratch, it creates it with mode `0600`, readable only by you, because the file may hold your paperless-ngx token. Rewriting an existing file keeps its permission bits, owner and group, each when saneless is permitted to set it: a non-root user cannot give the file back to another owner, but keeps the group if it belongs to it, and a filesystem without Unix permissions keeps none of them.

Without `--force`, a profile that already exists is left alone. Use `--force` to refresh the profiles `auto-profiles` created earlier:

```bash
saneless auto-profiles --force
```

`--force` merges; it does not replace whole profiles:

- It refreshes only profiles that carry `auto_generated = true`. In those, only the generated keys (`label`, `description`, `source`, `resolution`, `mode`, `auto_source_mode`, `duplex`, `auto_generated`) are rewritten in place, and a generated key the new run no longer writes is removed.
- Everything else in the profile is kept: `default_tags`, `default_correspondent`, `title`, `paper_size`, the empty-page settings, and your comments.
- A hand edit to a generated key, such as `resolution = 600`, is overwritten. To keep your edits, delete the `auto_generated` line from that profile.
- A profile without `auto_generated = true` is never changed, even with `--force`. It is listed as `Skipped (not auto-generated)`; rename or delete it to let `auto-profiles` regenerate it -- except `default`, which saneless requires and which you therefore cannot rename or delete. `default` gets a skip line of its own, `Skipped (not auto-generated): 'default' -- add auto_generated = true to its table to let auto-profiles --force refresh it`. To hand a `default` you wrote back to the tool, add `auto_generated = true` to its `[profiles.default]` table and run `saneless auto-profiles --force`.
- A flagged profile that already matches what the scanner reports is left as it is and not listed as `Refreshed`. When nothing changes at all, the file is not rewritten, and the command prints `No changes to` and the file's path.

`label` and `description` are ordinary generated keys, and they are the first ones that hold text you might want to write yourself. The rule is the same for them as for `resolution`: if a profile still carries `auto_generated = true`, a name you typed by hand is replaced the next time you run `saneless auto-profiles --force`. To keep your own wording, delete the `auto_generated` line from that profile -- that hands the profile to you permanently and `auto-profiles` never touches it again.

`saneless auto-profiles --force` is also how you fill in names for profiles that were generated before saneless started writing them. Startup generation only runs on a config that still holds nothing but the untouched `default` profile, so an existing config keeps its empty labels -- and lists profiles under their profile names -- until you run the command once.

The command reports what it did, one line per kind of change, and prints only the lines that apply:

```text
Added: ...
Refreshed: ...
Skipped (not auto-generated): ...
Skipped (already exists; use --force to refresh): ...
Removed (scanner no longer offers it): ...
Pinned [scanner] device: ...
```

When `[scanner] device` is empty, `auto-profiles` also writes the id of the device it generated profiles for into it, and prints the `Pinned` line. With no device set, every scan goes to whichever scanner SANE lists first, so a scanner that appears on your network later could take your scans; pinning stops that. A device you have already set is never overwritten, even with `--force`. If more than one scanner is visible, run `saneless devices` first and set `[scanner] device` yourself when the first one listed is not yours. `saneless doctor` and the status strip warn while several scanners are visible and none is set, and the log says when the auto-detected scanner changes between two scans.

Regenerating can rename profiles, so if you pass `--profile` in a script or a cron entry, check the name still matches.

`auto-profiles` always writes a `default` profile -- a copy of your scanner's flatbed profile if it has one, otherwise of the profile for the first source the scanner reports, and on a scanner with no choice of source the `Standard scan` profile -- and regenerating never removes it. saneless requires that profile, and a config without it is one saneless refuses to load. Every other auto-generated profile a new run no longer produces is removed, so a rename does not leave a stale duplicate behind; on a scanner with no choice of source nothing is removed at all.

Because the generated `default` is an exact copy of another profile, the scan page offers that profile only once: `default` is left out of the dropdown while it still carries `auto_generated = true` and matches another profile in every key, and the first such profile stands in for it. The page opens on `default`, or on the profile standing in for it, so an untouched form scans exactly as `saneless scan` does when you give no `--profile`. Once you change anything in `[profiles.default]` -- add `default_tags`, say -- it is no longer a copy, and the scan page lists it as a profile of its own. `saneless scan --profile default` and the web API accept `default` either way.

See [CLI Commands](../reference/cli-commands.md) for full `auto-profiles` documentation.

### Generation at server startup

`saneless serve` also generates profiles, once at startup, when the config holds only the untouched `default` profile -- a single profile named `default` with every field at its default value. Any profile you have written or changed turns this off. Generation runs in the background after the server starts, so a page loaded in the first moments may list only `default` until you reload it.

Where the generated profiles go depends on the config file saneless loaded:

- **A config file was loaded** (the `--config` path, or the first file found in the [search path](../reference/configuration.md#config-file-search-path)): the profiles are added to that file, as `saneless auto-profiles` would add them, and used straight away.
- **No config file was loaded:** the profiles are used for this run only and nothing is written. The log names the locations where a config file would be picked up.
- **The config file cannot be written** -- for example, a read-only mount, or `saneless.toml` bind-mounted as a single file (the rename fails with EBUSY; mount its directory instead, see [Deploy with Docker Compose](deploy-docker-compose.md)): the profiles are used for this run only, and a warning is logged.

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

Tag and correspondent IDs match the IDs in your paperless-ngx instance. Find them in the paperless-ngx admin interface or via its API. Each id must be from 1 to 2147483647, or the config does not load; an id listed twice counts once.

The web UI and `saneless scan --profile receipts` apply these defaults the same way:

- **The web form shows them pre-ticked.** When the page opens, and again whenever you choose another profile, the Tags list shows the profile's `default_tags` ticked and the Correspondent dropdown shows its `default_correspondent` selected, replacing whatever was ticked before. Scan without touching them and the document gets the defaults; clear the ticks, or choose no correspondent, and it gets none.
- **A hidden control still applies them.** With `show_tags = false` or `show_correspondent = false` in [`[web]`](../reference/configuration.md#web), every scan from the profile gets its defaults.
- **`saneless scan` applies them too.** The CLI has no tag or correspondent option, so a CLI scan always gets the profile's defaults.

**A default that no longer exists in paperless-ngx is skipped with a warning.** If a tag or correspondent was deleted in paperless-ngx, saneless finds out before it scans, asks paperless-ngx once more to be sure, and then files the document without it. The scan ends **Uploaded with a warning** (exit 7 from `saneless scan`), naming the id: *"tag 7 no longer exists in paperless-ngx and was not applied."* When saneless can read paperless-ngx's lists, the web form marks such a default in advance, still ticked, with a note that it will be skipped. Remove the id from the profile to stop the warning.

paperless-ngx lists only the tags and correspondents the API token's user may see, so a tag the token cannot see counts as missing and is skipped the same way, even though it exists. Give the token's user permission to view it if it should apply. If paperless-ngx cannot be asked at all -- it is down, or takes more than 5 seconds to answer -- the ids are sent unchecked and paperless-ngx decides.

## Empty page detection tuning

Empty page detection removes blank sides from duplex scans. It is enabled by default. To disable it for a specific profile:

```toml
[profiles.photos]
source = "Flatbed"
resolution = 600
mode = "Color"
enable_empty_page_detection = false
```

A page counts as empty only when its ink covers **no more than** `empty_page_coverage_threshold` percent of the page inside a narrow margin. So to keep pages with faint or sparse content -- pencil, a light stamp, a nearly empty form -- lower the threshold. Raising it removes more pages, not fewer. At `0`, only a page with no ink at all is removed:

```toml
[profiles.pencil-notes]
source = "ADF"
resolution = 300
mode = "Gray"
empty_page_coverage_threshold = 0.0005
```

See [How Empty Page Detection Works](../explanation/empty-page-detection.md#tuning-the-threshold) for what is measured and both directions written out.
