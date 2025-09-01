import os
import asyncio
import logging
from contextlib import suppress

#
# Project imports
#
from .baseacm import BaseAsyncContextManager
from .backend import MCBackend
from .protocol import (
    PacketReader,
    PacketWriter,
    PacketType,
    MCProtocolError
)
from utils.logger_adapters import PrefixLoggerAdapter
logger = logging.getLogger(__name__)

HOSTNAME = os.environ["HOSTNAME"]

class MCProxy(BaseAsyncContextManager):
    def __init__(self, listing):
        super().__init__()
        self.name = listing["name"]
        self.port = listing["port"]
        self.proxy_server: asyncio.Server
        self._backends = []
        self.subdomain_map = {}

        self.log = PrefixLoggerAdapter(logger, self.name)

    @property
    def backends(self) -> list[MCBackend]:
        return self._backends

    @backends.setter
    def backends(self, new_backends: list[MCBackend]):
        self._backends = new_backends
        self.subdomain_map = {server.subdomain: server for server in self._backends}  # Subdomain -> server

    async def _start(self):
        self.log.debug("Starting proxy server on port %s", self.port)
        self.proxy_server = await asyncio.start_server(self._handle_client, "0.0.0.0", self.port)
        await self.proxy_server.__aenter__()

    async def _stop(self, *args):
        self.log.info("Stopping proxy server")
        await self.proxy_server.__aexit__(*args)
        del self.proxy_server

    async def _handle_handshake(self, packet_reader: PacketReader, packet_writer: PacketWriter) -> MCBackend | None:
        forward = False  # Whether or not to forward to the backend
        backend = None  # Identify backend
        handshake = None  # Handshake to forward to the backend
        try:
            try:
                # Try parsing as a legacy ping
                legacy_ping = await packet_reader.read_legacy_ping()
                self.log.debug("Received legacy ping: %s", legacy_ping)
                server_address = legacy_ping["hostname"]
                if server_address[-len(HOSTNAME):] == HOSTNAME:
                    subdomain = server_address.split(HOSTNAME)[0][:-1]
                    self.log.debug("Identified subdomain: %s", subdomain)
                    if subdomain in self.subdomain_map:
                        backend = self.subdomain_map[subdomain]
                        self.log.debug("Found server %s at %s, responding with server details", backend.name, server_address)
                        packet_writer.write_legacy_ping_response(backend.version.protocol, backend.version.name, backend.motd, backend.max_players)
                        await packet_writer.drain()
                    else:
                        self.log.debug("No server found at %s", server_address)
                else:
                    self.log.debug("Could not find hostname %s in server address %s", HOSTNAME, server_address)
            except MCProtocolError as err:
                # Handle modern handshake
                try:
                    # Read initial handshake packet
                    handshake = await packet_reader.read_handshake_packet()
                    self.log.debug("Received handshake: %s", handshake)
                    server_address = handshake["server_address"]
                    if server_address[-len(HOSTNAME):] == HOSTNAME:
                        subdomain = server_address.split(HOSTNAME)[0][:-1]
                        self.log.debug("Identified subdomain: %s", subdomain)
                        if subdomain in self.subdomain_map:
                            backend = self.subdomain_map[subdomain]
                            self.log.debug("Found server %s at %s, responding with server details", backend.name, server_address)

                            if handshake["next_state"] == 1:
                                # Read request packet and respond
                                packet = _, (packet_id, _) = await packet_reader.read_packet()
                                # await packet_reader.read_request_packet()

                                if packet_id == PacketType.REQUEST:  # Client is requesting status
                                    handshake_response_paylod = {
                                        "version": {
                                            "name": backend.version.name,
                                            "protocol": backend.version.protocol
                                        },
                                        "players": {
                                            "max": backend.max_players,
                                            "online": backend.online_players,
                                            "sample": []
                                        },	
                                        "description": {
                                            "text": backend.motd
                                        }
                                    }
                                    if backend.icon is not None:
                                        handshake_response_paylod["favicon"] = backend.icon
                                    packet_writer.write_json_packet(0, handshake_response_paylod)
                                    await packet_writer.drain()
                                    ping_payload = await packet_reader.read_pingpong_packet()
                                elif packet_id == PacketType.PINGPONG:  # Client skipped status request
                                    _, ping_payload = packet_reader.decode_pingpong_packet(packet)

                                # Read ping packet and respond with pong
                                with suppress(asyncio.TimeoutError):
                                    packet_writer.write_pingpong_packet(ping_payload)
                                    await packet_writer.drain()
                            elif handshake["next_state"] == 2:
                                forward = True
                                handshake = handshake
                            else:
                                raise MCProtocolError(f"Unknown next state in handshake: {handshake['next_state']}")
                        else:
                            self.log.debug("No server found at %s", server_address)
                    else:
                        self.log.debug("Could not find hostname %s in server address %s", HOSTNAME, server_address)
                except MCProtocolError as err:
                    self.log.debug("Error during handshake: %s", err)
                except ConnectionResetError:
                    self.log.info("Client closed connection unexpectedly")
        except Exception as err:
            self.log.exception("Exception caught while handling client handshake: %s", err)
        return forward, backend, handshake

    async def _forward_to_backend(
            self,
            initial_data: bytes,
            client_reader: asyncio.StreamReader,
            client_writer: asyncio.StreamWriter,
            backend_reader: asyncio.StreamReader,
            backend_writer: asyncio.StreamWriter,
            backend: MCBackend
        ):
        # Forward traffic to actual minecraft server
        self.log.debug("Starting port forwarding to %s", backend.name)
        backend.incr_online_players()
        try:
            async def forward(initial_data, src_reader, dst_writer, direction_msg=None):
                if direction_msg:
                    forwarding_logger = PrefixLoggerAdapter(direction_msg) | self.log
                else:
                    forwarding_logger = self.log
                try:
                    if len(initial_data) > 0:
                        dst_writer.write(initial_data)
                        await dst_writer.drain()
                    while True:
                        data = await src_reader.read(PacketReader.DEFAULT_BUFFER_SIZE)
                        if not data:
                            forwarding_logger.debug("Connection closed")
                            break
                        dst_writer.write(data)
                        await dst_writer.drain()
                finally:
                    dst_writer.close()

            await asyncio.gather(
                forward(initial_data, client_reader, backend_writer, f"client -> {backend.name}"), # client -> proxy -> backend
                forward(b"", backend_reader, client_writer, f"{backend.name} -> client"), # backend -> proxy -> client
            )
        except Exception as err:
            self.log.exception("Exception caught while forwarding to backend: %s", err)
        finally:
            backend_writer.close()
            await backend_writer.wait_closed()
            backend.decr_online_players()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        logger.info("Client connected")
        try:
            packet_reader = PacketReader(reader, timeout=30)
            packet_writer = PacketWriter(writer, timeout=30)
            forward, backend, handshake = await self._handle_handshake(packet_reader, packet_writer)

            if forward:
                if not backend.is_running():
                    self.log.info("Starting backend server %s", backend.name)
                    backend.set_running()  # Start actual server process
                    # await asyncio.sleep(20)
                    # await backend.ready()  # TODO: figure out how to implement a method like this
                try:
                    # Try connecting to the backend server
                    backend_reader, backend_writer = await asyncio.open_connection("0.0.0.0", backend.port)
                except ConnectionRefusedError as err:
                    # Backend server is stating, respond with message telling the client to wait
                    self.log.info("Backend server %s not ready yet, sending waking kick message", backend.name)
                    kick_payload = {
                        "text": backend.waking_kick_msg
                    }
                    packet_writer.write_json_packet(0, kick_payload)
                    await packet_writer.drain()
                else:
                    # Backend server is running, forward packets to it
                    initial_data = packet_reader.unparsed.tobytes() + packet_writer.encode_handshake_packet(**handshake)
                    await self._forward_to_backend(initial_data, reader, writer, backend_reader, backend_writer, backend)
        except Exception as err:
            self.log.exception("Exception caught while handling client: %s", err)
        finally:
            writer.close()
            await writer.wait_closed()

    async def serve_forever(self):
        await self.start()
        await self.proxy_server.serve_forever()

    def __repr__(self):
        return f"MCProxy(\'{self.name}\')"