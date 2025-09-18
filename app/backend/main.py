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
from models.config import Config

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
    # Pseudocode:
    async with MCOrchestrator("/app/data") as orch:
        app.state.orch = orch
        task = asyncio.create_task(orch.run())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

app = FastAPI(
    title="Silkjam",
    description="Smooth Minecraft server setup to play with your friends!",
    version="1.0.0",
    lifespan=lifespan,
    root_path="/api"
)

def get_orch():
    return app.state.orch

@app.get("/", tags=["docs"])
async def docs_redirect():
    return RedirectResponse(url="/api/docs")

# v1 router
v1_router = APIRouter(prefix="/v1", tags=["v1"])

@v1_router.get("/config")
async def get_config(orch: MCOrchestrator = Depends(get_orch)) -> Config:
    return orch.config.model_dump(by_alias=True)

app.include_router(v1_router)

# @app.websocket("/ws/status/{server_id}")
# async def websocket_status(websocket: WebSocket, server_id: str):
#     await websocket.accept()
#     while True:
#         # Mock status websocket endpoint
#         status = {"server_id": server_id, "status": "running"}
#         await websocket.send_json(status)
#         await asyncio.sleep(5)  # optional throttle