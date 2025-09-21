import os
import sys
import json
import asyncio
import jsondiff
import websockets
from pathlib import Path
from typing import Annotated
from fastapi import FastAPI, APIRouter, Depends, HTTPException, Request, Header
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
import logging

#
# Project imports
#
from models import Config

STATIC_ROOT = Path("/app/data")

if os.environ.get("DEBUG", "").lower() == "true":
    format = "%(levelname)s [%(asctime)s] [%(name)s.%(funcName)s:%(lineno)d] %(message)s"
    level = logging.DEBUG
else:
    format = "%(levelname)s [%(asctime)s] %(message)s"
    level = logging.INFO
logging.basicConfig(
    level=level,
    format=format,
    handlers=[logging.StreamHandler(sys.stdout)]  # ensure logs go to stdout for Docker
)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

class LogFilter(logging.Filter):
    def filter(self, record):
        if record.args and len(record.args) >= 3:
            endpoint = "/api/v1/auth/static"
            if endpoint == record.args[2][:len(endpoint)] and record.args[4] in {204, 403}:
                return False
        return True
logging.getLogger("uvicorn.access").addFilter(LogFilter())

logger = logging.getLogger(__name__)
logger.info("Logging level is %s", level)

async def update_config(app, config_created: asyncio.Event):
    path = "/tmp/orchestrator.sock"
    while True:
        try:
            _, writer = await asyncio.open_unix_connection(path)
            writer.close()
            await writer.wait_closed()
            logger.info("Established connection to orchestrator")
            break
        except (ConnectionRefusedError, FileNotFoundError):
            logger.debug("Waiting for orchestrator...")
            await asyncio.sleep(1)
    async with websockets.unix_connect(path, uri="ws://localhost/v1/config") as websocket:
        async for message in websocket:
            data = app.state.config or {}
            update = json.loads(message)
            async with app.state.config_lock:
                app.state.config = Config.model_validate(jsondiff.patch(data, update), by_alias=True)
            logger.info("Updated config")
            config_created.set()
    logger.warning("Lost connection to orchestrator")

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.config = None
    app.state.config_lock = asyncio.Lock()
    async with asyncio.TaskGroup() as tg:
        config_created = asyncio.Event()
        task = tg.create_task(update_config(app, config_created))
        await config_created.wait()
        yield
        task.cancel()

def get_config() -> Config:
    return app.state.config

def get_config_lock() -> asyncio.Lock:
    return app.state.config_lock

app = FastAPI(
    title="Silkjam API",
    description="Smooth Minecraft server setup to play with your friends!",
    version="1.0.0",
    lifespan=lifespan,
    root_path="/api"
)

@app.get("/", response_class=RedirectResponse, tags=["docs"])
async def docs_redirect():
    return RedirectResponse(url="/api/docs")

# v1 router
v1_router = APIRouter(prefix="/v1", tags=["v1"])

@v1_router.get("/auth/static", status_code=204)
async def auth_static_path(
    x_filepath: Annotated[str, Header()],
    config: Config=Depends(get_config),
    config_lock: asyncio.Lock=Depends(get_config_lock)
):
    # Make sure path isn't going outside the root path
    relpath: Path = Path(os.path.join("/", x_filepath)[1:])
    if relpath.parts[0] == "..":
        logger.debug("Rejected path %s (%s) because it tried to leave the root directory", relpath, x_filepath)
        raise HTTPException(status_code=403)

    path: Path = STATIC_ROOT / relpath
    if path.is_relative_to(STATIC_ROOT / "servers"):
        server_name = path.parts[len(STATIC_ROOT.parts) + 1]

        async with config_lock:
            # Check server exists and is enabled
            if server_name in config.server_listing and config.server_listing[server_name].enabled:
                # Check that request is for dynmap file
                if path.is_relative_to(STATIC_ROOT / "servers" / server_name / "dynmap" / "web"):
                    return
    logger.debug("Forbidding path %s (%s)", relpath, x_filepath)
    raise HTTPException(status_code=403)

app.include_router(v1_router)