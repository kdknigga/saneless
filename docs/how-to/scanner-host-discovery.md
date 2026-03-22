# Scanner Host Discovery (Containers)

Configure saneless running inside a Docker container to detect network scanners via SANE's net backend.

## What you'll need

- A network scanner accessible via `saned` (SANE network daemon)
- saneless running in Docker ([Deploy with Docker Compose](deploy-docker-compose.md))
- The IP address of the machine running `saned` (e.g., `192.168.1.50`)

## The problem

Docker containers cannot access USB scanners attached to the host. Network scanners require SANE's net backend to know which hosts to probe, but containers do not inherit the host's `/etc/sane.d/net.conf`.

## The solution

Set the scanner host in saneless configuration. saneless injects the value into the `SANE_NET_HOSTS` environment variable before initializing SANE, so the net backend discovers scanners on the specified host(s).

### Method 1: Configuration file

Add the scanner host to your `config.toml`:

```toml
[scanner]
host = "192.168.1.50"
```

### Method 2: Environment variable

Set `SANELESS_SCANNER__HOST` in your `docker-compose.yml`:

```yaml
services:
  saneless:
    image: ghcr.io/kris-knigga/saneless:latest
    environment:
      - SANELESS_SCANNER__HOST=192.168.1.50
```

### Multiple scanner hosts

Separate multiple hosts with colons:

```toml
[scanner]
host = "192.168.1.50:192.168.1.51"
```

Or via environment variable:

```yaml
environment:
  - SANELESS_SCANNER__HOST=192.168.1.50:192.168.1.51
```

## Priority rules

If `SANE_NET_HOSTS` is already set externally (for example, passed directly in your Docker Compose environment), it takes priority over the `scanner.host` config value. saneless will not overwrite an externally-set `SANE_NET_HOSTS`.

## Verifying scanner discovery

After starting the container, verify that scanners are discovered:

```bash
docker exec saneless saneless devices
```

You should see your network scanner listed with its name, vendor, model, and type.

## Troubleshooting

**Scanner not found**

- Verify `saned` is running on the scanner host: `systemctl status saned.socket` (or `saned` service)
- Check that `saned` allows connections from the Docker network. Edit `/etc/sane.d/saned.conf` on the scanner host and add the Docker subnet (e.g., `172.17.0.0/16`)
- Verify the scanner host is reachable from the container: `docker exec saneless ping 192.168.1.50`

**Firewall blocking connections**

SANE's net backend uses TCP port 6566. Ensure this port is open on the scanner host:

```bash
# firewalld (Fedora/RHEL/Rocky)
sudo firewall-cmd --add-port=6566/tcp --permanent
sudo firewall-cmd --reload

# ufw (Debian/Ubuntu)
sudo ufw allow 6566/tcp
```

**Scanner host not in net mode**

Some scanners need to be explicitly configured in `saned` for network sharing. Check that the scanner appears in `scanimage -L` on the scanner host itself before attempting network discovery.
