# Install on Bare Metal

Install saneless directly on a Linux host with a SANE-compatible scanner.

## What you'll need

- A Linux system (Debian/Ubuntu, Fedora/RHEL/Rocky, Arch, or Alpine)
- Python 3.14 or later
- Git: pip fetches saneless from its GitHub repository, and runs `git` to do it
- A SANE-compatible scanner accessible via `saned` on the network or locally via USB
- A running paperless-ngx instance with an API token: paperless-ngx 2.16 or later, which speaks API version 9 or 10. An older paperless-ngx refuses every upload with `406`, and the status strip reports `incompatible_version`

## Step 1: Install Git and the SANE development headers

saneless depends on `python-sane`, which compiles against SANE's C library, and is installed from GitHub, which needs `git`. Install both for your distribution:

| Distribution | Command |
|---|---|
| Debian / Ubuntu | `sudo apt-get install git libsane-dev` |
| Fedora / RHEL / Rocky | `sudo dnf install git sane-backends-devel` |
| Arch | `sudo pacman -S git sane` |
| Alpine | `apk add git sane-dev` |

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

Create `saneless.toml` in your working directory (or `$XDG_CONFIG_HOME/saneless/saneless.toml`, default `~/.config/saneless/saneless.toml`; [Where saneless reads settings](../reference/configuration.md#where-saneless-reads-settings) lists every place saneless looks):

```toml
[paperless]
url = "http://paperless.local:8000"
token = "your-api-token-here"
```

Then, from the same directory, write scan profiles into that file from what your scanner reports:

```bash
saneless auto-profiles
```

Leave `[profiles.default]` out of the file you write by hand: a hand-written default turns off the profile generation `saneless serve` runs at startup. See [Configure Scan Profiles](configure-scan-profiles.md) for more profile options.

If `auto-profiles` creates the config file from scratch -- you ran it before writing one -- it lands in `$XDG_CONFIG_HOME/saneless/saneless.toml` (by default `~/.config/saneless/saneless.toml`), or in `/etc/saneless/saneless.toml` when the `/etc/saneless` directory already exists and you can write to it, and never in `./saneless.toml`. Add the `[paperless]` table above to that file.

If you run `sudo saneless auto-profiles` and `/etc/saneless` belongs to root, the new file is owned by root with mode `0600`. saneless running as your own user then finds that file on the next start and cannot read it, so every command stops with a configuration error. The command prints a note naming the file when this happens. Before you start saneless, give the file to the user saneless runs as; for your own user:

```bash
sudo chown "$USER": /etc/saneless/saneless.toml
```

Without an `/etc/saneless` directory, `sudo saneless auto-profiles` creates root's own per-user file instead, usually `/root/.config/saneless/saneless.toml`. saneless running as any other user never reads that file, and a `chown` does not change that, so the note printed then gives the commands that move it into `/etc/saneless` and give it to that user. Alternatively, run `saneless auto-profiles` as the user saneless runs as. If `sudo` keeps your `HOME` (`sudo -E`), the file goes to your own `~/.config/saneless` instead, and the directories it creates there and the file itself take the owner of the directory they are created in, so they stay yours.

Scans in progress are written under `tmp_dir`, by default `$TMPDIR/saneless-<uid>` (for example `/tmp/saneless-1000`), which saneless creates so that only your user can enter it; see [`[output]`](../reference/configuration.md#output) for what it refuses at startup.

Job history and any scan saneless could not deliver are kept under `data_dir`, by default `~/.local/state/saneless`. When saneless creates `data_dir`, `failed/` or a preserved page directory, it creates it `0700`. A new job database (`saneless.db`, with its `-wal` and `-shm` files) and each preserved PDF and page file are `0600`.

## Step 5: Start the web server

```bash
saneless serve
```

It prints the address it serves on, `0.0.0.0:8080` unless you change `web_host` and `web_port` in [`[output]`](../reference/configuration.md#output) or pass `--host` and `--port`. Ctrl-C or SIGTERM stops it gracefully, with exit code 0.

If you run `saneless serve` as a systemd service, set `TimeoutStopSec=90` in the unit's `[Service]` section. systemd kills a service that is still running when its stop timeout runs out, and the default timeout depends on the distribution and the system's `DefaultTimeoutStopSec`. A stop during a scan may need up to 90 seconds to keep the pages scanned so far, the same time the shipped `docker-compose.yml` gives the container with `stop_grace_period: 90s`.

## Troubleshooting

**`python-sane` fails to compile**

The SANE development headers are missing. Install the package for your distribution from the table in Step 1, then retry the install command from [Step 2](#step-2-install-saneless).

**saneless says python-sane cannot be imported**

`scan`, `devices`, `auto-profiles` and `serve` each exit with code 2 at once, printing a line with the reason the import failed and an install hint naming the missing SANE development package, then a `Try:` line saying to install it and reinstall saneless. python-sane is missing, or it cannot load the SANE library. Install the package from [Step 1](#step-1-install-git-and-the-sane-development-headers) and reinstall saneless. `saneless jobs` and `--help` keep working meanwhile.

**"No scanners found"**

- Verify your scanner is visible to SANE directly: `scanimage -L`
- If using a network scanner via `saned`, ensure `saned` is running on the scanner host and your machine is in its access list
- Check that the scanner is powered on and connected
- If saneless's log says `Scanner enumeration failed: ModuleNotFoundError`, python-sane is reachable only through `PYTHONPATH`. saneless lists scanners in a separate Python process that ignores `PYTHONPATH`, so python-sane must be installed in the same environment as saneless: reinstall saneless as in [Step 2](#step-2-install-saneless)

**Permission denied accessing scanner**

Add your user to the `scanner` group:

```bash
sudo usermod -aG scanner $USER
```

Log out and back in for the group change to take effect.

For a scan that fails after saneless is installed, start from its exit code in [Troubleshoot a Failed Scan](troubleshoot-a-failed-scan.md).
