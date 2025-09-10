if __name__ == "__main__":
    import os, sys, subprocess
    ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True).stdout.decode("utf-8").strip()
    sys.path.append(os.path.join(ROOT, 'app', 'orchestrator'))

import asyncio
import logging
from typing import Self
from contextlib import AbstractAsyncContextManager

#
# Project imports
#
from core.protocol import (
    PacketReader,
    PacketWriter,
    MCProtocolError
)
from models.config import Version

logger = logging.getLogger(__name__)

class MCClient(AbstractAsyncContextManager):
    def __init__(
            self, hostname: str,
            port: int,
            version: Version=Version(
                name="0.0.0",
                protocol=127
            )
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
            packet_writer.write_pingpong_packet(packet_writer.random_long())
            await self.writer.drain()

            _, status_response = await packet_reader.read_json_packet()
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
    import json
    import argparse
    from rich_argparse import RichHelpFormatter

    parser = argparse.ArgumentParser(description="Simple Minecraft client that requests a status from a server", formatter_class=RichHelpFormatter)
    parser.add_argument("host", help="Hostname of the Minecraft server. You may also specify a port using the \":\" character.")
    parser.add_argument("--verbose", "-v", action="store_true", default=False, help="Enables verbose logging")
    parser.add_argument("--full", "-f", action="store_true", default=False, help="Output full response")
    args = parser.parse_args()

    if ":" in args.host:
        host, port = args.host.split(":")
        port = int(port)
    else:
        host = args.host
        port = 25565  # Default minecraft port

    logging.getLogger("asyncio").setLevel(logging.WARNING)
    if args.verbose:
        level = logging.DEBUG
    else:
        level = logging.ERROR
    logging.basicConfig(level=level, format="%(message)s")
    
    async def request_status():
        async with MCClient(host, port) as client:
            return await client.request_status(timeout=30)

    try:
        status = asyncio.run(request_status())
        if status is not None:
            if not args.full and "favicon" in status and len(status["favicon"]) > 100:
                status["favicon"] = status["favicon"][:97] + "..."
            print(json.dumps(status, indent=4))
    except Exception as e:
        sys.exit(1)