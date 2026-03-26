# Quick Start

Get from zero to your first scanned document in paperless-ngx in under five minutes.

## Prerequisites

- A **SANE-compatible scanner** with `saned` running on the machine that has the scanner attached
- A **running paperless-ngx instance** with an API token (generate one under Settings > API Tokens)
- **Docker** (recommended) or **Python 3.14**

## Install

=== "Docker"

    Run the saneless container, pointing it at your scanner host:

    ```bash
    docker run -p 8080:8080 \
      -v ./config.toml:/etc/saneless/config.toml:ro \
      -e SANELESS_SCANNER__HOST=192.168.1.50 \
      ghcr.io/kris-knigga/saneless:latest
    ```

    Replace `192.168.1.50` with the IP address of the machine running `saned`.

    !!! tip "Network scanners"
        The `SANELESS_SCANNER__HOST` environment variable tells saneless where to find your scanner over the network. If your scanner is on the same machine as the container, you can omit this variable. For multi-host setups, see [Scanner Host Discovery](../how-to/scanner-host-discovery.md).

=== "Bare metal"

    Install the SANE development headers for your distribution:

    ```bash
    # Debian / Ubuntu
    sudo apt-get install libsane-dev

    # Fedora / RHEL / Rocky
    sudo dnf install sane-backends-devel
    ```

    Then install saneless with pipx:

    ```bash
    pipx install saneless
    ```

    Start the web server:

    ```bash
    saneless serve
    ```

## Configure

Create a `saneless.toml` file with your paperless-ngx connection details:

```toml
[paperless]
url = "http://192.168.1.50:8000"
token = "abc123def456"
```

Replace the URL and token with your actual paperless-ngx address and API token.

saneless searches for configuration in this order: `--config PATH`, `./saneless.toml`, `~/.config/saneless/config.toml`, `/etc/saneless/config.toml`. See the [Configuration reference](../reference/configuration.md) for all available options.

## Scan

### Web UI

Open `http://localhost:8080` in your browser. Select a profile, enter a title, and click **Scan**. The status area shows progress as the document is scanned, assembled into a PDF, and uploaded to paperless-ngx.

See [First Web UI Scan](first-web-ui-scan.md) for a full walkthrough of every form element.

### CLI

Run a scan from the command line:

```bash
saneless scan --title "My First Scan"
```

See [First CLI Scan](first-cli-scan.md) for the full CLI tutorial.

## Verify

Open your paperless-ngx web interface and search for the document title you used. The scanned PDF should appear with the correct title, ready for tagging and further processing.

## Next steps

- [First Web UI Scan](first-web-ui-scan.md) -- Walk through every element of the web interface.
- [First CLI Scan](first-cli-scan.md) -- Detailed CLI scanning tutorial.
- [Configure Scan Profiles](../how-to/configure-scan-profiles.md) -- Set up profiles for different scan types.
- [Deploy with Docker Compose](../how-to/deploy-docker-compose.md) -- Run saneless as a permanent service alongside paperless-ngx.
