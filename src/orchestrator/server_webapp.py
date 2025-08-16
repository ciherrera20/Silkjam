import asyncio
from fastapi import FastAPI, WebSocket
from fastapi.responses import RedirectResponse
from orchestrator.mc import monitor_mc_servers
import sys
import logging

# Configure logging first
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]  # ensure logs go to stdout for Docker
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Silkjam",
    description="Smooth Minecraft server setup to play with your friends!",
    version="1.0.0"
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

@app.on_event("startup")
async def on_startup():
    await monitor_mc_servers("/app/data")