import asyncio
from fastapi import FastAPI, WebSocket
from fastapi.responses import RedirectResponse
from orchestrator.mc import MCOrchestrator
import sys
from contextlib import asynccontextmanager
import logging

# Configure logging first
logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]  # ensure logs go to stdout for Docker
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    orch = MCOrchestrator("/app/data")
    for name, server in orch.servers.items():
        logger.info(f"Found server {name} on port {server.port}")
    task = asyncio.create_task(orch.run_servers())
    yield
    task.cancel()
    await task

app = FastAPI(
    title="Silkjam",
    description="Smooth Minecraft server setup to play with your friends!",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/")
async def docs_redirect():
    return RedirectResponse(url="/docs")

@app.websocket("/ws/status/{server_id}")
async def websocket_status(websocket: WebSocket, server_id: str):
    await websocket.accept()
    while True:
        # Mock status websocket endpoint
        status = {'server_id': server_id, 'status': 'running'}
        await websocket.send_json(status)
        await asyncio.sleep(5)  # optional throttle