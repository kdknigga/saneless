# saneless

SANE scanner to paperless-ngx bridge. Web UI and CLI for triggering scans, assembling multi-page PDFs, and ingesting documents into paperless-ngx with metadata.

## System Requirements

`python-sane` requires the SANE development headers to compile:

| Distro | Package |
|--------|---------|
| Debian / Ubuntu | `libsane-dev` |
| Fedora / RHEL / Rocky | `sane-backends-devel` |
| Arch | `sane` |
| Alpine | `sane-dev` |

Install them before installing saneless:

```bash
# Debian/Ubuntu
sudo apt-get install libsane-dev

# Fedora/RHEL/Rocky
sudo dnf install sane-backends-devel
```

## Install

saneless is not yet published on PyPI, so this installs the current default
branch from GitHub:

```bash
pip install git+https://github.com/kdknigga/saneless
```

## Docker

The Docker image includes `libsane` and handles `python-sane` compilation automatically.

First create a `config` directory and put your paperless-ngx details in `config/saneless.toml`:

```bash
mkdir config
```

```toml
[paperless]
url = "https://paperless.example.com"
token = "your-api-token-here"
```

Then start the container from the directory that holds `config`:

```bash
docker run -d --name saneless -p 8080:8080 \
  --stop-timeout 90 \
  -v "$(pwd)/config:/etc/saneless" \
  -v saneless-data:/var/lib/saneless \
  -e SANELESS_SCANNER__HOST=192.168.1.50 \
  ghcr.io/kdknigga/saneless:0.2.0-rc.6
```

- The container reads `/etc/saneless/saneless.toml` from the mounted `config` directory. Keep the directory writable by UID 1000, the user the container runs as, so saneless can save the profiles it generates there.
- `SANELESS_SCANNER__HOST` names the machine running `saned`. A container always needs it, because it reaches scanners only over the network.
- `saneless-data` is a named volume for the job database and any scans kept after a failed upload.
- `--stop-timeout 90` gives a stop during a scan time to keep the pages scanned so far, as `stop_grace_period: 90s` does in the shipped `docker-compose.yml`.

Open `http://localhost:8080` to scan. `docker exec saneless saneless doctor` checks the setup, and `docker exec saneless saneless auto-profiles` writes scan profiles into `config/saneless.toml` from what your scanner reports.

For a permanent setup, use [Deploy with Docker Compose](https://kdknigga.github.io/saneless/how-to/deploy-docker-compose/).

## Usage

```bash
saneless devices                      # List available SANE scanners
saneless auto-profiles                # Write scan profiles from what the scanner reports
saneless doctor                       # Check that saneless is ready to scan
saneless scan --title "Some Document" # Scan a document with the default profile
saneless scan --profile adf-duplex --title "Invoice" # A profile auto-profiles writes for a duplex feeder
saneless serve                        # Start web UI
saneless jobs                         # View job history
```

## Configuration

saneless reads its settings from `saneless.toml`; in the container that is `/etc/saneless/saneless.toml`. A minimal file:

```toml
[scanner]
host = "192.168.1.100"

[paperless]
url = "https://paperless.example.com"
token = "your-api-token-here"
```

Scan profiles come from `saneless auto-profiles`, which writes them into the same file from what your scanner reports. Where saneless looks for the file, and how `SANELESS_<SECTION>__<FIELD>` environment variables override it, is in [Where saneless reads settings](https://kdknigga.github.io/saneless/reference/configuration/#where-saneless-reads-settings).

## Documentation

Full documentation is available at **[kdknigga.github.io/saneless](https://kdknigga.github.io/saneless/)**.

- [Your First CLI Scan](https://kdknigga.github.io/saneless/getting-started/first-cli-scan/) -- step-by-step tutorial
- [How-To Guides](https://kdknigga.github.io/saneless/how-to/install-bare-metal/) -- installation, Docker, profiles, duplex, CLI
- [Configuration Reference](https://kdknigga.github.io/saneless/reference/configuration/) -- all TOML options and defaults
- [CLI Reference](https://kdknigga.github.io/saneless/reference/cli-commands/) -- commands, flags, exit codes
