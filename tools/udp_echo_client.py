import asyncio
import aioconsole
from typing import Any, Literal, cast


class EchoClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_con_lost: asyncio.Future[Literal[True]]):
        self.on_con_lost = on_con_lost
        self.transport: asyncio.DatagramTransport
        self.queue = asyncio.Queue[str]()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        print(f"[client] Received ({addr=}):", data.decode())

        # print("Close the socket")
        # self.transport.close()

    def error_received(self, exc: Exception) -> None:
        print('[client] Error received:', exc)

    def connection_lost(self, exc: Exception | None) -> None:
        print("[client] Connection closed:", exc)
        self.on_con_lost.set_result(True)

    def send_message(self, message: str) -> None:
        print('[client] Send:', message)
        self.transport.sendto(message.encode())

async def main() -> None:
    # Get a reference to the event loop as we plan to use
    # low-level APIs.
    loop = asyncio.get_running_loop()

    on_con_lost: asyncio.Future[Literal[True]] = loop.create_future()

    transport, protocol = await loop.create_datagram_endpoint(
        lambda: EchoClientProtocol(on_con_lost),
        remote_addr=('127.0.0.1', 9999))

    try:
        while True:
            msg = await aioconsole.ainput('> ')
            protocol.send_message(msg)
            await asyncio.sleep(0.1)
    except KeyboardInterrupt, asyncio.CancelledError:
        print('\n[client] Shutting down')

    transport.close()
    await on_con_lost

asyncio.run(main())