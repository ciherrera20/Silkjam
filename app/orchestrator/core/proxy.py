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
from models.config import ProxyListing

logger = logging.getLogger(__name__)

HOSTNAME = os.environ["HOSTNAME"]

class MCProxy(BaseAsyncContextManager):
    def __init__(
            self,
            listing: ProxyListing
        ):
        super().__init__()
        self.name = listing.name
        self.port = listing.port
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

    async def _identify_backend(
            self,
            packet_reader: PacketReader,
            conn_logger: logging.Logger | logging.LoggerAdapter
        ) -> tuple[
            MCBackend | None,
            dict | None,
            bool | None
        ]:
        backend = handshake = is_legacy_ping = None
        try:
            try:
                # Try parsing as a legacy ping
                handshake = await packet_reader.read_legacy_ping()
                is_legacy_ping = True
                conn_logger.debug("Received legacy ping: %s", handshake)
                server_address = handshake["hostname"]
                if server_address[-len(HOSTNAME):] == HOSTNAME:
                    subdomain = server_address.split(HOSTNAME)[0][:-1]
                    conn_logger.debug("Identified subdomain: %s", subdomain)
                    if subdomain in self.subdomain_map:
                        # Backend identified with legacy ping
                        backend = self.subdomain_map[subdomain]
                        conn_logger.debug("Found server %s at %s", backend.name, server_address)
                    else:
                        conn_logger.debug("No server found at %s", server_address)
                else:
                    conn_logger.debug("Could not find hostname %s in server address %s", HOSTNAME, server_address)
            except MCProtocolError as err:
                # Handle modern handshake
                try:
                    # Read initial handshake packet
                    handshake = await packet_reader.read_handshake_packet()
                    is_legacy_ping = False
                    conn_logger.debug("Received handshake: %s", handshake)
                    server_address = handshake["server_address"]
                    if server_address[-len(HOSTNAME):] == HOSTNAME:
                        subdomain = server_address.split(HOSTNAME)[0][:-1]
                        conn_logger.debug("Identified subdomain: %s", subdomain)
                        if subdomain in self.subdomain_map:
                            # Backend identified with modern handshake
                            backend = self.subdomain_map[subdomain]
                            conn_logger.debug("Found server %s at %s", backend.name, server_address)
                        else:
                            conn_logger.debug("No server found at %s", server_address)
                    else:
                        conn_logger.debug("Could not find hostname %s in server address %s", HOSTNAME, server_address)
                except MCProtocolError as err:
                    conn_logger.debug("Error during handshake: %s", err)
                except ConnectionResetError:
                    conn_logger.info("Client closed connection unexpectedly")
        except Exception as err:
            conn_logger.exception("Exception caught while identifying backend: %s", err)
        return backend, handshake, is_legacy_ping

    async def _handle_handshake(
            self,
            backend: MCBackend,
            handshake: dict,
            is_legacy_ping: bool,
            packet_reader: PacketReader,
            packet_writer: PacketWriter,
            conn_logger: logging.Logger | logging.LoggerAdapter
        ):
        try:
            if is_legacy_ping:
                # Respond to legacy ping
                conn_logger.debug("Responding to client legacy ping")
                packet_writer.write_legacy_ping_response(backend.version.protocol, backend.version.name, backend.motd, backend.max_players)
                await packet_writer.drain()
            else:
                # Respond to modern handshake
                conn_logger.debug("Responding to client handshake")
                try:
                    if handshake["next_state"] == 1:
                        # Read request packet and respond
                        packet = _, (packet_id, _) = await packet_reader.read_packet()

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
                        # Backend server is starting, respond with message telling the client to wait
                        conn_logger.info("Backend server %s not ready yet, sending waking kick message", backend.name)
                        kick_payload = {
                            "text": backend.waking_kick_msg
                        }
                        packet_writer.write_json_packet(0, kick_payload)
                        await packet_writer.drain()
                    else:
                        raise MCProtocolError(f"Unknown next state in handshake: {handshake['next_state']}")
                except MCProtocolError as err:
                    conn_logger.debug("Error during handshake: %s", err)
                except ConnectionResetError:
                    conn_logger.info("Client closed connection unexpectedly")
        except Exception as err:
            conn_logger.exception("Exception caught while handling client handshake: %s", err)

    async def _forward_to_backend(
            self,
            backend: MCBackend,
            handshake: dict,
            is_legacy_ping: bool,
            packet_reader: PacketReader,
            packet_writer: PacketWriter,
            conn_logger: logging.Logger | logging.LoggerAdapter
        ):
        # Forward traffic to actual minecraft server
        conn_logger.debug("Starting port forwarding to %s", backend.name)
        try:
            # Try connecting to the backend server
            backend_reader, backend_writer = await asyncio.open_connection("0.0.0.0", backend.server_port)
        except ConnectionRefusedError as err:
            # Backend server should be ready but is refusing connections
            conn_logger.error("Backend server %s should be ready, but is not accepting connections", backend.name)
            kick_payload = {
                "text": f"§4Error connecting to {backend.name}"
            }
            packet_writer.write_json_packet(0, kick_payload)
            await packet_writer.drain()
        else:
            if is_legacy_ping:
                conn_logger.debug("Forwarding legacy ping to backend")
                initial_data = packet_writer.encode_legacy_ping(**handshake)
                player_joining = False
            else:
                conn_logger.debug("Forwarding handshake to backend")
                initial_data = packet_writer.encode_handshake_packet(**handshake)
                player_joining = handshake["next_state"] == 2
            initial_data += packet_reader.unparsed.tobytes()

            if player_joining:
                backend.incr_online_players()
            try:
                async def forward(initial_data, src_reader, dst_writer, direction_msg=None):
                    if direction_msg:
                        forwarding_logger = PrefixLoggerAdapter(direction_msg) | conn_logger
                    else:
                        forwarding_logger = conn_logger
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
                    forward(initial_data, packet_reader.reader, backend_writer, f"client -> {backend.name}"), # client -> proxy -> backend
                    forward(b"", backend_reader, packet_writer.writer, f"{backend.name} -> client"), # backend -> proxy -> client
                )
            except Exception as err:
                conn_logger.exception("Exception caught while forwarding to backend: %s", err)
            finally:
                backend_writer.close()
                await backend_writer.wait_closed()
            if player_joining:
                backend.decr_online_players()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            ip, port = writer.get_extra_info("peername")
            conn_logger = PrefixLoggerAdapter(f"{ip}:{port}") | self.log
            conn_logger.debug("Client connected")

            packet_reader = PacketReader(reader, timeout=30)
            packet_writer = PacketWriter(writer, timeout=30)
            backend, handshake, is_legacy_ping = await self._identify_backend(packet_reader, conn_logger)
            if backend is not None and backend.started:
                if not backend.mcproc_running():
                    # Start server if a player is trying to join
                    player_joining = is_legacy_ping or handshake["next_state"] == 2
                    if not backend.mcproc_starting() and player_joining:
                        conn_logger.info("Starting backend server %s", backend.name)
                        backend.start_mcproc()  # Start actual server process

                    # Respond to client while server is sleeping
                    await self._handle_handshake(backend, handshake, is_legacy_ping, packet_reader, packet_writer, conn_logger)
                else:
                    # Backend server is running, forward packets to it
                    await self._forward_to_backend(backend, handshake, is_legacy_ping, packet_reader, packet_writer, conn_logger)
            elif not backend.started:
                conn_logger.debug("%s backend is down, closing connection", backend.name)
            else:
                conn_logger.debug("Could not identify backend, closing connection")
        except Exception as err:
            conn_logger.exception("Exception caught while handling client: %s", err)
        finally:
            writer.close()
            await writer.wait_closed()

    async def serve_forever(self):
        await self.start()
        await self.proxy_server.serve_forever()

    def __repr__(self):
        return f"MCProxy(\'{self.name}\')"