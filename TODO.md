# TODO

## Voice Chat

- Exercise bridge expiry, listener restart, and TCP peer-IP binding with real
  clients and NATs; adjust the 60-second idle timeout if necessary.
- Decide whether the voice queue size and listener restart backoff should be
  configurable.

## Testing

- Add unit tests.
- Add integration tests using a real Minecraft server.

## Backend and API

- Organize FastAPI backends using
  [APIRouter](https://fastapi.tiangolo.com/tutorial/bigger-applications/#include-an-apirouter-in-another).
- Add Ferium mod management and server-version upgrades.
- Create API endpoints to create/delete proxies and servers, upload/download
  worlds, and enable/disable proxies and servers.
- Watch configuration-file changes and apply them, accounting for changes that
  require a server restart.
- Enforce backend-specific IP bans in the proxy.
- Add a per-unit force-stop setting to the supervisor.
- Limit concurrent server initialization, with a configurable maximum if
  needed.
- Add a cooldown between backend-server restarts.
- Evaluate Pydantic packet validation and Construct for protocol parsing.
- Add an EULA configuration option.
- Log all client IPs attempting to ping a server.
- Move backups into each server directory and repurpose the current backup
  directory for remote synchronization.
- Serve static files, including Dynmap files, through authorization subrequests.

## Observability and Resource Management

- Add Prometheus metrics to Silkjam.
- Support a Prometheus metrics mod that instruments Minecraft servers; collect
  its metrics and add a `server` label.
- Run each backend as a less-privileged user.
- Limit and monitor per-server CPU, memory, disk, and network usage.

## Deployment and Architecture

- Separate Caddy and the frontend into their own containers.
- Revisit the overall organization, including Docker/environment file layout
  and project-environment portability.

## Frontend

- Create the web UI.
- Set up React.

## Completed milestones

- Added automatic local backups, including retry handling for files that change
  during reads and delayed retries after fatal errors.
- Added a dedicated status-checking unit, including a final player-count check
  before a server sleeps.
- Added a reverse proxy and continuous server-status/player-count/protocol
  polling.
- Added Pydantic configuration and property models, automatic
  `server.properties` creation, configurable sleep timeouts, dynamic backend
  port allocation, and RCON support.
- Added core start/stop APIs and automatic server sleeping when no players are
  connected.
- Kept always-running backend objects that supervise their own units rather
  than dynamically recreating a backend for each proxy signal.
- Forwarded pings to running backends after determining when a server is ready
  to accept players.
- Tracked the upstream mctools typing contribution:
  <https://github.com/OwenCochell/mctools/pull/18>.
