import random
import asyncio
import logging
from contextlib import suppress, AbstractAsyncContextManager

#
# Project imports
#
import mc_protocol_utils as mcpu

logger = logging.getLogger(__name__)

class MCClient(AbstractAsyncContextManager):
    def __init__(self, hostname: str, port: int, version: mcpu.MCVersion=mcpu.MCVersion("0.0.0", 127)):
        self.hostname = hostname
        self.port = port
        self.version = version

        self.reader = None
        self.writer = None

    async def __aenter__(self):
        logger.debug(f"Entering")
        logger.info(f"Connecting to {self.hostname}:{self.port}")
        self.reader, self.writer = await asyncio.open_connection(self.hostname, self.port)
        return self

    async def __aexit__(self, *args):
        self.writer.close()
        await self.writer.wait_closed()
        logger.info(f"Closed connection to {self.hostname}:{self.port}")
        logger.debug(f"Exiting")
        return False

    async def request_status(self, timeout: int | None=None) -> dict:
        try:
            logger.info(f"Requesting status from {self.hostname}:{self.port}")
            apacket_gen = mcpu.read_packets_forever(self.reader, timeout=timeout)

            logger.debug(f"Sending handshake packet to {self.hostname}:{self.port}")
            self.writer.write(mcpu.encode_handshake_packet(self.version.protocol, self.hostname, self.port))
            self.writer.write(mcpu.encode_request_packet())
            self.writer.write(mcpu.encode_pingpong_packet(mcpu.random_ping_payload()))
            await self.writer.drain()

            _, (_, status_response) = mcpu.decode_json_packet(await anext(apacket_gen))
            logger.debug(f"Recieved handshake response from {self.hostname}:{self.port}")
            return status_response
        except ConnectionResetError:
            logger.exception(f"Server {self.hostname}:{self.port} closed connection unexpectedly")
            return None

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format="[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s")
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    
    async def request_status():
        async with MCClient('localhost', 25565) as client:
            return await client.request_status(timeout=30)
    
    status = None
    err = None
    try:
        status = asyncio.run(request_status())
    except Exception as e:
        logger.exception(e)
        err = e