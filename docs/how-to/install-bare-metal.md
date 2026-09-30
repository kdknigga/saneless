# Install on Bare Metal

Install saneless directly on a Linux host with a SANE-compatible scanner.

## What you'll need

- A Linux system (Debian/Ubuntu, Fedora/RHEL/Rocky, Arch, or Alpine)
- Python 3.14 or later
- A SANE-compatible scanner accessible via `saned` on the network or locally via USB
- A running paperless-ngx instance with an API token: paperless-ngx 2.16 or later, which speaks API version 9 or 10

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
token = "your-api-token-here"

[profiles.default]
source = "Flatbed"
resolution = 300
mode = "Color"
```

See [Configure Scan Profiles](configure-scan-profiles.md) for more profile options.

If `auto-profiles` creates the config file from scratch -- you ran `saneless auto-profiles` before writing one -- it lands in `$XDG_CONFIG_HOME/saneless/saneless.toml` (by default `~/.config/saneless/saneless.toml`), or in `/etc/saneless/saneless.toml` when the `/etc/saneless` directory already exists and you can write to it. It never lands in `./saneless.toml` in the working directory, which would outrank both on the next start. Add the `[paperless]` table above to that file.

If you run `sudo saneless auto-profiles` and `/etc/saneless` belongs to root, the new file is owned by root with mode `0600`. saneless running as your own user then finds that file on the next start and cannot read it, so every command stops with a configuration error. The command prints a note naming the file when this happens. Before you start saneless, give the file to the user saneless runs as; for your own user:

```bash
sudo chown "$USER": /etc/saneless/saneless.toml
```

Without an `/etc/saneless` directory, `sudo saneless auto-profiles` creates root's own per-user file instead, usually `/root/.config/saneless/saneless.toml`. saneless running as any other user never reads that file, and a `chown` does not change that, so the note printed then gives the commands that move it into `/etc/saneless` and give it to that user. Alternatively, run `saneless auto-profiles` as the user saneless runs as. If `sudo` keeps your `HOME` (`sudo -E`), the file goes to your own `~/.config/saneless` instead, and the directories it creates there and the file itself take the owner of the directory they are created in, so they stay yours.

Scans in progress are written under `tmp_dir`, by default `$TMPDIR/saneless-<uid>` (for example `/tmp/saneless-1000`), which saneless creates so that only your user can enter it; see [`[output]`](../reference/configuration.md#output) for what it refuses at startup. Upgrading from an earlier release? The old `/tmp/saneless` directory is no longer used and may be deleted, once any `saneless.db` a release older than `data_dir` left in it has been moved to `data_dir`.

### Upgrading from an earlier release

This release checks more of your config file when saneless starts, and changes how a few settings are read:

- **A `0` or out-of-range number now stops saneless loading**, with exit 2 and a line naming the key; so does `true` or `false` where a number belongs. Each numeric setting's range is in the [configuration reference](../reference/configuration.md#output). There is no "no limit" value: replace `history_retention_days = 0` with `history_retention_days = 36500` to keep history for about a century, and replace `history_max_rows = 0`, which erased the history, with the number of jobs to keep, up to `1000000`.
- **Relative paths now follow the config file.** A relative `data_dir`, `tmp_dir`, `log_file` or `consume_dir` is resolved against the directory of the config file that loaded, not the directory you started saneless from, so every command finds the same job database wherever you run it. Write the path absolute to keep the old location; nothing is moved for you.
- **Generated profile names change only when you ask.** Profiles generated earlier keep their old `label` and `description` until you run `saneless auto-profiles --force`, which rewrites only the tables marked `auto_generated = true` and keeps your other keys. See [Auto-generated profiles](configure-scan-profiles.md#auto-generated-profiles).
- **Free space is counted in decimal megabytes.** `min_free_space_mb` counts a megabyte as 1,000,000 bytes, so the same number reserves about 5% less than before.
- **The job database gains an index when saneless opens it.** Its schema version does not change, so an earlier release still opens it if you roll back.
- **`default_title` in a profile is refused; write `title`.** An earlier release also loaded a profile's title spelled `default_title`. That spelling now stops saneless with exit 2 and a line saying to write it as `title`.

This release needs paperless-ngx 2.16 or later, which speaks API version 9 or 10; an older paperless-ngx refuses every upload with `406`, and the status strip reports `incompatible_version`. [Upgrading: paperless-ngx 2.16 or later](deploy-docker-compose.md#upgrading-paperless-ngx-216-or-later) lists what else changed about delivery and a scan's metadata; it applies to a bare-metal install too.

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
- If saneless's log says `Scanner enumeration failed: ModuleNotFoundError`, python-sane is reachable only through `PYTHONPATH`. saneless lists scanners in a separate Python process that ignores `PYTHONPATH`, so python-sane must be installed in the same environment as saneless: reinstall saneless as in [Step 2](#step-2-install-saneless)

**Permission denied accessing scanner**

Add your user to the `scanner` group:

```bash
sudo usermod -aG scanner $USER
```

Log out and back in for the group change to take effect.

For a scan that fails after saneless is installed, start from its exit code in [Troubleshoot a Failed Scan](troubleshoot-a-failed-scan.md).
