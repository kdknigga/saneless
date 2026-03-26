# First CLI Scan

This tutorial walks you through scanning your first document using the saneless command line. By the end, you will have a working scan-to-paperless pipeline from the terminal.

## Prerequisites

Before you begin, make sure you have:

- **A SANE-compatible scanner** connected via USB or network, with `saned` running on the machine that has the scanner attached. saneless talks to scanners through the SANE network protocol.
- **A running paperless-ngx instance** with an API token. You can generate a token in the paperless-ngx admin panel under **Settings > API Tokens**.
- **Python 3.14 or later** (for bare-metal install) or **Docker** (for container install).

## Step 1: Install saneless

=== "Bare metal"

    Install the SANE development headers for your distribution, then install saneless:

    ```bash
    # Debian / Ubuntu
    sudo apt-get install libsane-dev

    # Fedora / RHEL / Rocky
    sudo dnf install sane-backends-devel
    ```

    Then install saneless:

    ```bash
    pipx install saneless
    ```

=== "Docker"

    For a quick test, run saneless directly:

    ```bash
    docker run -p 8080:8080 ghcr.io/kris-knigga/saneless
    ```

    For a permanent setup alongside paperless-ngx, see the
    [Deploy with Docker Compose](../how-to/deploy-docker-compose.md) guide.

## Step 2: Verify your scanner is detected

Run the `devices` command to check that saneless can see your scanner:

```bash
saneless devices
```

You should see output like this:

```
Discovering scanners...
Name                 Vendor          Model                Type
------------------------------------------------------------
net:192.168.1.100:fujitsu:fi-7160    Fujitsu             fi-7160              scanner
```

!!! tip "No scanner found?"

    If no scanners appear:

    - **Check that `saned` is running** on the machine with the scanner attached.
    - **Verify network connectivity** -- can you reach the scanner host from the machine running saneless?
    - **USB scanners** must be connected to the machine running `saned`, not the machine running saneless.
    - **Container users**: if saneless runs in Docker, you need to tell it where to find scanners. See [Scanner Host Discovery (Containers)](../how-to/scanner-host-discovery.md).

## Step 3: Create a configuration file

Create a file called `saneless.toml` in your current directory with your paperless-ngx connection details:

```toml
[paperless]
url = "http://192.168.1.50:8000"
token = "abc123def456"
```

Replace the URL and token with your actual paperless-ngx address and API token.

This is the minimum configuration needed. saneless auto-detects your scanner, so you do not need to specify a device unless you have multiple scanners.

!!! info "Configuration file search paths"

    saneless looks for configuration in these locations, in order:

    1. The path you pass with `--config /path/to/config.toml`
    2. `./saneless.toml` (current directory)
    3. `~/.config/saneless/config.toml`
    4. `/etc/saneless/config.toml`

    The first file found is used. For all available configuration options, see the [Configuration reference](../reference/configuration.md).

## Step 4: Scan a document

Place a document on your scanner's flatbed (or in the ADF tray), then run:

```bash
saneless scan --title "My First Scan"
```

saneless will:

1. Detect your scanner automatically.
2. Scan the document from the flatbed at 300 DPI in color (the defaults).
3. Assemble a single-page PDF.
4. Upload the PDF to paperless-ngx with the title "My First Scan".

You will see progress output as each step completes:

```
Scanning...
Assembling PDF...
Uploading to paperless-ngx...
Done: My First Scan
```

!!! tip "Auto source scanners"
    If your scanner only exposes an "Auto" source, you can control whether it behaves as flatbed or ADF by setting `auto_source_mode` in your profile. See [Configure Scan Profiles](../how-to/configure-scan-profiles.md).

!!! tip "Paper size"
    To avoid scanning the entire scanner bed, set `paper_size` in your profile (e.g., `paper_size = "a4"` or `paper_size = "letter"`). See [Configure Scan Profiles](../how-to/configure-scan-profiles.md).

## Step 5: Verify in paperless-ngx

Open your paperless-ngx web interface in a browser. Search for "My First Scan" using the search bar. Your document should appear with the correct title and be ready for tagging, correspondent assignment, or any other paperless-ngx workflow you have configured.

If the document does not appear after a minute, check:

- The paperless-ngx URL and token in your `saneless.toml` are correct.
- paperless-ngx is running and accessible from the machine where saneless ran.
- The saneless output showed "Done" and not an error message.

## Next steps

Now that you have a working scan pipeline, explore these guides:

- [First Web UI Scan](first-web-ui-scan.md) -- Scan documents from the browser interface.
- [Configure Scan Profiles](../how-to/configure-scan-profiles.md) -- Set up profiles for different scan types (duplex, high resolution, grayscale).
- [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md) -- Scan double-sided multi-page documents with an automatic document feeder.
- [Deploy with Docker Compose](../how-to/deploy-docker-compose.md) -- Run saneless as a permanent service alongside paperless-ngx.
- [CLI Commands](../reference/cli-commands.md) -- See all available commands, flags, and options.
