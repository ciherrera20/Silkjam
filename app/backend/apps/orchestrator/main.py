import os
import sys
import asyncio
from fastapi import FastAPI, APIRouter, Depends, WebSocket
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
import logging

#
# Project imports
#
from core.orchestrator import MCOrchestrator
from models import Config

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
logging.getLogger("core.protocol").setLevel(logging.WARNING)
logging.getLogger("core.status_checker").setLevel(logging.INFO)
logging.getLogger("core.backup_manager").setLevel(logging.INFO)
logging.getLogger("supervisor.supervisor").setLevel(logging.WARNING)
logging.getLogger("supervisor.timer").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
logger.info("Logging level is %s", level)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with MCOrchestrator("/app/data") as orch:
        app.state.orch = orch
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(orch.run())
            yield
            task.cancel()

app = FastAPI(
    title="Silkjam Orchestrator",
    description="Smooth Minecraft server setup to play with your friends!",
    version="1.0.0",
    lifespan=lifespan,
    root_path="/api"
)

def get_orch() -> MCOrchestrator:
    return app.state.orch

@app.get("/", response_class=RedirectResponse, tags=["docs"])
async def docs_redirect():
    return RedirectResponse(url="/api/docs")

# v1 router
v1_router = APIRouter(prefix="/v1", tags=["v1"])

@v1_router.get("/config")
async def get_config(orch: MCOrchestrator=Depends(get_orch)) -> Config:
    return orch.config.model_dump(by_alias=True)

@v1_router.websocket("/config")
async def websocket_config(websocket: WebSocket, orch: MCOrchestrator=Depends(get_orch)):
    await websocket.accept()
    await websocket.send_json(orch.config.model_dump(by_alias=True))

app.include_router(v1_router)