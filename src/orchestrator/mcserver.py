import signal
import base64
import asyncio
import logging
import jproperties
from pathlib import Path
from enum import IntEnum
from collections import namedtuple
from contextlib import asynccontextmanager, suppress, AbstractAsyncContextManager, AsyncExitStack, ExitStack

#
# Project imports
#
import orchestrator.protocol_utils as pu

logger = logging.getLogger(__name__)

MINECRAFT_PORT = 25565
SERVER_VERSION = "1.21.4"

MCVersion = namedtuple("MCVersion", ["name", "protocol"])

class MCServer(AbstractAsyncContextManager):
    class Status(IntEnum):
        SLEEPING = 0
        RUNNING = 1

    def __init__(self, root: Path, listing: dict):
        self.root = root
        self.name = listing["name"]
        self.version = MCVersion(**listing["version"])
        self.status = MCServer.Status.SLEEPING

        # Create sync and async context manager stacks to handle all entering and exiting
        self._cm_stack = ExitStack()
        self._acm_stack = AsyncExitStack()

        self.properties = None
        self.icon = None

        self.server_proc = None
        self._proxy_server = None
        self._status_change = asyncio.Event()

    async def __aenter__(self):
        # Enter sync and async context manager stacks
        self._cm_stack.__enter__()
        await self._acm_stack.__aenter__()

        # Open and read server properties
        logger.info(f"{self.name}: Reading server properties")
        self.properties = jproperties.Properties()
        with (self.root / "server.properties").open("r") as f:
            self.properties.load(f.read())

        # Open and read icon if it exists
        logger.info(f"{self.name}: Reading server icon properties")
        icon_path = self.root / "world" / "icon.png"
        if icon_path.exists():
            with icon_path.open("rb") as f:
                data = f.read()
            self.icon = f"data:image/png;base64,{base64.b64encode(data).decode('utf-8')}"
        else:
            self.icon = None

        # Open proxy server
        self._proxy_server = await self._acm_stack.enter_async_context(self.mc_proxy_server())
        return self

    async def __aexit__(self, *args):
        # Exit all open sync and async context managers
        await self._acm_stack.__aexit__(*args)
        self._cm_stack.__exit__(*args)
        logger.debug(f"{self.name}: Exited stacks")
        return False

    async def serve_forever(self):
        # Startup proxy server as background task
        proxy_server_task = asyncio.create_task(self._proxy_server.serve_forever())

        try:
            while True:
                # Start up or shut down server proc
                if self.status == MCServer.Status.RUNNING and not self.is_running():
                    self.server_proc = await self._acm_stack.enter_async_context(self.mc_server_proc())
                elif self.status == MCServer.Status.SLEEPING and not self.is_sleeping():
                    await self.server_proc.__aexit__(None, None, None)

                # Do nothing until the status changes
                while not self._status_change.is_set():
                    await self._status_change.wait()
                logger.debug(f"{self.name}: Status change requested")
                self._status_change.clear()
        finally:
            # Cancel proxy server task when exiting
            proxy_server_task.cancel()
            try:
                await proxy_server_task
            except asyncio.CancelledError:
                pass

    @asynccontextmanager
    async def mc_server_proc(self, stop_timeout=90, sigint_timeout=90, sigterm_timeout=90):
        server_jar_file = self.root / "server.jar"
        server_proc = await asyncio.create_subprocess_exec(
            "java", "-Xmx2G", "-jar", server_jar_file, "nogui",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.root
        )
        logger.info(f"{self.name}: Starting minecraft server process with pid {server_proc.pid}")
        try:
            yield server_proc
        finally:
            logger.info(f"{self.name}: Closing minecraft server process")
            shutdown = server_proc.returncode is not None
            if shutdown:
                logger.debug(f"{self.name}: Minecraft server process has already exited")

            if not shutdown:
                with suppress(asyncio.TimeoutError):
                    # Send stop command and wait before progressing to SIGINT
                    logger.debug(f"{self.name}: Sending stop command to minecraft server process {server_proc.pid}")
                    server_proc.stdin.write(b"stop\n")
                    await server_proc.stdin.drain()
                    await asyncio.wait_for(server_proc.wait(), stop_timeout)
                    shutdown = True
                    logger.debug(f"{self.name}: Minecraft server process exited after stop command")

            if not shutdown:
                with suppress(asyncio.TimeoutError):
                    # Send SIGINT and wait before progressing to SIGTERM
                    logger.debug(f"{self.name}: Sending SIGINT to minecraft server process {server_proc.pid}")
                    server_proc.send_signal(signal.SIGINT)
                    await asyncio.wait_for(server_proc.wait(), sigint_timeout)
                    shutdown = True
                    logger.debug(f"{self.name}: Minecraft server process exited after SIGINT")

            if not shutdown:
                with suppress(asyncio.TimeoutError):
                    # Send SIGTERM and wait before progressing to SIGKILL
                    logger.debug(f"{self.name}: Sending SIGTERM to minecraft server process {server_proc.pid}")
                    server_proc.send_signal(signal.SIGTERM)
                    await asyncio.wait_for(server_proc.wait(), sigterm_timeout)
                    shutdown = True
                    logger.debug(f"{self.name}: Minecraft server process exited after SIGTERM")

            if not shutdown:
                # Send SIGKILL
                logger.debug(f"{self.name}: Sending SIGKILL to minecraft server process {server_proc.pid}")
                server_proc.send_signal(signal.SIGKILL)
                logger.debug(f"{self.name}: Minecraft server process exited after SIGKILL")

    @asynccontextmanager
    async def mc_proxy_server(self):
        logger.info(f"{self.name}: Starting proxy server on port {MINECRAFT_PORT}")
        proxy_server = await asyncio.start_server(self.handle_client, "0.0.0.0", MINECRAFT_PORT)
        async with proxy_server:
            try:
                yield proxy_server
            finally:
                logger.info(f"{self.name}: Closing proxy server")

    async def handle_client(self, reader, writer):
        try:
            # Forward traffic to actual minecraft server
            backend_reader, backend_writer = await asyncio.open_connection("0.0.0.0", self.port)
            try:
                async def forward(src_reader, dst_writer):
                    try:
                        while True:
                            data = await src_reader.read(4096)
                            if not data:
                                break
                            dst_writer.write(data)
                            await dst_writer.drain()
                    except Exception:
                        pass
                    finally:
                        dst_writer.close()

                await asyncio.gather(
                    forward(reader, backend_writer),
                    forward(backend_reader, writer)
                )
            except Exception as err:
                logger.exception(f"{self.name}: Proxy error: {err}")
            finally:
                writer.close()
        except ConnectionRefusedError as err:
            # Read some initial data to figure out if the packet is legacy ping
            data = await reader.read(1024)

            # Handle legacy pings
            try:
                _, legacy_ping = pu.decode_legacy_ping(data)
                logger.debug(f"{self.name}: Received legacy ping: {legacy_ping}")
                legacy_ping_response = pu.encode_legacy_ping_response(self.version.protocol, self.version.name, self.motd, self.max_players)
                writer.write(legacy_ping_response)
                await writer.drain()
                writer.close()
                return
            except pu.ProtocolError as err:
                pass

            # Handle modern handshake
            try:
                apacket_gen = pu.read_packets_forever(data, reader)

                # Read initial handshake packet
                _, handshake = pu.decode_handshake_packet(await anext(apacket_gen))
                logger.debug(f"{self.name}: Received handshake: {handshake}")
                if handshake["next_state"] == 1:
                    # Read request packet and respond
                    pu.decode_request_packet(await anext(apacket_gen))

                    # Status request
                    handshake_response_paylod = {
                        "version": {
                            "name": self.version.name,
                            "protocol": self.version.protocol
                        },
                        "players": {
                            "max": self.max_players,
                            "online": 0,
                            "sample": []
                        },	
                        "description": {
                            "text": self.motd
                        }
                    }
                    if self.icon is not None:
                        handshake_response_paylod["favicon"] = self.icon
                    handshake_response = pu.encode_json_packet(0, handshake_response_paylod)
                    writer.write(handshake_response)
                    await writer.drain()

                    # Read ping packet and respond with pong
                    _, ping_payload = pu.decode_ping_packet(await anext(apacket_gen))
                    pong_packet = pu.encode_pong_packet(ping_payload)
                    writer.write(pong_packet)
                    await writer.drain()

                    # Done with status request
                    writer.close()
                elif handshake["next_state"] == 2:
                    # Login attempt
                    kick_payload = {
                        "text": "§eServer is waking up, try again in 30s"
                    }
                    handshake_response = pu.encode_json_packet(0, kick_payload)
                    writer.write(handshake_response)
                    await writer.drain()
                    writer.close()

                    self.set_running()  # Start actual server process
                else:
                    raise pu.ProtocolError(f"Unknown next state in handshake: {handshake['next_state']}")
            except pu.ProtocolError as err:
                logger.debug(f"{self.name}: Error during handshake: {err}")
                writer.close()
                return

    @property
    def port(self):
        return int(self.properties["server-port"].data)
    
    @property
    def max_players(self):
        return int(self.properties["max-players"].data)

    @property
    def motd(self):
        return self.properties["motd"].data

    def is_sleeping(self):
        return self.server_proc is None or self.server_proc.returncode is not None

    def is_running(self):
        return self.server_proc is not None and self.server_proc.returncode is None

    def set_sleeping(self):
        if not self.is_sleeping():
            logger.debug(f"{self.name}: Set sleeping")
            self.status = MCServer.Status.SLEEPING
            self._status_change.set()

    def set_running(self):
        if not self.is_running():
            logger.debug(f"{self.name}: Set running")
            self.status = MCServer.Status.RUNNING
            self._status_change.set()