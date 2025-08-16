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
- Add core API to start/stop servers