# Quick Start

Get from zero to your first scanned document in paperless-ngx in under five minutes.

## Prerequisites

- A **SANE-compatible scanner**. In a container, or with the scanner attached to another machine, `saned` must be running on the machine the scanner is attached to. A bare-metal install on the machine the scanner is plugged into needs no `saned`.
- A **running paperless-ngx instance** with an API token (generate one under Settings > API Tokens)
- **Docker** (recommended) or **Python 3.14**

Not sure which deployment shape you are in? Read [Which setup do I have?](which-setup.md) first -- it takes a minute and decides everything below.

The web UI has **no login** and binds `0.0.0.0`, all network interfaces, so anyone who can reach the port can scan. They can also see that scans run and how they end, but not what they are: only the browser that started a scan sees its title and preview. saneless answers only to its own host names, so a hostile web page cannot use your browser to reach it (see the [Host check](../reference/web-api.md#host-check)). Put it [behind a reverse proxy](../how-to/deploy-docker-compose.md#running-behind-a-reverse-proxy) if that is not what you want.

## Configure

Write your paperless-ngx connection details into `saneless.toml` before you start saneless:

=== "Docker"

    Create a `config` directory. The next step mounts it into the container:

    ```bash
    mkdir config
    ```

    Then create `config/saneless.toml`:

    ```toml
    [paperless]
    url = "http://192.168.1.50:8000"
    token = "your-api-token-here"
    ```

    The container runs as UID 1000 and saves the profiles it generates into this directory, so keep it writable by that user. If `id -u` reports something else, run `sudo chown -R 1000:1000 config` once. Giving a file to another user needs root, so a plain `chown` fails with `Operation not permitted`.

=== "Bare metal"

    Create the per-user config directory, `$XDG_CONFIG_HOME/saneless` (by default `~/.config/saneless`):

    ```bash
    mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/saneless"
    ```

    Then create `saneless.toml` in it:

    ```toml
    [paperless]
    url = "http://192.168.1.50:8000"
    token = "your-api-token-here"
    ```

    If the scanner is attached to another machine running `saned`, also add a `[scanner]` section with `host` set to that machine's address.

Replace the URL and token with your actual paperless-ngx address and API token. [Where saneless reads settings](../reference/configuration.md#where-saneless-reads-settings) lists the other places saneless looks for this file and how environment variables override it, and the [Configuration reference](../reference/configuration.md) covers every option.

## Start saneless

=== "Docker"

    Start the container from the directory that holds `config`, pointing it at your scanner host:

    ```bash
    docker run -d --name saneless -p 8080:8080 \
      --stop-timeout 90 \
      -v "$(pwd)/config:/etc/saneless" \
      -v saneless-data:/var/lib/saneless \
      -e SANELESS_SCANNER__HOST=192.168.1.50 \
      ghcr.io/kdknigga/saneless:0.2
    ```

    Replace `192.168.1.50` with the IP address of the machine running `saned`.

    - The container reads `/etc/saneless/saneless.toml` from the mounted `config` directory.
    - `saneless-data` is a named volume for the job database and any scans kept after a failed upload. Without it they vanish when the container is recreated.
    - `--stop-timeout 90` gives a stop during a scan time to keep the pages scanned so far. Without it Docker kills the container 10 seconds after asking it to stop.
    - On a host with SELinux enforcing (Fedora, RHEL, Rocky), the container cannot read `config` until it is relabelled: write that mount as `-v "$(pwd)/config:/etc/saneless:z"`. See [Docker volumes](../reference/docker.md#volumes).

    !!! note "`SANELESS_SCANNER__HOST` is always required in a container"
        A container never reaches a USB scanner, and the image's `net.conf` is empty, so saneless finds no scanner until this variable names the machine running `saned`, even when that is the container's own host. For a `saned` on the Docker host, see [Which setup do I have?](which-setup.md). For multi-host setups, see [Scanner Host Discovery](../how-to/scanner-host-discovery.md).

=== "Bare metal"

    Install the SANE development headers for your distribution:

    ```bash
    # Debian / Ubuntu
    sudo apt-get install libsane-dev

    # Fedora / RHEL / Rocky
    sudo dnf install sane-backends-devel
    ```

    Then install saneless with pipx. saneless is not yet published on PyPI, so this installs the current default branch from GitHub:

    ```bash
    pipx install git+https://github.com/kdknigga/saneless
    ```

    Start the web server, and leave it running while you use a second terminal for the steps below:

    ```bash
    saneless serve
    ```

## Generate profiles

saneless writes scan profiles from what your scanner reports. Run this after the config file exists, so the profiles are saved into it:

=== "Docker"

    ```bash
    docker exec saneless saneless auto-profiles
    ```

=== "Bare metal"

    ```bash
    saneless auto-profiles
    ```

The output names the file it wrote and lists the profiles, such as `adf` for a scanner with a document feeder. If saneless already generated them when it started, they are listed as already existing, which is fine. Every scanner gets a `default` profile, which the CLI scan below uses.

## Scan

### Web UI

Open `http://localhost:8080` in your browser. Select a profile, enter a title, and click **Scan**. The status area shows progress as the document is scanned, assembled into a PDF, and uploaded to paperless-ngx.

See [First Web UI Scan](first-web-ui-scan.md) for a full walkthrough of every form element.

### CLI

Run a scan from the command line:

=== "Docker"

    ```bash
    docker exec saneless saneless scan --title "My First Scan"
    ```

=== "Bare metal"

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
