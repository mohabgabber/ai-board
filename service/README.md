# Optional read-only host

The Go host serves a validated snapshot. It does not expose arbitrary source files or accept board writes. The Python writer remains separate.

## Build and serve

From this directory:

```sh
go build -o ../bin/ai-board .
../bin/ai-board serve --root /path/to/private-board --listen 127.0.0.1:8080
```

On Windows use `../bin/ai-board.exe`. The root must contain a valid initialized `board-data.js`, `board.html`, `AGENT_PROMPT.md` and every referenced image. `check --root PATH` validates that complete snapshot.

`sync --source SOURCE --root CACHE` publishes a validated cache. `--watch` retries every three seconds, retaining the last good cache if the source disappears or becomes invalid. Do not use a destination shared by multiple publishers; only the Windows implementation enforces a publisher process lock.

## Configuration

Pass `--config /path/to/config.json` for service or serve mode. A minimal local configuration:

```json
{
  "root": "C:/ProgramData/AIBoard/site",
  "listen": "127.0.0.1:8080",
  "redirect": "",
  "hostname": "localhost"
}
```

Actual config files, serving caches and logs are ignored by Git. The optional redirect listener is disabled by default.

## Windows service

Build `bin/ai-board.exe` first. From an elevated Windows PowerShell prompt run:

```powershell
./service/Install.ps1 -Source "C:/path/to/private-board" -Owner "DOMAIN\User" -OwnerSid "S-1-5-21-REPLACE-WITH-REAL-SID"
```

Replace both owner values with the real interactive account that can read the source folder. The installer creates the `AIBoard` service running as LocalService, its local cache/config and one interactive publisher task. It configures automatic service recovery and keeps serving the cached board before that user logs in.

Installation binds to loopback. It does not change firewall, DNS, hosts files, sleep settings or the router. Review the script before running it; installation is a separate machine-level action, not part of cloning or testing the repository.

To remove an installation, stop only the `AIBoard-Publish` task and `AIBoard` service, unregister that task and delete that service. Preserve your source board and back up the cache before intentionally removing installed files. No automatic uninstall deletes board history.

## Optional DNS

The small LAN DNS proxy is included for deployments that need it; it is **off by default**. It requires explicit `dns_listen`, `dns_ip`, `dns_cidr` and `dns_upstreams` config. Use your own verified network values. The hostname is answered locally; ordinary queries forward to explicit upstreams and clients are filtered by CIDR. Router/DHCP distribution and firewall access are separate configuration work.

Documentation/test IPs use the reserved example range, not a real installation. No LAN availability or router configuration is implied.
