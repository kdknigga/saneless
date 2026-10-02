# First CLI Scan

This tutorial walks you through scanning your first document using the saneless command line. By the end, you will have a working scan-to-paperless pipeline from the terminal.

## Prerequisites

Before you begin, make sure you have:

- **A SANE-compatible scanner**, reached one of two ways. On a bare-metal install, saneless can use a scanner attached to this machine directly. In a container, and for any scanner on another machine, saneless reaches it over the SANE network protocol, which needs `saned` running on the machine the scanner is attached to. If you are not sure which of those you have, start with [Which setup do I have?](which-setup.md).
- **A running paperless-ngx instance** with an API token. You can generate a token in the paperless-ngx admin panel under **Settings > API Tokens**.
- **Python 3.14 or later** (for bare-metal install) or **Docker with Docker Compose** (for container install).

The examples use an HP LaserJet 3030, an all-in-one with a glass and a document feeder, shared by `saned` on a machine at `192.168.1.50`. A scanner with no glass works too; Step 4 shows what changes.

## Step 1: Install saneless

=== "Bare metal"

    Install the SANE development headers for your distribution, then install saneless:

    ```bash
    # Debian / Ubuntu
    sudo apt-get install libsane-dev

    # Fedora / RHEL / Rocky
    sudo dnf install sane-backends-devel
    ```

    Then install saneless. It is not yet published on PyPI, so this installs the current default branch from GitHub:

    ```bash
    pipx install git+https://github.com/kdknigga/saneless
    ```

=== "Docker"

    This tutorial assumes the compose deployment. Follow Steps 1 to 3 of
    [Deploy with Docker Compose](../how-to/deploy-docker-compose.md): they create
    `config/saneless.toml` and `docker-compose.yml`, and start the `saneless`
    service. Every command in this tutorial then runs inside that service, as
    `docker compose exec saneless saneless …`.

## Step 2: Create a configuration file

=== "Bare metal"

    Create a file called `saneless.toml` in the directory you will run saneless from. saneless reads `./saneless.toml` from the current directory, so run every command in this tutorial from there.

=== "Docker"

    You created `config/saneless.toml` beside `docker-compose.yml` in the compose guide. The container reads it as `/etc/saneless/saneless.toml`.

The file needs only your paperless-ngx connection details:

```toml
[paperless]
url = "http://192.168.1.50:8000"
token = "your-api-token-here"
```

Replace the URL and token with your actual paperless-ngx address and API token. Leave scan profiles out: Step 4 asks the scanner what it offers and writes them into this file. [Where saneless reads settings](../reference/configuration.md#where-saneless-reads-settings) explains how saneless finds the file.

If your scanner is attached to another machine running `saned`, as the example scanner is, saneless needs that machine's address. On bare metal, add `host = "192.168.1.50"` under a `[scanner]` table in this file. In the compose deployment, `SANELESS_SCANNER__HOST` in the compose file sets it.

## Step 3: Verify your scanner is detected

Run the `devices` command to check that saneless can see your scanner:

=== "Bare metal"

    ```bash
    saneless devices
    ```

=== "Docker"

    ```bash
    docker compose exec saneless saneless devices
    ```

You should see output like this:

```text
Discovering scanners...
Name                           Vendor          Model                Type
------------------------------------------------------------------------
net:192.168.1.50:hpaio:/usb/h… Hewlett-Packard hp_LaserJet_3030     all-in-one
```

`Discovering scanners...` is a status line printed on stderr, and so is `No scanners found.` when there are none. Only the table goes to stdout, so piping the command into another tool leaves those lines out. The table fits your terminal, so a long device name is cut short with `…`; `saneless devices --json` prints it whole.

!!! tip "No scanner found?"

    If no scanners appear:

    - **Check that `saned` is running** on the machine with the scanner attached. A bare-metal install with the scanner on this machine does not need `saned` at all.
    - **Verify network connectivity** -- can you reach the scanner host from the machine running saneless?
    - **In a container**, a locally attached scanner is never visible directly: the container reaches every scanner over the SANE network protocol, so even a scanner plugged into the container's own host needs `saned` on that host and `SANELESS_SCANNER__HOST` pointing at it.
    - **Container users**: if saneless runs in Docker, you need to tell it where to find scanners. See [Scanner Host Discovery (Containers)](../how-to/scanner-host-discovery.md).

## Step 4: Generate scan profiles

Ask the scanner what it offers and write a scan profile for each of its sources:

=== "Bare metal"

    ```bash
    saneless auto-profiles
    ```

=== "Docker"

    ```bash
    docker compose exec saneless saneless auto-profiles
    ```

    In the compose deployment the server writes the same profiles when it starts, if it could reach the scanner and the file held no profiles. The command then reports `Skipped (already exists; use --force to refresh): 'auto', 'adf', 'default'` instead of `Added:`, and still pins the scanner. Either way the profiles are in `config/saneless.toml`.

For the HP LaserJet 3030 it prints:

```text
Profiles in /home/you/saneless.toml:
Added: 'auto', 'adf', 'default'
  auto: source=Auto, resolution=300, mode=Color
  adf: source=ADF, resolution=300, mode=Color
  default: source=Auto, resolution=300, mode=Color
Pinned [scanner] device: 'net:192.168.1.50:hpaio:/usb/hp_LaserJet_3030?serial=00MXBM121742'
```

The first line names the config file it wrote: the `saneless.toml` you created in Step 2 (in the container, `/etc/saneless/saneless.toml`). The command reads the sources the scanner reports and writes one profile for each. The HP names its sources `Auto` and `ADF`, and its glass is reached through `Auto`. `default` is a copy of `auto`, so it scans one page from the glass; `adf` takes sheets from the document feeder. The last line records the scanner in `[scanner] device`, so later scans keep going to it.

An `Auto` profile scans one page from the glass unless the scanner shows no sign of having one. `auto-profiles` sets this for you in the profile's `auto_source_mode`; to change it, see [Auto source](../how-to/configure-scan-profiles.md#auto-source).

!!! note "If your scanner has no glass"

    A sheet-fed scanner such as the Fujitsu fi-7160 is listed like this:

    ```text
    Name                           Vendor          Model                Type
    ------------------------------------------------------------------------
    net:192.168.1.50:fujitsu:fi-7… FUJITSU         fi-7160              scanner
    ```

    It offers only its feeder, so `auto-profiles` writes `adf-front`, `adf-back` and `adf-duplex`, and a `default` that feeds from `ADF Front`:

    ```text
    Profiles in /home/you/saneless.toml:
    Added: 'adf-front', 'adf-back', 'adf-duplex', 'default'
      adf-front: source=ADF Front, resolution=300, mode=Color
      adf-back: source=ADF Back, resolution=300, mode=Color
      adf-duplex: source=ADF Duplex, resolution=300, mode=Color
      default: source=ADF Front, resolution=300, mode=Color
    Pinned [scanner] device: 'net:192.168.1.50:fujitsu:fi-7160:12345'
    ```

    The scan in the next step then takes every sheet in the feeder.

## Step 5: Scan a document

Place a document on the glass (or, with no glass, in the feeder), then run:

=== "Bare metal"

    ```bash
    saneless scan --title "My First Scan"
    ```

=== "Docker"

    ```bash
    docker compose exec saneless saneless scan --title "My First Scan"
    ```

saneless will:

1. Open the scanner `auto-profiles` recorded in `[scanner] device`.
2. Scan with the `default` profile: on the HP, one page from the glass at 300 DPI in color.
3. Assemble the scanned pages into a PDF.
4. Upload the PDF to paperless-ngx with the title "My First Scan".

You will see progress output as each step completes:

```
Scanning...
Assembling PDF...
Uploading to paperless-ngx...
Done: My First Scan
```

`Done:` means the document was uploaded cleanly, and the command exits 0. A scan that did not go
cleanly says so instead: `Uploaded with a warning: My First Scan` (exit 7) when, for example, a
sheet was skipped, with the warning printed to stderr; or `Saved to folder: My First Scan` (exit 6)
when the upload kept failing and a consume folder is configured, so the PDF went there without its
title, tags or correspondent. See the [exit codes](../reference/cli-commands.md#exit-codes) for
every outcome.

!!! tip "Paper size"
    To avoid scanning the entire scanner bed, set `paper_size` in your profile (e.g., `paper_size = "a4"` or `paper_size = "letter"`). See [Configure Scan Profiles](../how-to/configure-scan-profiles.md).

## Step 6: Verify in paperless-ngx

Open your paperless-ngx web interface in a browser. Search for "My First Scan" using the search bar. Your document should appear with the correct title and be ready for tagging, correspondent assignment, or any other paperless-ngx workflow you have configured.

If the document does not appear after a minute, check:

- The paperless-ngx URL and token in your `saneless.toml` are correct.
- paperless-ngx is running and accessible from the machine where saneless ran.
- The saneless output showed `Done:` and the command exited 0. `Uploaded with a warning:` (exit 7)
  also means the document was uploaded; `Saved to folder:` (exit 6) means it was put in the consume
  folder instead, and an error line means it was not delivered. See the
  [exit codes](../reference/cli-commands.md#exit-codes).

## Next steps

Now that you have a working scan pipeline, explore these guides:

- [First Web UI Scan](first-web-ui-scan.md) -- Scan documents from the browser interface.
- [Configure Scan Profiles](../how-to/configure-scan-profiles.md) -- Set up profiles for different scan types (duplex, high resolution, grayscale).
- [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md) -- Scan double-sided multi-page documents with an automatic document feeder.
- [Deploy with Docker Compose](../how-to/deploy-docker-compose.md) -- Run saneless as a permanent service alongside paperless-ngx.
- [CLI Commands](../reference/cli-commands.md) -- See all available commands, flags, and options.
