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

Add the scanner host to your `saneless.toml`:

```toml
[scanner]
host = "192.168.1.50"
```

### Method 2: Environment variable

Set `SANELESS_SCANNER__HOST` in your `docker-compose.yml`:

```yaml
services:
  saneless:
    image: ghcr.io/kdknigga/saneless:0.2.0-rc.6
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

If `SANE_NET_HOSTS` is already set externally to a non-empty value (for example, passed directly in your Docker Compose environment), it takes priority over the `scanner.host` config value. saneless will not overwrite an externally-set `SANE_NET_HOSTS`.

An exported but empty `SANE_NET_HOSTS` (for example `SANE_NET_HOSTS=` left in a Compose file) is treated as unset, so `scanner.host` is used. The Scanner check in the status strip and in `saneless doctor` follows the same rule, so it checks the hosts SANE will actually dial.

When `[scanner] device` is set, the Scanner check looks for that device by name in the list SANE returns, rather than taking whichever device is listed first. SANE can open some devices it never lists, for example an `escl:` URL with no matching `escl.conf` entry. If SANE does not list the configured device, the check opens it once, as a scan would, and reports it ready if that works. With `[scanner] device` empty, the check uses the first device listed, which is the one a scan would use.

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

**What the Scanner row says**
{: #what-the-scanner-row-says }

Before it asks SANE for scanners, the Scanner check in the status strip and in `saneless doctor` opens a connection to `saned` on each configured scanner host and starts the SANE network handshake, so it can tell why a host is not usable. Each row below names the problem, and its next step says what fixes it. "Pressing **Check again**" means the status strip's **Check again** button; `saneless doctor` checks afresh every time it runs.

| The row says | What it means | What fixes it | Cleared by |
|--------------|---------------|---------------|------------|
| "The scanner host is not answering, so the scanner could not be checked." | Nothing answered on port 6566 in time: the scanner host is switched off or off the network, a firewall is dropping the connection, or something accepted the connection and never replied. | Switch the scanner host on and check its network connection and firewall (see **Firewall blocking connections** above). | Pressing **Check again** once fixed |
| "The scanner host is on, but its scanner service is not running.", or "The configured scanner's host is on, but its scanner service is not running." | The scanner host turned the connection down straight away: nothing is listening on port 6566. saneless still asks SANE for scanners, and when another scanner is usable anyway the row is amber instead: "... is ready, but the scanner service is not running on the scanner host." | Start `saned` on the scanner host (for example `systemctl start saned.socket`), and check it listens on the network, not only on the scanner host itself. | Pressing **Check again** once fixed |
| "The scanner host could not be found by name.", or "The configured scanner's host could not be found by name." | A host name in `[scanner] host`, in `SANE_NET_HOSTS`, or in a network device (`net:...`) set in `[scanner] device` could not be looked up. | Correct the name where you set it, or fix it in your DNS or `/etc/hosts`. When `SANE_NET_HOSTS` is set, it is used instead of `[scanner] host`, so correct it there. | Pressing **Check again** once the name is fixed in DNS or `/etc/hosts`; restarting saneless after changing `[scanner] host`, `[scanner] device` or `SANE_NET_HOSTS` |
| "The scanner host is refusing this machine.", or in amber "... is ready, but the scanner host is refusing this machine." | `saned` on the scanner host is turning this machine away, because this machine's address is not in its access list. | Add this machine's address to `/etc/sane.d/saned.conf` on the scanner host (see below). | Pressing **Check again** once fixed |
| "The scanner host is answering, but no scanner was found on it." | `saned` let this machine in but listed no scanner: the scanner is switched off, or not connected to the scanner host. | Check the scanner is switched on and connected to the scanner host; `scanimage -L` on the scanner host should list it. | Pressing **Check again** once fixed |
| "The configured scanner was not found." | The device set in `[scanner] device` is not in the list SANE returns, and opening it failed. | Check it is switched on and connected, then press **Check again**. If `saneless devices` does not list it, set `[scanner] device` to one it lists, then restart saneless. | Pressing **Check again** once the scanner is connected; restarting saneless after changing `[scanner] device` |
| "The configured scanner is not listed, and its host cannot be checked in advance, so the scanner could not be checked." | `[scanner] device` is a network device (`net:...`) that SANE did not list, and its scanner host is written in a form the check cannot connect to in advance, such as an IPv6 address. Opening the device would make SANE connect to that host with no time limit, so the check does not open it. | Add the device's scanner host to `[scanner] host`, or set `[scanner] device` to one `saneless devices` lists. | Restarting saneless |
| "No scanner was found." | No scanner host is configured, and SANE found no scanner at all. | Check the scanner is switched on and connected, then press **Check again**. | Pressing **Check again** once fixed |
| "The scanner library failed while listing scanners, so the scanner could not be checked." (amber) | The scanner library stopped with an error while saneless was asking it for scanners, and saneless's log names the signal it died from. saneless asks for scanners in a separate short-lived process, so only that process ended: the server itself keeps running. | Press **Check again**. If it keeps happening, the log line is worth reporting. | Pressing **Check again** |
| "The scanner library did not finish listing scanners in time, so the scanner could not be checked." (amber) | Listing scanners took longer than 30 seconds, so saneless stopped it. That is usually a scanner host that stopped answering just after the check reached it, or one of the hosts the check does not test in advance (see below). | Check the scanner, and its scanner host if it has one, are switched on and reachable, then press **Check again**. | Pressing **Check again** once fixed |
| "The scanner library gave no usable answer while listing scanners, so the scanner could not be checked." (amber) | saneless could not start the separate process that asks the scanner library for scanners, for example because the machine is short of memory or processes, or that process ended without an answer saneless could read. saneless's log has a warning that says which of these happened, with the system error's name, the process's exit status, or how much it wrote, and any error that process printed, such as a Python traceback. | Press **Check again**. If it keeps happening, the log is worth reporting. | Pressing **Check again** |

With several scanner hosts, the row reports the worst problem and counts the hosts that have it, for example "1 of 2 scanner hosts is refusing this machine". When a usable scanner is visible anyway, a host problem is shown in amber as "... is ready, but ..." rather than in red, because scanning still works. The row never names a host, an address or a device id, because the status strip is visible to everyone on your network.

**Refusing this machine.** `saned` checks every new connection against `/etc/sane.d/saned.conf` on the scanner host, which lists the addresses allowed to use it. Add the address `saned` sees for this machine: for saneless in a container on the same machine as `saned`, that is the Docker network's subnet (for example `172.17.0.0/16`); for a container reaching a different machine, it is usually the Docker host's own address. `saned`'s log on the scanner host names the address it turned away, in a line such as `access by host 192.168.1.20 denied`. There is no need to restart `saned`: it reads `saned.conf` afresh for each connection.

**Why some rows say restart saneless.** saneless asks SANE for scanners in a fresh, short-lived process every time. So a scanner host name that becomes resolvable, a scanner service that starts, and a scanner that is switched on or plugged in are all picked up the next time you press **Check again**, with no restart. The server also restarts its own connection to SANE at the start of every scan, so the next scan recovers too. A restart is only needed after you change saneless's own settings, such as `[scanner] host`, `[scanner] device` or `SANE_NET_HOSTS`, because saneless reads them when it starts. In Docker, restart it with `docker restart saneless`.

**The configured scanner's own host.** When `[scanner] device` is a network device (`net:host:...`), the check also tests that device's scanner host in advance on port 6566, where SANE will open the device, even if it is not in `SANE_NET_HOSTS` or `[scanner] host`, or is listed there with a different port. If that host is not answering, the row says so and the check stops there, because SANE would wait on the host until saneless stopped the listing. A host whose scanner service is not running answers at once, so the check still asks SANE for scanners and reports that host with its own row.

**Hosts the check does not test in advance.** Apart from the configured scanner's own host, the check tests only the hosts in `SANE_NET_HOSTS` or `[scanner] host`, and not all of those. SANE still connects to every host below when it lists scanners, so if one of them is switched off, the Scanner check can take up to about 30 seconds to answer, because saneless stops a scanner listing that has not finished by then:

- hosts listed only in the `net.conf` of the machine saneless runs on (in a container, the container's own `/etc/sane.d/net.conf`);
- the fifth and later different hosts in the list, because the check tests at most four so that it always finishes quickly;
- IPv6 addresses, and any entry that is not a plain host name or IPv4 address, such as a bare number;
- a port number after a host, as in `scanbox.lan:6566`: the check tests `scanbox.lan` on that port, but SANE reads the number as a second host name. Leave the port out; SANE always uses port 6566.

For a scan that fails after the scanner is discovered, see [Troubleshoot a Failed Scan](troubleshoot-a-failed-scan.md).
