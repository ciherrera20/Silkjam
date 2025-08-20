import signal
import base64
import asyncio
import logging
import jproperties
from pathlib import Path
from enum import IntEnum
from contextlib import asynccontextmanager, suppress, AbstractAsyncContextManager, AsyncExitStack

#
# Project imports
#
import mc_protocol_utils as mcpu

class PrefixLoggerAdapter(logging.LoggerAdapter):
    """ A logger adapter that adds a prefix to every message """
    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        return (f'[{self.extra["prefix"]}] ' + msg, kwargs)
logger = logging.getLogger(__name__)

MINECRAFT_PORT = 25565

class MCServer(AbstractAsyncContextManager):
    class Status(IntEnum):
        SLEEPING = 0
        RUNNING = 1

    def __init__(self, root: Path, listing: dict):
        self.root = root
        self.name = listing["name"]
        self.version = mcpu.MCVersion(**listing["version"])
        self.status = MCServer.Status.SLEEPING

        # Create context manager stack to handle all entering and exiting
        self._acm_stack = AsyncExitStack()

        self.properties = None
        self.icon = None

        self.server_proc = None
        self._proxy_server = None
        self._status_change = asyncio.Event()

        self.log = PrefixLoggerAdapter(logger, {"prefix": self.name})

    async def __aenter__(self):
        self.log.debug("Entering")

        # Enter context manager stack
        await self._acm_stack.__aenter__()

        # Open and read server properties
        self.log.info("Reading server properties")
        self.properties = jproperties.Properties()
        with (self.root / "server.properties").open("r") as f:
            self.properties.load(f.read())

        # Open and read icon if it exists
        self.log.info("Reading server icon properties")
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
        # Exit all open context managers
        await self._acm_stack.__aexit__(*args)

        self.log.debug("Exiting")
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
                self.log.debug("Status change requested")
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
        self.log.info("Starting minecraft server process with pid %s", server_proc.pid)
        try:
            yield server_proc
        finally:
            self.log.info("Closing minecraft server process")
            shutdown = server_proc.returncode is not None
            if shutdown:
                self.log.debug("Minecraft server process has already exited")

            if not shutdown:
                with suppress(asyncio.TimeoutError):
                    # Send stop command and wait before progressing to SIGINT
                    self.log.debug("Sending stop command to minecraft server process %s", server_proc.pid)
                    server_proc.stdin.write(b"stop\n")
                    await server_proc.stdin.drain()
                    await asyncio.wait_for(server_proc.wait(), stop_timeout)
                    shutdown = True
                    self.log.debug("Minecraft server process exited after stop command")

            if not shutdown:
                with suppress(asyncio.TimeoutError):
                    # Send SIGINT and wait before progressing to SIGTERM
                    self.log.debug("Sending SIGINT to minecraft server process %s", server_proc.pid)
                    server_proc.send_signal(signal.SIGINT)
                    await asyncio.wait_for(server_proc.wait(), sigint_timeout)
                    shutdown = True
                    self.log.debug("Minecraft server process exited after SIGINT")

            if not shutdown:
                with suppress(asyncio.TimeoutError):
                    # Send SIGTERM and wait before progressing to SIGKILL
                    self.log.debug("Sending SIGTERM to minecraft server process %s", server_proc.pid)
                    server_proc.send_signal(signal.SIGTERM)
                    await asyncio.wait_for(server_proc.wait(), sigterm_timeout)
                    shutdown = True
                    self.log.debug("Minecraft server process exited after SIGTERM")

            if not shutdown:
                # Send SIGKILL
                self.log.debug("Sending SIGKILL to minecraft server process %s", server_proc.pid)
                server_proc.send_signal(signal.SIGKILL)
                self.log.debug("Minecraft server process exited after SIGKILL")

    @asynccontextmanager
    async def mc_proxy_server(self):
        self.log.info("Starting proxy server on port %s", MINECRAFT_PORT)
        proxy_server = await asyncio.start_server(self._handle_client, "0.0.0.0", MINECRAFT_PORT)
        async with proxy_server:
            try:
                yield proxy_server
            finally:
                self.log.info("Closing proxy server")

    async def _handle_pings(self, reader, writer):
        try:
            # Read some initial data to figure out if the packet is a legacy ping
            data = await reader.read(1024)

            try:
                # Try parsing as a legacy ping
                _, legacy_ping = mcpu.decode_legacy_ping(data)
                self.log.debug("Received legacy ping: %s", legacy_ping)
                legacy_ping_response = mcpu.encode_legacy_ping_response(self.version.protocol, self.version.name, self.motd, self.max_players)
                writer.write(legacy_ping_response)
                await writer.drain()
            except mcpu.MCProtocolError as err:
                # Handle modern handshake
                try:
                    apacket_gen = mcpu.read_packets_forever(reader, initial_data=data, timeout=30)

                    # Read initial handshake packet
                    _, handshake = mcpu.decode_handshake_packet(await anext(apacket_gen))
                    self.log.debug("Received handshake: %s", handshake)
                    if handshake["next_state"] == 1:
                        # Read request packet and respond
                        mcpu.decode_request_packet(await anext(apacket_gen))

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
                        handshake_response = mcpu.encode_json_packet(0, handshake_response_paylod)
                        writer.write(handshake_response)
                        await writer.drain()

                        # Read ping packet and respond with pong
                        with suppress(asyncio.TimeoutError):
                            _, ping_payload = mcpu.decode_pingpong_packet(await anext(apacket_gen))
                            writer.write(mcpu.encode_pingpong_packet(ping_payload))
                            await writer.drain()
                    elif handshake["next_state"] == 2:
                        # Login attempt
                        kick_payload = {
                            "text": "§eServer is waking up, try again in 30s"
                        }
                        handshake_response = mcpu.encode_json_packet(0, kick_payload)
                        writer.write(handshake_response)
                        await writer.drain()
                        self.set_running()  # Start actual server process
                    else:
                        raise mcpu.MCProtocolError(f"Unknown next state in handshake: {handshake['next_state']}")
                except mcpu.MCProtocolError as err:
                    self.log.debug("Error during handshake: %s", err)
                except ConnectionResetError:
                    self.log.info("Client closed connection unexpectedly")
        except Exception as err:
            self.log.exception("Exception caught while handling client ping: %s", err)

    async def _forward_to_backend(self, reader, writer, backend_reader, backend_writer):
        # Forward traffic to actual minecraft server
        try:
            async def forward(src_reader, dst_writer):
                try:
                    while True:
                        data = await src_reader.read(4096)
                        if not data:
                            break
                        dst_writer.write(data)
                        await dst_writer.drain()
                finally:
                    dst_writer.close()

            await asyncio.gather(
                forward(reader, backend_writer),
                forward(backend_reader, writer)
            )
        except Exception as err:
            self.log.exception("Exception caught while forwarding to backend: %s", err)
        finally:
            backend_writer.close()
            await backend_writer.wait_closed()

    async def _handle_client(self, reader, writer):
        try:
            try:
                # Try connecting to the backend server
                backend_reader, backend_writer = await asyncio.open_connection("0.0.0.0", self.port)
            except ConnectionRefusedError as err:
                # Backend server isn't running, handle the pings here in the proxy
                await self._handle_pings(reader, writer)
            else:
                # Backend server is running, forward packets to it
                await self._forward_to_backend(reader, writer, backend_reader, backend_writer)
        except Exception as err:
            self.log.exception("Exception caught while handling client: %s", err)
        finally:
            writer.close()
            await writer.wait_closed()

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
            self.log.debug("Set sleeping")
            self.status = MCServer.Status.SLEEPING
            self._status_change.set()

    def set_running(self):
        if not self.is_running():
            self.log.debug("Set running")
            self.status = MCServer.Status.RUNNING
            self._status_change.set()