import asyncio
from fastapi import FastAPI, WebSocket
from fastapi.responses import RedirectResponse

app = FastAPI(
    title="Silkjam",
    description="Smooth Minecraft server setup to play with your friends!",
    version="0.1.0"
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