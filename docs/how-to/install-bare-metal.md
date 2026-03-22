# Install on Bare Metal

Install saneless directly on a Linux host with a SANE-compatible scanner.

## What you'll need

- A Linux system (Debian/Ubuntu, Fedora/RHEL/Rocky, Arch, or Alpine)
- Python 3.14 or later
- A SANE-compatible scanner accessible via `saned` on the network or locally via USB
- A running paperless-ngx instance with an API token

## Step 1: Install SANE development headers

saneless depends on `python-sane`, which compiles against SANE's C library. Install the development headers for your distribution:

| Distribution | Command |
|---|---|
| Debian / Ubuntu | `sudo apt-get install libsane-dev` |
| Fedora / RHEL / Rocky | `sudo dnf install sane-backends-devel` |
| Arch | `sudo pacman -S sane` |
| Alpine | `apk add sane-dev` |

## Step 2: Install saneless

=== "pipx (recommended)"

    [pipx](https://pipx.pypa.io/) installs saneless in an isolated virtual environment, avoiding dependency conflicts:

    ```bash
    pipx install saneless
    ```

=== "pip"

    ```bash
    pip install saneless
    ```

!!! tip
    Prefer `pipx` if you have it installed. It keeps saneless and its dependencies isolated from your system Python packages.

## Step 3: Verify the installation

Check that saneless is installed and can find your scanner:

```bash
saneless --version
```

Then discover available scanners:

```bash
saneless devices
```

You should see output listing your scanner's name, vendor, model, and type. If you see "No scanners found", check the troubleshooting section below.

## Step 4: Create a configuration file

Create `saneless.toml` in your working directory (or `~/.config/saneless/config.toml`):

```toml
[paperless]
url = "http://paperless.local:8000"
token = "your-paperless-api-token"

[profiles.default]
source = "Flatbed"
resolution = 300
mode = "Color"
```

See [Configure Scan Profiles](configure-scan-profiles.md) for more profile options.

## Troubleshooting

**`python-sane` fails to compile**

The SANE development headers are missing. Install the package for your distribution from the table in Step 1, then retry `pip install saneless`.

**"No scanners found"**

- Verify your scanner is visible to SANE directly: `scanimage -L`
- If using a network scanner via `saned`, ensure `saned` is running on the scanner host and your machine is in its access list
- Check that the scanner is powered on and connected

**Permission denied accessing scanner**

Add your user to the `scanner` group:

```bash
sudo usermod -aG scanner $USER
```

Log out and back in for the group change to take effect.
