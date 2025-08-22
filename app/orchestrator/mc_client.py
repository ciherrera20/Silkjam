import asyncio
import logging
from typing import Self
from contextlib import AbstractAsyncContextManager

#
# Project imports
#
from mc_protocol_utils import PacketReader, PacketWriter, MCVersion, MCProtocolError

logger = logging.getLogger(__name__)

class MCClient(AbstractAsyncContextManager):
    def __init__(
            self, hostname: str,
            port: int,
            version: MCVersion=MCVersion("0.0.0", 127)
        ):
        self.hostname = hostname
        self.port = port
        self.version = version

        self.reader = None
        self.writer = None

    async def __aenter__(self) -> Self:
        logger.debug("Entering")
        logger.info("Connecting to %s:%s", self.hostname, self.port)
        self.reader, self.writer = await asyncio.open_connection(self.hostname, self.port)
        return self

    async def __aexit__(self, *_) -> bool:
        self.writer.close()
        await self.writer.wait_closed()
        logger.info("Closed connection to %s:%s", self.hostname, self.port)
        logger.debug("Exiting")
        return False

    async def request_status(self, timeout: int | None=None) -> dict:
        try:
            logger.info("Requesting status from %s:%s", self.hostname, self.port)
            packet_reader = PacketReader(self.reader, timeout=timeout)
            packet_writer = PacketWriter(self.writer, timeout=timeout)

            logger.debug("Sending handshake packet to %s:%s", self.hostname, self.port)
            packet_writer.write_handshake_packet(self.version.protocol, self.hostname, self.port)
            packet_writer.write_request_packet()
            packet_writer.write_pingpong_packet(packet_writer.random_ping_payload())
            await self.writer.drain()

            status_response = await packet_reader.read_json_packet()
            logger.debug("Recieved handshake response from %s:%s", self.hostname, self.port)
            return status_response
        except ConnectionResetError:
            logger.error("Server %s:%s closed connection unexpectedly", self.hostname, self.port)
            return None
        except MCProtocolError as err:
            logger.error("Could not interpret server %s:%s's response: %s", self.hostname, self.port, err)
            return None
        except Exception as err:
            logger.exception("Exception caught while requesting server %s:%s's status: %s", self.hostname, self.port, err)

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s")
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    
    async def request_status():
        async with MCClient("AstraEste.minehut.gg", 25565) as client:
            return await client.request_status(timeout=30)
    
    status = None
    err = None
    try:
        status = asyncio.run(request_status())
    except Exception as e:
        logger.exception(e)
        err = e