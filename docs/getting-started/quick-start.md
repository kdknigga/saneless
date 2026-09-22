# Quick Start

Get from zero to your first scanned document in paperless-ngx in under five minutes.

## Prerequisites

- A **SANE-compatible scanner** with `saned` running on the machine that has the scanner attached
- A **running paperless-ngx instance** with an API token (generate one under Settings > API Tokens)
- **Docker** (recommended) or **Python 3.14**

Not sure which deployment shape you are in? Read [Which setup do I have?](which-setup.md) first -- it takes a minute and decides everything below.

The web UI has **no login** and binds `0.0.0.0`, all network interfaces, so anyone who can reach the port can scan; put it [behind a reverse proxy](../how-to/deploy-docker-compose.md#running-behind-a-reverse-proxy) if that is not what you want.

## Install

=== "Docker"

    Run the saneless container, pointing it at your scanner host:

    ```bash
    docker run -p 8080:8080 \
      -v "$(pwd)/config:/etc/saneless" \
      -e SANELESS_SCANNER__HOST=192.168.1.50 \
      ghcr.io/kdknigga/saneless:latest
    ```

    Replace `192.168.1.50` with the IP address of the machine running `saned`.

    The container reads its configuration from `saneless.toml` inside the mounted `./config` directory. Put the file in `./config`, and keep the directory writable so saneless can save generated profiles to it.

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

saneless searches for configuration in this order: `--config PATH`, `./saneless.toml`, `$XDG_CONFIG_HOME/saneless/saneless.toml` (default `~/.config/saneless/saneless.toml`), `/etc/saneless/saneless.toml`. See the [Configuration reference](../reference/configuration.md) for all available options.

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
