# Suggested project structure:
```
minecraft-orchestrator/
├── Dockerfile                  # Builds the orchestrator container
├── docker-compose.yml          # Optional, for local dev or orchestrator startup
├── README.md                   # Project documentation
├── requirements.txt            # Python dependencies (if using FastAPI/Flask)
├── orchestrator/               # Orchestrator core code
│   ├── __init__.py
│   ├── main.py                 # Entry point (starts REST API server)
│   ├── api/                    # REST API endpoints
│   │   ├── __init__.py
│   │   ├── servers.py          # Endpoints for create/list/delete servers
│   │   ├── backups.py          # Endpoints to trigger backups / restore
│   │   └── mods.py             # Endpoints to manage Fabric mods via ferium
│   ├── core/                   # Internal logic
│   │   ├── __init__.py
│   │   ├── server_manager.py   # Logic to spawn, stop, and track servers
│   │   ├── backup_manager.py   # Handles rclone uploads, schedules
│   │   ├── mod_manager.py      # Runs ferium commands to update mods
│   │   └── user_manager.py     # Handles Minecraft server users and privileges
│   └── utils/
│       ├── __init__.py
│       ├── logger.py           # Logging setup
│       └── config.py           # Loads config/environment variables
├── data/                       # Base directory for all server worlds
│   └── (world folders mounted here)
├── backups/                    # Local temporary storage for backups (optional)
├── scripts/                    # Helper scripts (cron, init, etc.)
│   ├── backup.sh
│   ├── restore.sh
│   └── start_server.sh
└── tests/                      # Unit / integration tests
    ├── test_api.py
    └── test_core.py
```

# TODOs
- Backend
    - Organize FastAPI backends: https://fastapi.tiangolo.com/tutorial/bigger-applications/#include-an-apirouter-in-another
    - Add ferium mod manager and server version upgrading
    - Create API endpoints
        - Endpoint to create/delete proxy
        - Endpoint to create/delete server
        - Endpoint to upload/download world
        - Endpoint to enable/disable proxies/servers
    - Create web ui
    - Use watchfiles to watch for changes in config file and apply them
        - Not sure the best way to implement config changes. Some require server restarts, some don't
    - Enforce IP bans per backend in proxy
    - Figure out how to create a less privileged user per backend server and start the server processes as that user
    - Figure out how to limit resources per server, and how to monitor resource usage (CPU, memory, disk usage, maybe network?)
    - Add per-unit force stop setting when exiting to supervisor
    - Add semaphore for initializing server processes to avoid overwhelming the machine
        - Add config option for the number of concurrent server starts
        - Maybe not necessary?
    - Add cooldown between backend server restarts?
    - Look into using Pydanic to validate protocol packets and possibly Construct to ease parsing them
    - Add EULA param to config
    - Add logging for all IPs trying to ping the server
    - Move backups into server folder and change current backup folder into a remote sync
    - Use subauth requests in nginx to serve static files, including dynmap files
- Frontend:
    - Set up React
- Still not happy with overall organization. Move docker stuff and env stuff back out into project root. Also, think about portability of project_environment

# IN PROGRESS

# DONE
- [DONE] Add automatic backups
    - [DONE] Local backups for now, can add cloud sync with rclone later
    - [DONE] Add retry on "file changed as we read it"
    - [DONE] Catch fatal errors and retry after some time
- [DONE] Separate background process to ping server status into its own unit
    - [DONE] Add one last check for player count before setting server status to sleep
- [DONE] Add nginx reverse proxy
- [DONE] Add background task to constantly ping server process and read the number of players connected, as well as the protocol version
- [DONE] Use Pydantic for reading and writing config and server properties
- [DONE] Create server.properties if it doesn't exist
- [DONE] Add ability to disable sleep timeout
- [DONE] Use dynamic port allocation for the backend
- [DONE] Track PR to mctools: https://github.com/OwenCochell/mctools/pull/18
- [DONE] Figure out how to use RCON
- [DONE] Add core API to start/stop servers
- [DONE] Add server sleeping after a certain amount of time with no players
- [DONE] Consider re-writing backend as a single class that is started and stopped dynamically by orchestrator when the proxy received a signal
    - Decided against. Its easier to have a backend class that is always running and supervises its own set of units.
- [DONE] When backend server is running, forward pings to it
    - [DONE] Figure out when backend server is ready to accept players
