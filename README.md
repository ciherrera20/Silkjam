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
- Create server.properties if it doesn't exist
- Use dynamic port allocation for the backend
- Figure out how to create a less privileged user per backend server and start the server processes as that user
- Add unit to constantly ping server process and read the number of players connected, as well as the protocol version
- Add capability to backup to remote drive
- Add ability to disable sleep timeout
- When backend server is running, forward pings to it
    - [DONE] Figure out when backend server is ready to accept players
- Figure out why when spamming connect as the server starts, all future connects just show Server disconnected message

# DONE
- [DONE] Track PR to mctools: https://github.com/OwenCochell/mctools/pull/18
- [DONE] Figure out how to use RCON
- [DONE] Add core API to start/stop servers
- [DONE] Add server sleeping after a certain amount of time with no players
- [DONE] Consider re-writing backend as a single class that is started and stopped dynamically by orchestrator when the proxy received a signal
    - Decided against. Its easier to have a backend class that is always running and supervises its own set of units.
