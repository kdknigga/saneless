# Which setup do I have?

saneless is deployed in one of three shapes. Find yours below before you install
anything -- the shapes differ in whether you need `saned` and what you set
`SANELESS_SCANNER__HOST` to, and picking the wrong one costs you an hour.

## The one rule about USB

**Only a bare-metal install enumerates a locally attached USB scanner directly**,
through libsane on that machine.

**A container never touches the USB bus.** It always reaches a scanner over the
SANE network protocol -- including a `saned` that runs on the container's own
host. This is why saneless needs no `--privileged` flag and no device mapping:
`saned` owns the scanner, saneless talks to `saned`.

## Shape 1 -- Bare metal, scanner on this machine

*How to tell:* `scanimage -L` on this machine lists your scanner, and you are not
using Docker.

This is the only shape where a locally attached USB scanner works with no `saned`
and no `scanner.host`. Install the SANE headers, install saneless, and scan:

```bash
saneless devices
saneless scan --profile default
```

Leave `scanner.host` empty. See [Install on Bare Metal](../how-to/install-bare-metal.md).

## Shape 2 -- Container, scanner attached to the container's own host

*How to tell:* the scanner's cable goes into the same box that runs Docker.

The container cannot see that scanner directly, so you still need `saned` running
on that box, and `SANELESS_SCANNER__HOST` pointing at it -- either
`host.docker.internal` or the host's LAN address.

=== "Docker Compose"

    ```yaml
    services:
      saneless:
        image: ghcr.io/kdknigga/saneless:latest
        ports:
          - "8080:8080"
        volumes:
          - ./config:/etc/saneless
          - saneless-data:/var/lib/saneless
        environment:
          - TZ=America/Chicago
          - SANELESS_SCANNER__HOST=host.docker.internal

    volumes:
      saneless-data:
    ```

=== "docker run"

    ```bash
    docker run -p 8080:8080 \
      -v "$(pwd)/config:/etc/saneless" \
      -v saneless-data:/var/lib/saneless \
      -e SANELESS_SCANNER__HOST=host.docker.internal \
      ghcr.io/kdknigga/saneless:latest
    ```

## Shape 3 -- Container, scanner on another machine

*How to tell:* the scanner is plugged into a different machine that runs `saned`,
or it speaks SANE over the network itself.

Identical to shape 2 apart from the address: point `SANELESS_SCANNER__HOST` at the
machine that has the scanner.

=== "Docker Compose"

    ```yaml
    services:
      saneless:
        image: ghcr.io/kdknigga/saneless:latest
        ports:
          - "8080:8080"
        volumes:
          - ./config:/etc/saneless
          - saneless-data:/var/lib/saneless
        environment:
          - TZ=America/Chicago
          - SANELESS_SCANNER__HOST=192.168.1.50

    volumes:
      saneless-data:
    ```

=== "docker run"

    ```bash
    docker run -p 8080:8080 \
      -v "$(pwd)/config:/etc/saneless" \
      -v saneless-data:/var/lib/saneless \
      -e SANELESS_SCANNER__HOST=192.168.1.50 \
      ghcr.io/kdknigga/saneless:latest
    ```

## Notes on the container lines above

- **Mount the config *directory*, never the file inside it.** saneless saves
  generated profiles by renaming a temporary file over `saneless.toml`, and a
  single-file bind mount makes that rename fail. Put your `saneless.toml` inside
  `./config` and keep the directory writable.
- **The container's port is fixed at 8080.** Change the left-hand half of the
  mapping to serve it elsewhere, for example `-p 8888:8080`.
- **The image runs as UID 1000.** If `id -u` on your host reports something else,
  run `chown -R 1000:1000 ./config` once. See [Docker](../reference/docker.md).
- **The data volume is part of the minimum** -- without it the job database and
  any preserved scans vanish when the container is recreated.

Next: [Quick Start](quick-start.md), or, for multi-host and multi-scanner setups,
[Scanner Host Discovery (Containers)](../how-to/scanner-host-discovery.md).
