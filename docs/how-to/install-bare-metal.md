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

saneless is not yet published on PyPI, so both commands below install the current default branch from GitHub.

=== "pipx (recommended)"

    [pipx](https://pipx.pypa.io/) installs saneless in an isolated virtual environment, avoiding dependency conflicts:

    ```bash
    pipx install git+https://github.com/kdknigga/saneless
    ```

=== "pip"

    ```bash
    pip install git+https://github.com/kdknigga/saneless
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

Create `saneless.toml` in your working directory (or `$XDG_CONFIG_HOME/saneless/saneless.toml`, default `~/.config/saneless/saneless.toml`):

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

Scans in progress are written under `tmp_dir`, by default `$TMPDIR/saneless-<uid>` (for example `/tmp/saneless-1000`), which saneless creates so that only your user can enter it; see [`[output]`](../reference/configuration.md#output) for what it refuses at startup. Upgrading from an earlier release? The old `/tmp/saneless` directory is no longer used and may be deleted, once any `saneless.db` a release older than `data_dir` left in it has been moved to `data_dir`.

### Upgrading from an earlier release

saneless now keeps what it writes under `data_dir` (by default `~/.local/state/saneless`) to your user. When it creates `data_dir`, `failed/` or a preserved page directory, it creates it `0700`. A new job database (`saneless.db`, with its `-wal` and `-shm` files) and each preserved PDF and page file are `0600`.

Files and directories an earlier release created keep their modes: saneless does not change them at startup. To tighten them, run this once, with `DATA_DIR` set to your `data_dir`:

```bash
DATA_DIR=~/.local/state/saneless
chmod 700 "$DATA_DIR" "$DATA_DIR/failed"
chmod 600 "$DATA_DIR"/saneless.db*
find "$DATA_DIR/failed" -mindepth 1 -type d -exec chmod 700 {} +
find "$DATA_DIR/failed" -type f -exec chmod 600 {} +
```

`chmod` reports an error for a path that does not exist yet, such as `failed/` before any scan has been preserved; that path needs nothing.

## Troubleshooting

**`python-sane` fails to compile**

The SANE development headers are missing. Install the package for your distribution from the table in Step 1, then retry the install command from [Step 2](#step-2-install-saneless).

**saneless says python-sane cannot be imported**

`scan`, `devices`, `auto-profiles` and `serve` each exit with code 2 at once, printing one line with the reason the import failed and an install hint naming the missing SANE development package. python-sane is missing, or it cannot load the SANE library. Install the package from [Step 1](#step-1-install-sane-development-headers) and reinstall saneless. `saneless jobs` and `--help` keep working meanwhile.

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

For a scan that fails after saneless is installed, start from its exit code in [Troubleshoot a Failed Scan](troubleshoot-a-failed-scan.md).
