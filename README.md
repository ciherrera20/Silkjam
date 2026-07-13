# Silkjam

Silkjam orchestrates on-demand Minecraft servers behind a TCP proxy. It starts
backends when players join, manages server and RCON ports, tracks server status,
and can proxy Simple Voice Chat UDP traffic. The web API is served through
Caddy alongside the small frontend.

## Quick start

Docker Compose is the supported way to run Silkjam locally.

```bash
cp .env.sample .env
docker compose up --build
```

The sample configuration uses `mc.localhost`, exposes the API on
`http://localhost:8500`, Minecraft on TCP port `25565`, and Simple Voice Chat
on UDP port `24454`. It uses `CADDY_TLS=tls internal`, which creates a local
certificate; browsers may require Caddy's local CA to be trusted before HTTPS
is shown as trusted.

Runtime data is stored under `DATA_VOLUME` (by default `./volumes/data` in the
sample). On first start Silkjam creates `config.json` there. Edit that file to
add backend servers and routes, then restart the service after configuration
changes.

## Configuration

Environment variables configure the container and public endpoints:

| Variable | Purpose |
| --- | --- |
| `DOMAIN` | Minecraft and HTTPS domain, for example `mc.example.com`. |
| `MINECRAFT_PORT` | Host TCP port mapped to the default proxy port. |
| `VOICECHAT_PORT` | Host UDP port for Simple Voice Chat; it must match the proxy's `voice_port`. |
| `API_PORT` | Host port mapped to the internal API port 8000. |
| `CADDY_TLS` | Set `tls internal` for local development; leave empty for Caddy's public certificate handling. |
| `DATA_VOLUME`, `CADDY_VOLUME` | Persistent host paths or named volumes for Silkjam and Caddy data. |
| `SERVER_PORTS` | Inclusive internal allocation range, such as `40000:45000`. |
| `DEBUG` | Set to `true` for verbose logs. |
| `PUID`, `PGID` | UID and GID used for mounted runtime data. |

`config.json` contains `proxy_listing` and `server_listing`. A proxy maps
subdomains to backends; the empty subdomain (`""`) is the base domain. Set
`voice_port` to enable its UDP listener. `voice_ip_binding` defaults to `true`
and accepts voice datagrams only from a player's Minecraft TCP source IP; set it
to `false` for deployments where TCP and UDP legitimately use different client
IPs.

Server entries control the Minecraft version, sleep behavior, backup behavior,
and per-backend `jvm_flags` (default: `-Xmx2G`). When a voice-enabled backend is
routed through exactly one voice-enabled proxy, Silkjam writes that public
`DOMAIN:voice_port` value to the backend's `voice_host` property automatically.

For example:

```json
{
  "proxy_listing": {
    "public": {
      "port": 25565,
      "voice_port": 24454,
      "voice_ip_binding": true,
      "enabled": true,
      "subdomains": {"": "survival", "play": "survival"}
    }
  },
  "server_listing": {
    "survival": {
      "version": {"name": "1.21.4", "protocol": 769},
      "sleep_properties": {"timeout": 300, "motd": null},
      "backup_properties": {
        "interval": 60,
        "max_backups": 5,
        "strategy": "exponential",
        "enabled": true
      },
      "jvm_flags": ["-Xmx2G"],
      "enabled": true
    }
  }
}
```

## Production deployment

Set `DOMAIN` to a public DNS name pointing at the host, use persistent volumes,
and leave `CADDY_TLS` unset so Caddy can obtain and renew certificates. Publish
TCP ports 443 and 25565 and the configured UDP voice port through your firewall
and container runtime. Ensure the configured ownership (`PUID`/`PGID`) can read
and write the data and Caddy volumes.

Before exposing the service, set a real domain, use production volume paths,
and review `config.json` routes and server settings. Keep `DEBUG=false` unless
you are diagnosing an issue.

## Development commands

Use uv for local checks outside the container:

```bash
uv sync
uv run ruff format app test
uv run ruff check app test
uv run mypy app/backend
uv run pytest
```

See [TODO.md](TODO.md) for planned work and completed historical milestones.
