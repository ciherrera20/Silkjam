# test_status_ws.py
import asyncio
import websockets
import json

# Replace with the URL of your FastAPI WebSocket endpoint
SERVER_ID = "the_village"
WS_URL = f"ws://localhost:8500/ws/status/{SERVER_ID}"

async def main():
    async with websockets.connect(WS_URL) as websocket:
        print(f"Connected to {WS_URL}")
        try:
            while True:
                message = await websocket.recv()
                status = json.loads(message)
                print(f"Server status: {status}")
        except websockets.ConnectionClosed:
            print("Connection closed")

if __name__ == "__main__":
    asyncio.run(main())
