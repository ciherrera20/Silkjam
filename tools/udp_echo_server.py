import asyncio
from typing import Any, cast

class EchoServerProtocol(asyncio.DatagramProtocol):
    transport: asyncio.DatagramTransport

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        message = data.decode()
        print('[server] Received %r from %s' % (message, addr))
        print('[server] Send %r to %s' % (message, addr))
        self.transport.sendto(data, addr)


async def main() -> None:
    print("[server] Starting UDP server")

    # Get a reference to the event loop as we plan to use
    # low-level APIs.
    loop = asyncio.get_running_loop()

    # One protocol instance will be created to serve all
    # client requests.
    transport, protocol = await loop.create_datagram_endpoint(
        EchoServerProtocol,
        local_addr=('127.0.0.1', 9999))

    try:
        await asyncio.sleep(3600)  # Serve for 1 hour.
    finally:
        transport.close()


asyncio.run(main())