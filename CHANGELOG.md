# Changelog

All notable changes to saneless are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and saneless uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The first release, 0.2.0.

### Added

- A web UI for scanning from any device on the local network. A status strip
  checks the configuration, the scanner, paperless-ngx, the profiles, the
  fallback folder and the data folder, and the scan's status updates live. Only
  the browser that started a scan sees its title, preview and error text. See
  [First Web UI Scan](https://kdknigga.github.io/saneless/getting-started/first-web-ui-scan/).
- The `saneless` command line: `scan`, `devices`, `jobs`, `serve`,
  `auto-profiles` and `doctor`, with documented exit codes for scripting. See
  [CLI Commands](https://kdknigga.github.io/saneless/reference/cli-commands/).
- Scan profiles in `saneless.toml`, and `saneless auto-profiles`, which writes
  them from what the scanner reports. See
  [Configure Scan Profiles](https://kdknigga.github.io/saneless/how-to/configure-scan-profiles/).
- Duplex scanning through the document feeder, in hardware or by flipping the
  stack by hand, and multi-page documents built from several scans into one
  PDF. See
  [Set Up ADF Duplex Scanning](https://kdknigga.github.io/saneless/how-to/set-up-adf-duplex/)
  and
  [Scan a Multi-Page Document](https://kdknigga.github.io/saneless/how-to/scan-a-multi-page-document/).
- Empty-page detection, which drops blank pages from every scan, flatbed or
  feeder. See
  [Empty Page Detection](https://kdknigga.github.io/saneless/explanation/empty-page-detection/).
- A consume-directory fallback that saves the PDF to a folder paperless-ngx
  watches when the paperless-ngx API cannot take the upload. See
  [Consume Directory Fallback](https://kdknigga.github.io/saneless/explanation/consume-directory-fallback/).
- A container image published to the GitHub Container Registry, with build
  provenance and an SBOM you can verify. See
  [Docker](https://kdknigga.github.io/saneless/reference/docker/).
- This documentation site, at
  [kdknigga.github.io/saneless](https://kdknigga.github.io/saneless/).

### Changed

- Some log lines carry a new logger name, so a log filter or alert keyed on the
  name the release candidates used needs updating:
  - the `Auto-profiles:` lines from start-up profile generation now come from
    `saneless.startup_profiles` (was `saneless.worker`); the
    `Auto-profiles: startup generation failed` error still comes from
    `saneless.worker`;
  - the manual-duplex warnings about mismatched resolutions and a backs pass
    stopped at its page cap come from `saneless.duplex` (was
    `saneless.pipeline`);
  - the saned pre-probe lines come from `saneless.scanner.saned_probe` (was
    `saneless.checks`);
  - the warning that a paperless-ngx list could not be fetched comes from
    `saneless.web.metadata_view` (was `saneless.web.routes`), and reads
    "answering unavailable" where it read "using empty list";
  - the debug line saying whether a prompt answer's owner matched comes from
    `saneless.web.owner` (was `saneless.web.routes`), and starts
    "Answer for job" where it started "Flip answer for job".

  Two error lines that no release candidate logged come from the view modules:
  the lazy list load's failure to read the job from
  `saneless.web.metadata_view`, and the failure to render the job status from
  `saneless.web.status_view`.

### Removed

- `saneless.Settings` is no longer re-exported from the package, so importing
  `saneless` does not load the settings stack; import it from `saneless.config`.
- `saneless.web` no longer re-exports `create_app`; import it from
  `saneless.web.app`.

### Security

- The web UI has no login, by design: it is an appliance for a trusted local
  network. See
  [What an unauthenticated client can read](https://kdknigga.github.io/saneless/reference/web-api/#what-an-unauthenticated-client-can-read).
- A Host check answers only requests that name saneless's own host names, which
  stops DNS rebinding. See
  [Host check](https://kdknigga.github.io/saneless/reference/web-api/#host-check).
- Same-origin checks reject cross-site requests that change state. See
  [Cross-site requests](https://kdknigga.github.io/saneless/reference/web-api/#cross-site-requests).
- The paperless-ngx token is kept out of logs and error messages: text about a
  paperless-ngx request has the token struck out before it is shown or logged,
  and a config error names the setting, never its value.

[Unreleased]: https://github.com/kdknigga/saneless/commits/master
