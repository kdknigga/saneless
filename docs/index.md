# saneless

SANE scanner to paperless-ngx bridge. Web UI and CLI for triggering scans, assembling multi-page PDFs, and ingesting documents into paperless-ngx with metadata -- all from any device on the local network.

---

## Tutorials

**New to saneless? Start here.** These tutorials walk you through the basics from installation to your first successful scan.

- [Scan Your First Document](tutorials/scan-your-first-document.md) -- Install saneless, configure your scanner and paperless-ngx, scan a document, and verify it arrived.

## How-To Guides

**Step-by-step instructions for specific tasks.** Each guide solves one problem and assumes you already have saneless running.

- [Install on Bare Metal](how-to/install-bare-metal.md) -- Install saneless directly on a Linux host with system SANE headers.
- [Deploy with Docker Compose](how-to/deploy-docker-compose.md) -- Run saneless as a container alongside paperless-ngx.
- [Configure Scan Profiles](how-to/configure-scan-profiles.md) -- Create profiles for different scan types (color, grayscale, high-res).
- [Set Up ADF Duplex Scanning](how-to/set-up-adf-duplex.md) -- Scan double-sided documents with an automatic document feeder.
- [Scanner Host Discovery (Containers)](how-to/scanner-host-discovery.md) -- Connect a containerized saneless instance to a remote scanner.
- [Use the CLI for Scripting](how-to/cli-scripting.md) -- Automate scans with shell scripts and JSON output.

## Reference

**Technical details: CLI, configuration, API, Docker.** Look up exact option names, defaults, and behaviors.

- [CLI Commands](reference/cli-commands.md) -- All commands, flags, and exit codes.
- [Configuration (TOML)](reference/configuration.md) -- Every configuration key, its type, default, and description.
- [Environment Variables](reference/environment-variables.md) -- Override any config value via environment variables.
- [Web API](reference/web-api.md) -- HTTP endpoints for scanning, status, and health checks.
- [Docker](reference/docker.md) -- Image tags, volumes, environment variables, and health checks.

## Explanation

**Background and design decisions.** Understand why saneless works the way it does.

- [Architecture Overview](explanation/architecture.md) -- How the scanner, pipeline, worker, and web layer fit together.
- [Empty Page Detection](explanation/empty-page-detection.md) -- How saneless detects and removes blank pages from ADF scans.
- [Consume Directory Fallback](explanation/consume-directory-fallback.md) -- How file-based ingestion works when the paperless-ngx API is unavailable.
