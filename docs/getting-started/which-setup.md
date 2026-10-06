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
        image: ghcr.io/kdknigga/saneless:0.2
        stop_grace_period: 90s
        ports:
          - "8080:8080"
        volumes:
          - ./config:/etc/saneless
          - saneless-data:/var/lib/saneless
        environment:
          - TZ=America/Chicago
          - SANELESS_SCANNER__HOST=host.docker.internal
        extra_hosts:
          - "host.docker.internal:host-gateway"

    volumes:
      saneless-data:
    ```

=== "docker run"

    ```bash
    docker run -d --name saneless -p 8080:8080 \
      --stop-timeout 90 \
      --add-host=host.docker.internal:host-gateway \
      -v "$(pwd)/config:/etc/saneless" \
      -v saneless-data:/var/lib/saneless \
      -e SANELESS_SCANNER__HOST=host.docker.internal \
      ghcr.io/kdknigga/saneless:0.2
    ```

On Linux Docker Engine, `host.docker.internal` resolves only because the
`host-gateway` line above maps it to the host; Docker Desktop provides the name
by itself.

`saned` refuses every client its `/etc/sane.d/saned.conf` does not list, and the
container connects from an address on its Docker network, not from the host's LAN
address. Print that network's subnet:

=== "Docker Compose"

    ```bash
    docker network inspect <project>_default --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
    ```

    Compose names the network after the project, which defaults to the name of
    the directory holding `docker-compose.yml`: a project in `saneless/` gets
    `saneless_default`.

=== "docker run"

    ```bash
    docker network inspect bridge --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
    ```

Add the subnet it prints, such as `172.17.0.0/16`, as a line of its own in
`/etc/sane.d/saned.conf` on this host. See
[Scanner Host Discovery](../how-to/scanner-host-discovery.md#what-the-scanner-row-says)
if the Scanner row still says the host is refusing this machine.

## Shape 3 -- Container, scanner on another machine

*How to tell:* the scanner is plugged into a different machine that runs `saned`,
or it speaks SANE over the network itself.

Identical to shape 2 apart from the address: point `SANELESS_SCANNER__HOST` at the
machine that has the scanner.

=== "Docker Compose"

    ```yaml
    services:
      saneless:
        image: ghcr.io/kdknigga/saneless:0.2
        stop_grace_period: 90s
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
    docker run -d --name saneless -p 8080:8080 \
      --stop-timeout 90 \
      -v "$(pwd)/config:/etc/saneless" \
      -v saneless-data:/var/lib/saneless \
      -e SANELESS_SCANNER__HOST=192.168.1.50 \
      ghcr.io/kdknigga/saneless:0.2
    ```

## Notes on the container lines above

- **Mount the config *directory*, never the file inside it.** saneless saves
  generated profiles by renaming a temporary file over `saneless.toml`, and a
  single-file bind mount makes that rename fail. Put your `saneless.toml` inside
  `./config` and keep the directory writable.
- **The container's port is fixed at 8080.** Change the left-hand half of the
  mapping to serve it elsewhere, for example `-p 8888:8080`.
- **The image runs as UID 1000.** If `id -u` on your host reports something else,
  run `sudo chown -R 1000:1000 ./config` once; a plain `chown` to another user
  fails without root. See [Docker](../reference/docker.md).
- **`--stop-timeout 90` belongs on every `docker run`.** Docker kills a
  container 10 seconds after asking it to stop unless told otherwise, and a stop
  during a scan may need longer to keep the pages scanned so far. The shipped
  `docker-compose.yml` sets the same budget as `stop_grace_period: 90s`.
- **The data volume is part of the minimum** -- without it the job database and
  any preserved scans vanish when the container is recreated.

Next: [Quick Start](quick-start.md), or, for multi-host and multi-scanner setups,
[Scanner Host Discovery (Containers)](../how-to/scanner-host-discovery.md).
