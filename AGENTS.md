# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `app/`. The Python backend is organized by responsibility:

- `app/backend/apps/` contains FastAPI and orchestrator entry points.
- `app/backend/core/` contains protocol, proxy, client, backend, and backup logic.
- `app/backend/models/` contains Pydantic configuration and listing models.
- `app/backend/supervisor/` manages long-running units and timers.
- `app/backend/utils/` holds logging and backup strategies.

Tests are in `test/`; helper scripts are in `tools/`. Deployment files are `Dockerfile`, `docker-compose.yml`, `app/Caddyfile`, and `app/supervisord.conf`. Put local type stubs under `stubs/<package>/`.

## Build, Test, and Development Commands

Use uv for all project commands:

```bash
uv sync                         # create/update the development environment
uv run ruff format app test     # format Python files
uv run ruff check app test      # lint source and tests
uv run mypy app/backend         # strict type-check backend code
uv run pytest                   # run the test suite
docker compose up --build       # build and run the full service locally
```

To add a dependency, use `uv add <package>` (or `uv add --dev <package>`); this updates both `pyproject.toml` and `uv.lock`.

## Coding Style & Naming Conventions

Target Python 3.14 and use four-space indentation. Ruff has a 100-character line limit and enforces `E`, `F`, import sorting, bugbear, and pyupgrade rules. Run the formatter before committing, then address lint failures rather than broadly disabling rules. Use `snake_case` for functions, variables, and modules; `PascalCase` for classes; and precise type annotations. Mypy is strict—keep dependency workarounds as narrow stubs in `stubs/` rather than introducing `Any` broadly.

## Commit & Pull Request Guidelines

Use concise, imperative commit subjects, such as `Fix uvicorn commands` or `Add caddy TLS option`. Keep unrelated formatting or refactoring separate from functional changes. Pull requests should explain the behavior change, note configuration or migration implications, link an issue when available, and include verification results. Include screenshots only for user-visible API or frontend changes.

## Configuration & Security

Do not commit secrets, private hostnames, or runtime data volumes. Configure local deployment with environment variables in Compose (`DOMAIN`, `CADDY_TLS`, ports, UID/GID). For development TLS, `CADDY_TLS=tls internal` avoids public certificate requests; use production certificate settings only for a real domain.
