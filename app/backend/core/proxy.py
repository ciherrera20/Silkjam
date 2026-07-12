import asyncio
import logging
import os
import re
import uuid
from contextlib import AsyncExitStack, suppress
from typing import Any

import httpx

from backend.core.backend import MCBackend
from backend.core.protocol import (
    HandshakeRequest,
    LegacyPingRequest,
    LoginStart,
    MCProtocolError,
    PacketReader,
    PacketType,
    PacketWriter,
)
from backend.models import SUBDOMAIN_REGEX, ProxyListing, UserProfile
from backend.supervisor import BaseUnit
from backend.utils.logger_adapters import PrefixLoggerAdapter

logger = logging.getLogger(__name__)

type Handshake = LegacyPingRequest | HandshakeRequest

DOMAIN: str = os.environ["DOMAIN"]
DOMAIN_REGEX: re.Pattern[str] = re.compile(
    rf"(?:({SUBDOMAIN_REGEX.pattern})\.)?{re.escape(DOMAIN)}"
)
HOLD_TIMEOUT: int = 25


class MCProxy(BaseUnit):
    BUFFER_SIZE = 1 << 16  # 64 KiB

    def __init__(self, name: str, backends: dict[str, MCBackend], listing: ProxyListing):
        super().__init__()
        self.name = name
        self.listing = listing
        self.backends = backends
        self.stack: AsyncExitStack
        self.proxy_server: asyncio.Server
        self.httpx_client: httpx.AsyncClient
        self.mojang_limiter = asyncio.BoundedSemaphore(10)

        # player uuid -> { session_id -> (full profile, backend) }
        self.players: dict[uuid.UUID, dict[int, tuple[UserProfile, MCBackend]]] = {}

        self.log = PrefixLoggerAdapter(logger, {"proxy": self.name})

    @property
    def port(self) -> int:
        return self.listing.port

    async def _start(self) -> None:
        self.log.info("Starting proxy server on port %s", self.port)
        self.stack = AsyncExitStack()
        self.proxy_server = await asyncio.start_server(self._handle_client, "0.0.0.0", self.port)
        self.httpx_client = httpx.AsyncClient()
        await self.stack.enter_async_context(self.proxy_server)
        await self.stack.enter_async_context(self.httpx_client)

    async def _stop(self, *args: Any) -> None:
        self.log.info("Stopping proxy server")
        await self.stack.__aexit__(*args)
        del self.httpx_client
        del self.proxy_server
        del self.stack

    async def _identify_backend(
        self, packet_reader: PacketReader, conn_logger: logging.Logger | logging.LoggerAdapter[Any]
    ) -> tuple[MCBackend, Handshake] | None:
        handshake: Handshake
        try:
            try:
                # Try parsing as a legacy ping
                handshake = await packet_reader.read_legacy_ping()
                conn_logger.debug("Received legacy ping: %s", handshake)
                server_address = handshake.hostname
                if m := DOMAIN_REGEX.fullmatch(server_address):
                    subdomain = m.group(1) or ""
                    conn_logger.debug('Identified subdomain: "%s"', subdomain)
                    if subdomain in self.listing.subdomains:
                        # Backend identified with legacy ping
                        server_name = self.listing.subdomains[subdomain]
                        if self.listing.subdomains[subdomain] in self.backends:
                            backend = self.backends[server_name]
                            conn_logger.debug("Found server %s at %s", backend.name, server_address)
                            return backend, handshake
                        else:
                            conn_logger.debug(
                                "Backend %s not found, could be disabled or invalid", server_name
                            )
                    else:
                        conn_logger.debug("No server found at %s", server_address)
                else:
                    conn_logger.debug("Invalid server address %s", server_address)
            except MCProtocolError:
                # Handle modern handshake
                try:
                    # Read initial handshake packet
                    handshake = await packet_reader.read_handshake_packet()
                    conn_logger.debug("Received handshake: %s", handshake)
                    server_address = handshake.server_address
                    if m := DOMAIN_REGEX.match(server_address):
                        subdomain = m.group(1) or ""
                        conn_logger.debug('Identified subdomain: "%s"', subdomain)
                        if subdomain in self.listing.subdomains:
                            # Backend identified with legacy ping
                            server_name = self.listing.subdomains[subdomain]
                            if self.listing.subdomains[subdomain] in self.backends:
                                backend = self.backends[server_name]
                                conn_logger.debug(
                                    "Found server %s at %s", backend.name, server_address
                                )
                                return backend, handshake
                            else:
                                conn_logger.debug(
                                    "Backend %s not found, could be disabled or invalid",
                                    server_name,
                                )
                        else:
                            conn_logger.debug("No server found at %s", server_address)
                    else:
                        conn_logger.debug("Invalid server address %s", server_address)
                except MCProtocolError as err:
                    conn_logger.debug("Error during handshake: %s", err)
                except ConnectionResetError:
                    conn_logger.info("Client closed connection unexpectedly")
        except Exception as err:
            conn_logger.exception("Exception caught while identifying backend: %s", err)
        return None

    async def _read_login_start(
        self,
        packet_reader: PacketReader,
        protocol_version: int,
        conn_logger: logging.Logger | logging.LoggerAdapter[Any],
    ) -> LoginStart | None:
        try:
            try:
                login_start = await packet_reader.read_login_start_packet(protocol_version)
                conn_logger.debug(
                    "Received login start: %s (UUID %s)",
                    login_start.username,
                    login_start.player_id,
                )
                return login_start
            except MCProtocolError as err:
                conn_logger.debug("Error during login start: %s", err)
            except ConnectionResetError:
                conn_logger.info("Client closed connection unexpectedly")
        except Exception as err:
            conn_logger.exception("Exception caught while identifying user: %s", err)
        return None

    async def _handle_handshake(
        self,
        backend: MCBackend,
        handshake: Handshake,
        packet_reader: PacketReader,
        packet_writer: PacketWriter,
        conn_logger: logging.Logger | logging.LoggerAdapter[Any],
    ) -> None:
        try:
            if isinstance(handshake, LegacyPingRequest):
                # Respond to legacy ping
                conn_logger.debug("Responding to client legacy ping")
                packet_writer.write_legacy_ping_response(
                    backend.version.protocol,
                    backend.version.name,
                    backend.motd,
                    backend.max_players,
                )
                await packet_writer.drain()
            else:
                assert isinstance(handshake, HandshakeRequest)
                # Respond to modern handshake
                conn_logger.debug("Responding to client handshake")
                try:
                    if handshake.next_state == 1:
                        # Read request packet and respond
                        packet = _, (packet_id, _) = await packet_reader.read_packet()

                        if packet_id == PacketType.REQUEST:  # Client is requesting status
                            handshake_response_paylod = {
                                "version": {
                                    "name": backend.version.name,
                                    "protocol": backend.version.protocol,
                                },
                                "players": {
                                    "max": backend.max_players,
                                    "online": backend.online_players,
                                    "sample": [],
                                },
                                "description": {"text": backend.motd},
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
                    elif handshake.next_state == 2:
                        # The backend is starting, so tell the client to wait.
                        conn_logger.info(
                            "Backend server %s not ready yet, sending waking kick message",
                            backend.name,
                        )
                        kick_payload = {"text": backend.waking_kick_msg}
                        packet_writer.write_json_packet(0, kick_payload)
                        await packet_writer.drain()
                    else:
                        raise MCProtocolError(
                            b"", f"Unknown next state in handshake: {handshake.next_state}"
                        )
                except MCProtocolError as err:
                    conn_logger.debug("Error during handshake: %s", err)
                except ConnectionResetError:
                    conn_logger.info("Client closed connection unexpectedly")
        except Exception as err:
            conn_logger.exception("Exception caught while handling client handshake: %s", err)

    async def _track_player_session(
        self,
        session_id: int,
        login_start: LoginStart,
        backend: MCBackend,
        conn_logger: logging.Logger | logging.LoggerAdapter[Any],
    ) -> None:
        """Resolve and track a player session until this task is cancelled.

        Looks up the username through Mojang's API, then records the session
        under the player's UUID. Cancellation removes only this session.
        """
        if login_start.player_id is None:
            # Hit Mojang endpoint to lookup player username
            conn_logger.debug("Resolving Minecraft username %s", login_start.username)
            try:
                async with self.mojang_limiter:
                    response = await self.httpx_client.get(f"https://api.mojang.com/users/profiles/minecraft/{login_start.username}")
                    # Add a small delay as a primitive way to rate limit use of the API just in case
                    await asyncio.sleep(1)
                response.raise_for_status()
                user_profile = UserProfile.model_validate(response.json())
            except Exception as err:
                conn_logger.debug("Error identifying user %s: %s", login_start.username, err)
                return
            conn_logger.debug(
                "Resolved Minecraft username %s to player %s (%s)",
                login_start.username,
                user_profile.name,
                user_profile.id,
            )
        else:
            user_profile = UserProfile(name=login_start.username, id=login_start.player_id)

        # Track player until task is canceled
        player_sessions = self.players.setdefault(user_profile.id, {})
        if session_id not in player_sessions:
            player_sessions[session_id] = (user_profile, backend)
            conn_logger.debug(
                "Tracking session %s for player %s (%s) on backend %s",
                session_id,
                user_profile.name,
                user_profile.id,
                backend.name,
            )
        try:
            # Wait forever
            await asyncio.Event().wait()
        finally:
            # Remove session
            tracked_sessions = self.players.get(user_profile.id)
            if tracked_sessions is None or session_id not in tracked_sessions:
                conn_logger.warning(
                    "Player %s (%s) was not tracked during session %s cleanup",
                    user_profile.name,
                    user_profile.id,
                    session_id,
                )
            else:
                del tracked_sessions[session_id]
                if len(tracked_sessions) == 0:
                    del self.players[user_profile.id]
                conn_logger.debug(
                    "Stopped tracking session %s for player %s (%s)",
                    session_id,
                    user_profile.name,
                    user_profile.id,
                )

    async def _forward_to_backend(
        self,
        backend: MCBackend,
        handshake: Handshake,
        packet_reader: PacketReader,
        packet_writer: PacketWriter,
        conn_logger: logging.Logger | logging.LoggerAdapter[Any],
    ) -> None:
        # Forward traffic to actual minecraft server
        conn_logger.debug("Starting port forwarding to %s", backend.name)
        try:
            # Try connecting to the backend server
            backend_reader, backend_writer = await asyncio.open_connection(
                "0.0.0.0", backend.server_port
            )
        except ConnectionRefusedError:
            # Backend server should be ready but is refusing connections
            conn_logger.error(
                "Backend server %s should be ready, but is not accepting connections", backend.name
            )
            kick_payload = {"text": f"§4Error connecting to {backend.name}"}
            packet_writer.write_json_packet(0, kick_payload)
            await packet_writer.drain()
        else:
            if isinstance(handshake, LegacyPingRequest):
                conn_logger.debug("Forwarding legacy ping to backend")
                initial_data = packet_writer.encode_legacy_ping(
                    handshake.protocol_version,
                    handshake.hostname,
                    handshake.port,
                )
                player_joining = False
            else:
                conn_logger.debug("Forwarding handshake to backend")
                initial_data = packet_writer.encode_handshake_packet(
                    handshake.protocol_version,
                    handshake.server_address,
                    handshake.server_port,
                    next_state=handshake.next_state,
                )
                player_joining = handshake.next_state == 2

            if player_joining:
                # Read login start before forwarding to backend
                backend.incr_online_players()
                track_player_session_task = None
                if login_start := await self._read_login_start(
                    packet_reader,
                    handshake.protocol_version,
                    conn_logger
                ):
                    track_player_session_task = asyncio.create_task(
                        self._track_player_session(id(object()), login_start, backend, conn_logger)
                    )
                    initial_data += packet_writer.encode_login_start_packet(
                        login_start.username, login_start.player_id, login_start.extra_data,
                    )

            initial_data += packet_reader.unparsed.tobytes()
            try:

                async def forward(
                    initial_data: bytes,
                    src_reader: asyncio.StreamReader,
                    dst_writer: asyncio.StreamWriter,
                    direction_msg: str,
                ) -> None:
                    try:
                        if len(initial_data) > 0:
                            dst_writer.write(initial_data)
                            await dst_writer.drain()
                        while data := await src_reader.read(MCProxy.BUFFER_SIZE):
                            dst_writer.write(data)
                            await dst_writer.drain()
                        conn_logger.debug("[%s] Connection closed", direction_msg)
                    finally:
                        dst_writer.close()

                await asyncio.gather(
                    forward(
                        initial_data,
                        packet_reader.reader,
                        backend_writer,
                        f"client -> {backend.name}",
                    ),  # client -> proxy -> backend
                    forward(
                        b"", backend_reader, packet_writer.writer, f"{backend.name} -> client"
                    ),  # backend -> proxy -> client
                )
            except Exception as err:
                conn_logger.exception("Exception caught while forwarding to backend: %s", err)
            finally:
                backend_writer.close()
                await backend_writer.wait_closed()

                if player_joining:
                    backend.decr_online_players()
                    if track_player_session_task is not None:
                        track_player_session_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await track_player_session_task


    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            ip, port = writer.get_extra_info("peername")
            conn_logger = PrefixLoggerAdapter(self.log, {"ip": ip, "port": port})
            conn_logger.debug("Client connected")

            packet_reader = PacketReader(reader, timeout=30)
            packet_writer = PacketWriter(writer, timeout=30)
            if res := await self._identify_backend(packet_reader, conn_logger):
                backend, handshake = res
                if not backend.mcproc_running():
                    # Start server if a player is trying to join
                    player_joining = (
                        isinstance(handshake, HandshakeRequest)
                        and handshake.next_state == 2
                    )
                    if player_joining:
                        # Set server running if it isn't already
                        if not backend.mcproc_starting():
                            conn_logger.debug("Starting backend server %s", backend.name)
                            backend.start_mcproc()

                        # Keep client connected until server starts (or fails to start)
                        started = False
                        with suppress(asyncio.TimeoutError):
                            started = await asyncio.wait_for(
                                backend.mcproc_done_starting(), HOLD_TIMEOUT
                            )

                        if started:
                            # Forward client logging in to backend if it started successfully
                            conn_logger.debug("Backend server started successfully")
                            await self._forward_to_backend(
                                backend,
                                handshake,
                                packet_reader,
                                packet_writer,
                                conn_logger,
                            )
                        else:
                            # Respond to client logging in if server failed to start
                            conn_logger.debug(
                                "Backend server failed to start on time, sending waking kick msg"
                            )
                            await self._handle_handshake(
                                backend,
                                handshake,
                                packet_reader,
                                packet_writer,
                                conn_logger,
                            )
                    else:
                        # Respond to client not logging in while server is sleeping
                        await self._handle_handshake(
                            backend,
                            handshake,
                            packet_reader,
                            packet_writer,
                            conn_logger,
                        )
                else:
                    # The backend is running, so forward packets regardless of login state.
                    await self._forward_to_backend(
                        backend,
                        handshake,
                        packet_reader,
                        packet_writer,
                        conn_logger,
                    )
            else:
                conn_logger.debug("Could not identify backend, closing connection")
        except Exception as err:
            conn_logger.exception("Exception caught while handling client: %s", err)
        finally:
            writer.close()
            await writer.wait_closed()

    async def run(self) -> None:
        await self.start()
        await self.proxy_server.serve_forever()

    def __repr__(self) -> str:
        return f"MCProxy('{self.name}')"
