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

Install before `pip install saneless`:

```bash
# Debian/Ubuntu
sudo apt-get install libsane-dev

# Fedora/RHEL/Rocky
sudo dnf install sane-backends-devel
```

## Install

```bash
pip install saneless
```

## Docker

The Docker image includes `libsane` and handles `python-sane` compilation automatically:

```bash
docker run -p 8080:8080 ghcr.io/kris-knigga/saneless
```

## Usage

```bash
saneless devices          # List available SANE scanners
saneless scan             # Scan a document
saneless scan --profile duplex --title "Invoice"
saneless serve            # Start web UI
saneless jobs             # View job history
```

## Configuration

Create `saneless.toml`:

```toml
[scanner]
host = "192.168.1.100"

[paperless]
url = "https://paperless.example.com"
token = "your-api-token"

[profiles.default]
source = "flatbed"
resolution = 300
mode = "color"
```

Or use environment variables: `SANELESS_SCANNER__HOST`, `SANELESS_PAPERLESS__URL`, etc.

## Documentation

Full documentation is available at **[saneless.github.io](https://kris-knigga.github.io/saneless/)**.

- [Scan Your First Document](https://kris-knigga.github.io/saneless/tutorials/scan-your-first-document/) -- step-by-step tutorial
- [How-To Guides](https://kris-knigga.github.io/saneless/how-to/install-bare-metal/) -- installation, Docker, profiles, duplex, CLI
- [Configuration Reference](https://kris-knigga.github.io/saneless/reference/configuration/) -- all TOML options and defaults
- [CLI Reference](https://kris-knigga.github.io/saneless/reference/cli-commands/) -- commands, flags, exit codes
