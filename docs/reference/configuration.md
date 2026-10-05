# Configuration (TOML)

saneless uses a TOML configuration file with environment variable overrides. All settings have sensible defaults -- a minimal config only needs a `[paperless]` section with `url` and `token`. Scan profiles come from `saneless auto-profiles`, which asks the scanner what it offers, or from the built-in `default` profile until you run it.

## Where saneless reads settings

Settings are loaded from the first file found, in priority order:

1. `--config PATH` -- explicit CLI flag (highest priority)
2. `./saneless.toml` -- current working directory
3. `$XDG_CONFIG_HOME/saneless/saneless.toml` -- XDG config directory (`~/.config/saneless/saneless.toml` when `XDG_CONFIG_HOME` is unset, empty or relative)
4. `/etc/saneless/saneless.toml` -- system-wide (typical for Docker)

The file is called `saneless.toml` in every searched location. No other filename is a search candidate, in any of them.

If no file is found, defaults and environment variables are used.

Each setting then takes its value from the first of these that sets it:

1. An environment variable, `SANELESS_<SECTION>__<FIELD>` (see [Environment Variables](environment-variables.md))
2. The loaded `saneless.toml`
3. The built-in default

A set variable wins over the file silently: nothing warns that the value in the file was ignored, and the startup log lists only the names of the settings that came from the environment. So keep the paperless-ngx URL and token in `saneless.toml` and nowhere else, and do not also set `SANELESS_PAPERLESS__URL` or `SANELESS_PAPERLESS__TOKEN` in a compose file or shell profile, where a stale value would go on overriding the one you edit.

Only one file is ever read. If more than one `saneless.toml` is found, the first one in this list is read and the others are not: the log warns once for each file it is not reading, and the [Configuration check](#the-configuration-check) is amber and names the file in use and every file that is not read. One file reached twice -- through a symlink, or because the working directory is one of the other locations -- counts once.

When no config file was loaded, `saneless auto-profiles` writes `/etc/saneless/saneless.toml` if the `/etc/saneless` directory already exists and is writable (in the container, the mounted `./config` directory), otherwise `$XDG_CONFIG_HOME/saneless/saneless.toml`, and never `./saneless.toml`, which would be read ahead of both. It creates a missing XDG directory with mode `0700`, owned like the directory it is created in when the command may set that (so `sudo -E` leaves nothing root-only in your home), and it never creates `/etc/saneless`.

A path that is not a regular file (for example a directory) is skipped. An explicit `--config PATH` that does not exist, or is not a regular file, is an error (exit code 2). A leading `~` in `--config` is expanded to your home directory.

!!! warning "saneless reads saneless.toml, never config.toml"

    saneless reads only `saneless.toml`. A file called `config.toml` in a searched directory is recognised by its name, never opened, and never merged into your settings.

    Rename it:

    ```bash
    mv ~/.config/saneless/config.toml ~/.config/saneless/saneless.toml
    # or, for a system-wide install
    sudo mv /etc/saneless/config.toml /etc/saneless/saneless.toml
    ```

    Until you do, saneless says so in four places rather than starting up quietly on defaults: the log names the file it ignored, the [Configuration check](#the-configuration-check) is red and names both the file and the rename, `saneless doctor` lists it in its resolution table and exits 2, and `saneless auto-profiles` refuses to run -- a fresh `saneless.toml` would load ahead of the old file and bury the URL and token in the file you have not renamed yet.

    If the `config.toml` sits beside a `saneless.toml` that did load, the check is amber instead: the right file is in use, but the `config.toml` may hold settings that are being ignored. Move anything you still need out of it into the loaded file, and only then delete it. Do not delete it first: the `config.toml` may hold the only copy of your paperless-ngx URL and token, and the `saneless.toml` in use may have been written by `auto-profiles` with profiles and nothing else.

    A `./config.toml` belonging to some other tool in your working directory is flagged in the same way, because saneless cannot tell the two apart and will not read either. Run saneless from another directory, or create the `saneless.toml` it is looking for.

### When no config file loads

Every start logs one line saying where the settings came from. When a file loaded, that line names the file. When none did, it names every place saneless looked instead, absolute and in search order:

```text
Configuration: no config file; defaults + environment; searched /home/you/saneless.toml, /home/you/.config/saneless/saneless.toml, /etc/saneless/saneless.toml
```

The searched list appears only in this case. A start that loaded a file logs that file's path and no list -- the location that matters is the one in use, and three extra lines on every healthy start are noise in a log you read when something is wrong.

A file under the old name found during that search gets its own warning, one per file, in search order:

```text
Ignoring /etc/saneless/config.toml: saneless reads saneless.toml, not config.toml; rename it to /etc/saneless/saneless.toml, then restart saneless
```

If a `saneless.toml` did load and an old-named file was left beside it, the warning says to move before it says to delete:

```text
Ignoring leftover /etc/saneless/config.toml: /etc/saneless/saneless.toml is in use; move anything you still need from it into /etc/saneless/saneless.toml, then delete it
```

A second `saneless.toml` that was found and not read gets a warning of its own, one per file:

```text
Not reading /etc/saneless/saneless.toml: /home/you/.config/saneless/saneless.toml is in use and was found first; move anything you still need from it into /home/you/.config/saneless/saneless.toml, or delete it if it is not deliberate
```

On a machine where the log is not to hand, `saneless doctor` prints the same facts as a table -- every candidate, whether it exists, which one was used and any file ignored under the old name. See [CLI Commands](cli-commands.md#saneless-doctor).

### The Configuration check

`Configuration` is the first of the six rows on the status page and in `saneless doctor`, and it is always present. It is the row that says which configuration file is in use:

| Situation | State | Message | Next step |
|-----------|-------|---------|-----------|
| A file loaded, nothing left under the old name | OK | `Config file loaded.` | *(none)* |
| A file loaded, another `saneless.toml` found after it | Warning | `Using USED; OTHER is also there and is not read.` | `If that is not deliberate, move anything you still need from OTHER into USED, then delete OTHER and restart saneless.` |
| A file loaded, an old-named file beside it | Warning | `Using saneless.toml; an old config.toml is being ignored.` | `Move anything you still need from FILE into the saneless.toml in use, then delete FILE and restart saneless.` |
| No file loaded, nothing found under the old name | Warning | `No config file; running on defaults and environment variables.` | `The saneless log lists every place it looked for saneless.toml.` |
| No file loaded, an old-named file found | Failed | `No config file loaded: saneless now reads saneless.toml, not config.toml.` | `Rename FILE to saneless.toml, then restart saneless.` |

`USED` is the `saneless.toml` in use, and `OTHER` every other `saneless.toml` found, joined with "and"; with more than one, the sentences say "are also there and are not read" and "then delete them". `FILE` is the old-named file saneless found and did not read. On the status page each is named by its documented spelling -- `./saneless.toml`, `$XDG_CONFIG_HOME/saneless/saneless.toml` or `/etc/saneless/saneless.toml`, and `./config.toml`, `$XDG_CONFIG_HOME/saneless/config.toml` or `/etc/saneless/config.toml` for an old-named file -- because that page is visible to everyone on your network and carries no filesystem paths. The log, `saneless doctor` and the stderr warning of a one-shot command give the absolute path instead.

A second `saneless.toml` is reported ahead of an old-named leftover: when both are there, the row names the second `saneless.toml`, and a one-shot command's stderr warning prints that row and then the leftover row.

A missing config file is a warning and never a failure. Configuring saneless entirely through `SANELESS_*` variables is supported (see [Environment Variables](environment-variables.md)), and an unset paperless-ngx URL or token already reddens the Paperless row -- it is not this row's job to report it twice.

## Validation

Every section rejects keys it does not know, and so does the top level. A misspelt key is an error when the config loads, not a setting that is silently ignored. The error names the file (or the environment variable that supplied the value), the section and the key, suggests a close match when there is one, and lists the valid keys (or names the section a misplaced key belongs in). Type and value errors use the same `[section] key` form. Each problem gets its own line, and values are never printed, so a token in a mistyped key does not end up in your terminal or log. saneless then exits with code 2.

```text
Configuration error in /etc/saneless/saneless.toml:
  [paperless] unknown key 'tokne' (did you mean 'token'?); valid keys: url, token, consume_dir
  [paperless] unknown key 'web_port'; it belongs in [output]
```

A `SANELESS_*` environment variable that holds malformed JSON for a list or table field gets a line of its own, naming the variable and never its value, and the file's own errors are still listed with it:

```text
Configuration error in /etc/saneless/saneless.toml:
  [output] history_retention_days: Input should be greater than or equal to 1
  environment variable 'SANELESS_PROFILES__DEFAULT__DEFAULT_TAGS': must be JSON (a list or table is written as JSON, for example [3, 7])
```

While such a variable cannot be read, the file is checked with the environment left out, so a mistake in another `SANELESS_*` variable is reported on the next start, once the JSON is fixed.

Unknown `SANELESS_*` environment variables are rejected the same way; see [Environment Variables](environment-variables.md#notes).

---

## `[scanner]`

Scanner connection settings.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `""` | SANE net host IP/hostname. Empty = local USB, on a bare-metal install only -- in a container, leave this set, because the container reaches every scanner through `saned` over the network. Colon-separated for multiple hosts (e.g., `192.168.1.50:192.168.1.51`). |
| `device` | string | `""` | Pin a specific SANE device name. Empty = auto-detect first available, which lets a scanner that appears on your network later take your scans; setting it is recommended. `saneless devices` lists the names, and `saneless auto-profiles` writes the one it used when this is empty. |

## `[paperless]`

Paperless-ngx API connection settings. saneless works with paperless-ngx 2.16 or later (API version 9 or 10): it speaks version 9 until paperless-ngx says it allows 10, as 3.x does. An older paperless-ngx refuses both, and every upload and tag fetch then fails with `does not accept API version 9 or 10; saneless needs paperless-ngx 2.16 or later`, while the status strip reports `incompatible_version`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | `""` | Paperless-ngx base URL (e.g., `http://paperless:8000`). It must be an `http://` or `https://` address that names a host, and it must not contain a user name or password: put the API token in `token`. Write an international host name in its `xn--` form. Spaces and line breaks around the value are ignored. `""` means not set: saneless still starts, but a scan fails as a configuration error when it comes to upload, and nothing is copied to `consume_dir`. Any other value that breaks these rules stops saneless when the config loads (exit 2), with an error naming the key and never the value |
| `token` | string | `""` | API authentication token. Spaces and line breaks around it are ignored, so a trailing newline from a secret file or a CRLF `.env` file does no harm. What remains must be visible ASCII, with no spaces or control characters inside it. A value that breaks these rules stops saneless when the config loads (exit 2), with an error naming the key and never the value. saneless never puts the token into a log or error message itself, and when a paperless-ngx reply, a proxy's reply or an HTTP library error quotes it, the token is replaced by `***` in the message, the log and any traceback |
| `consume_dir` | string | unset (empty or omitted means disabled) | Fallback directory for PDF deposit when API is unavailable. An empty or whitespace-only value disables the fallback rather than naming the working directory. A leading `~` is expanded. saneless never creates it: a directory that is missing when a fallback is needed fails the scan with `does not exist — is the paperless-ngx volume mounted?`, and the PDF is kept in `failed/`. See [How Consume Directory Fallback Works](../explanation/consume-directory-fallback.md) |

### Document date

saneless sends no document date with an upload, so paperless-ngx dates the document itself. It
uses the first date it finds in the document: in the file name, but only when
`PAPERLESS_FILENAME_DATE_ORDER` is set in paperless-ngx, and then in the text of the document
after OCR. It reads dates in the order `PAPERLESS_DATE_ORDER` gives, skips any listed in
`PAPERLESS_IGNORE_DATES`, and takes only dates after 1900 that are not in the future. When it
finds none, the document is dated with the date and time paperless-ngx received the upload, or,
for a file saneless [saved to the consume directory](../explanation/consume-directory-fallback.md),
the time saneless saved it there. Those are paperless-ngx settings, not saneless ones: set them
on the paperless-ngx side.

## `[output]`

Output, logging, and web server settings.

In the path settings (`tmp_dir`, `data_dir`, `log_file`, and `consume_dir` under `[paperless]`), a leading `~` is expanded to your home directory. Environment variables such as `$HOME` inside a value are not expanded.

A relative path is resolved against the directory of the config file that was loaded -- the directory the file was found in, even when the file is a symlink to somewhere else -- so `data_dir = "state"` in `/etc/saneless/saneless.toml` means `/etc/saneless/state`, whichever directory saneless was started from. A relative value from a `SANELESS_*` variable is resolved the same way, or against the working directory when no config file was loaded. The log and `saneless doctor` show the absolute result. Write a path absolute to keep it where it is whatever file loads.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tmp_dir` | string | `$TMPDIR/saneless-<uid>`, for example `/tmp/saneless-1000` (`/tmp` when `TMPDIR` is unset; `<uid>` is your numeric user id) | Scratch space for the scan in progress; its contents are deleted as each scan finishes and nothing durable is kept here. When it is missing, saneless creates it with mode `0700`, so only your user can enter it. An existing directory is refused at startup, and again before each scan, if it is a symlink, is owned by another user, or is group- or world-writable; the message names `output.tmp_dir` and the path, and tells you to run `chmod 700` on it, remove it, or choose another directory. Group or world *read* is accepted. Only the directory itself is checked, so do not put it inside a directory other users can write to unless that directory is sticky, as `/tmp` is: there another user could swap it for a symlink after the check. A leading `~` is expanded |
| `data_dir` | string | `$XDG_STATE_HOME/saneless` (`~/.local/state/saneless` when `XDG_STATE_HOME` is unset) | Durable state: the job database (`saneless.db`) and `failed/`, where scans that could not be delivered to paperless-ngx are preserved. Must survive restarts. The container image sets this to `/var/lib/saneless`. When saneless creates it, it is created with mode `0700`; a directory that already exists keeps its mode. A leading `~` is expanded |
| `log_file` | string | `$XDG_STATE_HOME/saneless/saneless.log` (`~/.local/state/saneless/saneless.log` when `XDG_STATE_HOME` is unset) | Log file path, **for one-shot CLI commands only**. `saneless serve` streams its records to stderr and writes no file at all, so under Docker or systemd the platform (`docker logs`, journald) holds them and owns retention. A leading `~` is expanded |
| `log_level` | string | `"INFO"` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, case-insensitive (`warn` means `WARNING`); any other value is rejected when the config loads. Applies in **both** modes, unlike the three keys around it. `saneless -v` shows saneless's own debug detail without changing this setting |
| `log_max_bytes` | int | `10485760` | Max log file size in bytes before rotation (the default is 10 MiB), 1 or more. **One-shot CLI commands only**, like `log_file`: `saneless serve` writes no file, so there is nothing to rotate |
| `log_backup_count` | int | `5` | Number of rotated log files to keep, from 1 to 1000. **One-shot CLI commands only**, like `log_file`: `saneless serve` writes no file, so there is nothing to keep |
| `history_retention_days` | int | `7` | Days to keep job history, by creation time and regardless of whether the job finished. From 1 to 36500 (about a century; there is no "forever" value, and 0 is refused) |
| `history_max_rows` | int | `500` | Maximum job history entries retained in SQLite, from 1 to 1000000 (0 is refused; it would erase the history); the newest are kept. Refused submits are kept separately, only the newest 20, and do not count toward this limit, so a burst of refusals never pushes a real scan out of history. |
| `paperless_task_timeout` | int | `300` | Seconds to wait for paperless-ngx task completion, from 1 to 86400 (one day) |
| `paperless_cache_ttl_seconds` | int | `60` | Cache TTL for paperless tag/correspondent lists (seconds), 0 or more (0 turns the cache off) |
| `operator_wait_timeout_seconds` | int | `600` | Seconds saneless waits for a person to answer a prompt, in the web UI and the CLI. It bounds every such wait. A manual duplex scan waits this long for the operator to flip the stack between passes; if nobody confirms in time, the job fails and nothing is uploaded. A multi-page scan waits this long at each prompt between passes; if nobody answers in time, the document is finished with the pages kept so far. Must be a whole number of seconds from 1 to 86400 (one day); 0 and negative values are rejected when the config is loaded, so there is no "wait forever" setting. This key was renamed from `flip_timeout_seconds`, with no alias: a config that still sets the old name fails to load, and the error suggests `operator_wait_timeout_seconds` |
| `min_free_space_mb` | int | `500` | Free disk space (MB) saneless keeps in reserve for assembling the PDF, 0 or more. A megabyte here is 1,000,000 bytes, and so is every MB figure saneless prints about free space. It is checked twice: once before a scan starts, and again before each page is written to disk, against that page's size *plus* this reserve. A scan that runs out of room fails naming the page number and the path, and the pages already scanned are preserved. Assembling a PDF needs about twice the spooled pages' size free on top of this reserve, because the single-page PDFs and the merged document sit beside the spool while it is built. saneless checks for that before it assembles anything; when that much is not free it refuses before writing a byte, names the MB needed and the MB free, and keeps the page files in `failed/` |
| `web_host` | string | `"0.0.0.0"` | Web server bind address. The default `0.0.0.0` listens on all network interfaces |
| `web_port` | int | `8080` | Web server port, from 0 to 65535. `0` lets the OS choose a free port; a value outside the range is rejected when the config is loaded |

A number outside its key's range stops saneless when the config loads (exit 2), with an error naming the section and the key. A TOML `true` or `false` is not a number, in this section or in a profile, so `web_port = true` is refused with `must be a number, not true or false` rather than read as 1.

## `[web]`

Which optional controls the scan form shows, and which extra host names saneless answers to. Both form keys default to `true`, so the form shows both controls unless you turn one off.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `show_tags` | bool | `true` | Show the Tags checkbox list on the scan form. `false` hides the whole Tags block, filter included |
| `show_correspondent` | bool | `true` | Show the Correspondent dropdown on the scan form. `false` hides it |
| `allowed_hosts` | list of strings | `[]` | Extra host names saneless answers to, on top of the ones it always answers to. See [Allowed host names](#allowed-host-names) |

This is one appliance with one configured form shape, not a per-browser preference: everyone who opens the page sees the same form, and there is no control in the UI to turn either back on. Edit the file and restart saneless.

**Hiding a control changes the form, never the scan.** A shown control starts from the profile's defaults: the form shows the profile's `default_tags` pre-ticked and its `default_correspondent` pre-selected, when the page opens and again whenever another profile is chosen, replacing whatever was ticked before. So an untouched form scans with the defaults, and a form whose ticks were cleared, or whose correspondent was set to none, scans with none. A hidden control applies the defaults, just as an untouched one does -- the same way a blank title falls back to the profile's `title`. So `show_tags = false` with `default_tags = [3, 7]` means every scan from that profile is tagged 3 and 7, and nobody has to think about it. Use this to hand a household member a form with a Profile, a Title and a Scan button. `saneless scan --profile` applies the same defaults, through the same rule.

**A default that no longer exists in paperless-ngx is skipped with a warning.** Before any paper moves, saneless checks the scan's tags and correspondent against paperless-ngx's lists, asking again once if an id is missing. An id still missing is dropped: the document is filed without it, and the scan ends **Uploaded with a warning** (exit 7 from `saneless scan`), for example *"tag 9 no longer exists in paperless-ngx and was not applied."* When saneless can read paperless-ngx's list, the form shows such a default still ticked, with a note that it no longer exists and will be skipped; untick it to scan without the warning. A tag the API token's user cannot see is missing as far as saneless can tell, so it is dropped the same way. When paperless-ngx cannot be asked -- each list gets 5 seconds -- the ids are sent unchecked, and paperless-ngx decides.

**The bind address is not here.** `web_host` and `web_port` stayed under [`[output]`](#output), where they have always been, because moving them would break every deployment that already sets them or their `SANELESS_OUTPUT__WEB_*` variables. `[web]` holds the form-shape keys and `allowed_hosts`, the names saneless answers to; `[output]` holds the address it listens on.

```toml
[web]
show_tags = true
show_correspondent = false
allowed_hosts = ["scan.example.com", ".home.example"]
```

### Allowed host names

saneless refuses every request whose `Host` header does not name it, with `421 Misdirected Request`. This is what stops a web page on another site from reaching saneless through DNS rebinding; see [Host check](web-api.md#host-check). Without configuration, saneless answers to IP addresses (IPv4, and IPv6 in brackets), `localhost` and every other name without a dot, and names ending in `.local`, `.home.arpa`, `.internal` or `.lan`.

`allowed_hosts` adds names to that set. It never replaces it, so adding a name cannot lock you out of `http://<lan-ip>:8080`. Each entry is one of:

- **An exact name**, such as `scan.example.com`. It matches that name only, not `www.scan.example.com`.
- **A suffix with a leading dot**, such as `.example.com`. It matches `example.com` itself and every name under it, such as `scan.example.com` and `a.b.example.com`.

Entries are compared without case, and the port a request uses does not matter, so write the name alone. A suffix must contain a dot after its leading one: `.com` is refused, because it would trust a whole top-level domain.

**A leading-dot entry trusts every name anyone can register under it.** saneless refuses only a suffix without a second dot, such as `.com`. It cannot tell a domain you own from one where anybody can register a name: a dynamic DNS service's domain such as `.duckdns.org`, or a registry suffix such as `.co.uk`. It accepts both. An entry like that lets a hostile site register a name under it, point that name at saneless's address and reach saneless through DNS rebinding. List your own full name instead, such as `me.duckdns.org`. Use a leading-dot entry only for a domain you control, where nobody else can create names.

There is no `*`. A single wildcard would turn the check off for every name at once, which is exactly what DNS rebinding needs. An empty entry, a `*` anywhere, a port (`scan.example.com:8080`), a scheme (`https://scan.example.com`), a path, a user name, whitespace or a non-ASCII character is a configuration error, and saneless does not start. Write an international name in its `xn--` form. An IP address needs no entry: every IP address is already answered.

A reverse proxy that keeps the original `Host` header -- as it should -- sends its public name to saneless, so put that name here. See [Running behind a reverse proxy](../how-to/deploy-docker-compose.md#running-behind-a-reverse-proxy).

## `[profiles.NAME]`

Scan profiles define scanner settings and default metadata. At least one profile named `default` must exist.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | `""` | What the web UI's profile dropdown calls this profile, at most 64 characters. Empty means the option shows the profile's own name. `saneless auto-profiles` fills it in ("Feeder, double-sided") |
| `description` | string | `""` | One short sentence shown beneath the dropdown when this profile is selected, at most 200 characters. Empty means no description line. `saneless auto-profiles` fills it in |
| `source` | string | `"Flatbed"` | Scan source, as your scanner reports it: for example `Flatbed`, `ADF`, `ADF Duplex`, `Auto`. Run `saneless devices --capabilities` to list them. Matched ignoring case and surrounding spaces; a name the scanner does not list is refused before scanning, except that a flatbed name may fall back to the scanner's `Auto` source. |
| `duplex` | string | `"none"` | How both sides of a sheet are scanned: `none`, `hardware` or `manual`. `manual` runs the two-pass flip workflow and needs a single-sided feeder source (see [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md#manual-duplex)). `hardware` scans both sides through the source. Most scanners select duplex by the source's name (`ADF Duplex`); on scanners that select it with a separate ADF mode option (`adf-mode`, as the epson2, kodakaio and magicolor drivers do), saneless sets that option to `Duplex`, and the scan is refused before any sheet is fed if the option is switched off for the source. On a single-sided feeder source with neither, a warning is logged and one side is scanned (see [ADF Hardware Duplex](../how-to/set-up-adf-duplex.md#adf-hardware-duplex)). |
| `auto_source_mode` | string | `"flatbed"` | When source is `"Auto"`: route as `"flatbed"` (single page) or `"adf"` (multi-page feeder). Ignored for explicit sources. |
| `paper_size` | string | `"full"` | Constrain scan area to a standard paper size. Presets: `full` (entire scanner bed), `a3`, `a4`, `a5`, `letter`, `legal`. On the flatbed, sets the scan area from the top-left corner, or crops the image after scanning when the scanner will not take the area. On a feeder, applied only when the scanner reports `page-width` and `page-height` for the source, which saneless sets so the scanner places the page itself; on any other feeder it is not applied, the page is the full window and is not cropped, and the log says so (see [Paper size on a feeder](../how-to/set-up-adf-duplex.md#paper-size-on-a-feeder)). |
| `resolution` | int | `300` | Scan resolution in DPI, from 1 to 12800. A very high resolution can make a page larger than the image-size limit saneless allows when it assembles the PDF -- about 2,000 dpi on an A4 page -- and that fails the scan after the paper has moved, so stay well below it |
| `mode` | string | `"color"` | Color mode: `Color`, `Gray`, `Lineart` |
| `default_tags` | int[] | `[]` | Paperless-ngx tag IDs to apply automatically, each from 1 to 2147483647. A repeated id counts once. The web form shows them pre-ticked; an id paperless-ngx no longer has is skipped with a warning (see [`[web]`](#web)) |
| `default_correspondent` | int or null | `null` | Paperless-ngx correspondent ID, from 1 to 2147483647. The web form shows it pre-selected; one paperless-ngx no longer has is skipped with a warning |
| `title` | string | `""` | Default document title, used as written when the title is left blank (typed title first, then this, then `Scan <date time>`). At most 118 characters: paperless-ngx keeps 127, and a manual duplex scan whose halves are uploaded separately adds ` (fronts)` or ` (backs)`. saneless never shortens a title. `title` is the only spelling: `default_title` is refused when the config loads, with a hint to write `title` |
| `enable_empty_page_detection` | bool | `true` | Enable automatic empty page removal |
| `empty_page_coverage_threshold` | float | `0.001` | The most ink a page may carry and still be removed as blank, as a percentage of the page inside a 3% margin on every edge. From 0 to 100, where `0` removes only pages with no ink at all; any other value is rejected when the config loads. Lower keeps more pages. See [How Empty Page Detection Works](../explanation/empty-page-detection.md#tuning-the-threshold) |
| `auto_generated` | bool | `false` | Whether this profile was auto-generated from scanner capabilities |

A profile `title` longer than 118 characters, or a `default_tags` or `default_correspondent` id outside 1 to 2147483647, fails when the config loads (exit 2), with an error naming the profile and the key.

`auto_generated = true` marks a profile as tool-owned: `saneless auto-profiles --force` rewrites its generated keys, `label` and `description` among them, so anything you write there is replaced the next time you run it. **To take a profile over, delete its `auto_generated` line.** saneless then leaves the whole profile alone, and your own `label` and `description` are what the dropdown shows. See [Configure Scan Profiles](../how-to/configure-scan-profiles.md) for the full ownership rules.

---

## Complete Example

```toml
[scanner]
host = "192.168.1.50"
device = ""

[paperless]
url = "http://paperless:8000"
token = "your-api-token-here"
consume_dir = ""

# The absolute paths below are the container layout. A bare-metal install
# keeps the default paths: leave tmp_dir, data_dir and log_file out.
[output]
tmp_dir = "/tmp/saneless-1000"
data_dir = "/var/lib/saneless"
log_file = "/var/log/saneless/saneless.log"
log_level = "INFO"
log_max_bytes = 10485760
log_backup_count = 5
history_retention_days = 7
history_max_rows = 500
paperless_task_timeout = 300
paperless_cache_ttl_seconds = 60
operator_wait_timeout_seconds = 600
min_free_space_mb = 500
web_host = "0.0.0.0"
web_port = 8080

[web]
show_tags = true
show_correspondent = true

[profiles.default]
label = "Glass (flatbed)"
description = "One page at a time from the scanner glass."
source = "Flatbed"
resolution = 300
mode = "Color"

[profiles.receipts]
source = "ADF"
resolution = 200
mode = "Gray"
paper_size = "letter"
default_tags = [3, 7]
default_correspondent = 12
title = "Receipt"
enable_empty_page_detection = true

[profiles.duplex-letters]
source = "ADF Duplex"
resolution = 300
mode = "Color"
default_tags = [1]
title = "Letter"

[profiles.auto]
source = "Auto"
auto_source_mode = "adf"
resolution = 300
mode = "Color"
```
