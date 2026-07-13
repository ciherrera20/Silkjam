import asyncio
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from typing import Any, cast

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
type VoicePacket = tuple[bytes, uuid.UUID, tuple[str | Any, int]]
type PlayerSession = tuple[float, UserProfile, MCBackend, str]

DOMAIN: str = os.environ["DOMAIN"]
DOMAIN_REGEX: re.Pattern[str] = re.compile(
    rf"(?:({SUBDOMAIN_REGEX.pattern})\.)?{re.escape(DOMAIN)}"
)
HOLD_TIMEOUT: int = 25
VOICE_PACKET_QUEUE_SIZE: int = 1024
VOICE_BRIDGE_IDLE_TIMEOUT: float = 60
VOICE_BRIDGE_CLEANUP_INTERVAL: float = 10
VOICE_LISTENER_RESTART_DELAY: float = 1
VOICE_LISTENER_MAX_RESTART_DELAY: float = 30


class VoiceProxy(asyncio.DatagramProtocol):
    PING_PROTOCOL_SENTINEL = uuid.UUID("58bc9ae9-c7a8-45e4-a11c-efbb67199425")

    def __init__(
        self,
        handler: Callable[[VoicePacket], None],
        log: logging.Logger | logging.LoggerAdapter[Any],
    ) -> None:
        self.handler = handler
        self.log = log
        self.closed = asyncio.Event()
        self.transport: asyncio.DatagramTransport

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.log.info("Voice UDP listener is ready")
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        if len(data) < 17 or data[0] != 0b11111111:
            self.log.debug("Dropped invalid Simple Voice Chat datagram from %s", addr)
            return
        player_id = uuid.UUID(bytes=data[1:17])
        if player_id == self.PING_PROTOCOL_SENTINEL:
            if len(data) < 41:
                self.log.debug("Dropped malformed Simple Voice Chat ping from %s", addr)
            else:
                self.transport.sendto(data[17:41], addr)
                self.log.debug("Responded to Simple Voice Chat connectivity ping from %s", addr)
            return
        self.handler((data, player_id, addr))

    def error_received(self, exc: Exception) -> None:
        self.log.warning("Voice UDP listener received an error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is None:
            self.log.debug("Voice UDP listener closed")
        else:
            self.log.warning("Voice UDP listener closed with an error: %s", exc)
        self.closed.set()


class VoiceBridge(asyncio.DatagramProtocol):
    def __init__(
        self,
        proxy_transport: asyncio.DatagramTransport,
        client_addr: tuple[str | Any, int],
        log: logging.Logger | logging.LoggerAdapter[Any],
    ):
        self.proxy_transport = proxy_transport
        self.client_addr = client_addr
        self.log = log
        self.transport: asyncio.DatagramTransport
        self.connected = False
        self.last_activity = time.monotonic()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)
        self.connected = True

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self.last_activity = time.monotonic()
        self.proxy_transport.sendto(data, self.client_addr)

    def forward(self, data: bytes) -> None:
        self.last_activity = time.monotonic()
        self.transport.sendto(data)

    def error_received(self, exc: Exception) -> None:
        self.log.warning("Voice bridge received an error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        del self.transport
        self.connected = False
        if exc is None:
            self.log.debug("Voice bridge closed")
        else:
            self.log.warning("Voice bridge closed with an error: %s", exc)


class MCProxy(BaseUnit):
    BUFFER_SIZE = 1 << 16  # 64 KiB

    def __init__(self, name: str, backends: dict[str, MCBackend], listing: ProxyListing):
        super().__init__()
        self.name = name
        self.listing = listing
        self.backends = backends
        self.stack: AsyncExitStack
        self.proxy_server: asyncio.Server
        self.voice_transport: asyncio.DatagramTransport | None = None
        self.httpx_client: httpx.AsyncClient
        self.mojang_limiter = asyncio.BoundedSemaphore(10)

        # player UUID -> { session ID -> (connection time, profile, backend, TCP peer IP) }
        self.player_sessions: dict[uuid.UUID, dict[int, PlayerSession]] = {}
        self.voice_bridges: dict[tuple[uuid.UUID, MCBackend], VoiceBridge] = {}
        self.voice_packet_queue: asyncio.Queue[VoicePacket] = asyncio.Queue(
            maxsize=VOICE_PACKET_QUEUE_SIZE
        )
        self.voice_queue_drops = 0

        self.log = PrefixLoggerAdapter(logger, {"proxy": self.name})

    @property
    def port(self) -> int:
        return self.listing.port

    @property
    def voice_port(self) -> int | None:
        return self.listing.voice_port

    async def _start(self) -> None:
        if self.voice_port is None:
            self.log.info("Starting proxy server on TCP port %s", self.port)
        else:
            self.log.info(
                "Starting proxy server on TCP port %s and voice listener on UDP port %s",
                self.port,
                self.voice_port,
            )
        self.stack = AsyncExitStack()
        self.proxy_server = await asyncio.start_server(self._handle_client, "0.0.0.0", self.port)
        self.httpx_client = httpx.AsyncClient()
        await self.stack.enter_async_context(self.proxy_server)
        await self.stack.enter_async_context(self.httpx_client)

    async def _stop(self, *args: Any) -> None:
        self.log.info("Stopping proxy server")
        self._close_all_voice_bridges("proxy stopped")
        if self.voice_transport is not None:
            self.voice_transport.close()
            self.voice_transport = None
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
        client_ip: str,
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
                    response = await self.httpx_client.get(
                        f"https://api.mojang.com/users/profiles/minecraft/{login_start.username}"
                    )
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
        player_sessions = self.player_sessions.setdefault(user_profile.id, {})
        if session_id not in player_sessions:
            player_sessions[session_id] = (time.monotonic(), user_profile, backend, client_ip)
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
            tracked_sessions = self.player_sessions.get(user_profile.id)
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
                    del self.player_sessions[user_profile.id]
                    self._close_player_voice_bridges(user_profile.id, "player disconnected")
                else:
                    _, _, selected_backend, _ = min(
                        tracked_sessions.values(), key=lambda session: session[0]
                    )
                    self._close_player_voice_bridges(
                        user_profile.id,
                        "player session changed backend",
                        keep_backend=selected_backend,
                    )
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
        client_ip: str,
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
                    packet_reader, handshake.protocol_version, conn_logger
                ):
                    track_player_session_task = asyncio.create_task(
                        self._track_player_session(
                            id(object()), login_start, backend, client_ip, conn_logger
                        )
                    )
                    initial_data += packet_writer.encode_login_start_packet(
                        login_start.username,
                        login_start.player_id,
                        login_start.extra_data,
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
            peername = writer.get_extra_info("peername")
            if peername is None:
                raise ConnectionError("Could not determine client address")
            ip, port = peername[:2]
            conn_logger = PrefixLoggerAdapter(self.log, {"ip": ip, "port": port})
            conn_logger.debug("Client connected")

            packet_reader = PacketReader(reader, timeout=30)
            packet_writer = PacketWriter(writer, timeout=30)
            if res := await self._identify_backend(packet_reader, conn_logger):
                backend, handshake = res
                if not backend.mcproc_running():
                    # Start server if a player is trying to join
                    player_joining = (
                        isinstance(handshake, HandshakeRequest) and handshake.next_state == 2
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
                                ip,
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
                        ip,
                        conn_logger,
                    )
            else:
                conn_logger.debug("Could not identify backend, closing connection")
        except Exception as err:
            conn_logger.exception("Exception caught while handling client: %s", err)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _get_or_create_voice_bridge(
        self, player_id: uuid.UUID, addr: tuple[str | Any, int]
    ) -> VoiceBridge | None:
        # Grab the player session connected the longest. This guards against unauthenticated
        # clients spoofing a player's UUID in a handshake to drop their voice connection.
        tracked_sessions = self.player_sessions.get(player_id)
        if tracked_sessions is None:
            self.log.debug("Dropped voice packet for untracked player %s", player_id)
            return None
        _, _, backend, client_ip = min(
            tracked_sessions.values(),
            key=lambda session: session[0],
        )
        if self.listing.voice_ip_binding and addr[0] != client_ip:
            self.log.debug(
                "Dropped voice packet for player %s from %s: does not match TCP peer IP %s",
                player_id,
                addr,
                client_ip,
            )
            return None
        if backend.voice_port is None or not backend.mcproc_running():
            self.log.debug(
                "Dropped voice packet for player %s: backend %s has no active voice server",
                player_id,
                backend.name,
            )
            return None

        voice_bridge = self.voice_bridges.get((player_id, backend))
        if voice_bridge is None or not voice_bridge.connected or voice_bridge.client_addr != addr:
            if voice_bridge is not None and voice_bridge.connected:
                self.log.info(
                    "Replacing voice bridge for player %s on backend %s after address changed",
                    player_id,
                    backend.name,
                )
                voice_bridge.transport.close()
            loop = asyncio.get_running_loop()
            assert (
                self.voice_transport is not None
            )  # This code only runs if the voice proxy was started
            new_voice_bridge = VoiceBridge(self.voice_transport, addr, self.log)
            await loop.create_datagram_endpoint(
                lambda: new_voice_bridge,
                remote_addr=("127.0.0.1", backend.voice_port),
            )
            self.voice_bridges[player_id, backend] = new_voice_bridge
            voice_bridge = new_voice_bridge
            self.log.info(
                "Created voice bridge for player %s from %s to backend %s on UDP port %s",
                player_id,
                addr,
                backend.name,
                backend.voice_port,
            )
        return voice_bridge

    def _queue_voice_packet(self, packet: VoicePacket) -> None:
        try:
            self.voice_packet_queue.put_nowait(packet)
        except asyncio.QueueFull:
            self.voice_queue_drops += 1
            if self.voice_queue_drops == 1 or self.voice_queue_drops % 100 == 0:
                self.log.warning(
                    "Dropped %s voice packets because the routing queue is full",
                    self.voice_queue_drops,
                )

    def _close_voice_bridge(self, key: tuple[uuid.UUID, MCBackend], reason: str) -> None:
        voice_bridge = self.voice_bridges.pop(key, None)
        if voice_bridge is None:
            return
        player_id, backend = key
        self.log.info(
            "Closing voice bridge for player %s on backend %s: %s",
            player_id,
            backend.name,
            reason,
        )
        if voice_bridge.connected:
            voice_bridge.transport.close()

    def _close_player_voice_bridges(
        self, player_id: uuid.UUID, reason: str, keep_backend: MCBackend | None = None
    ) -> None:
        for key in tuple(self.voice_bridges):
            bridge_player_id, backend = key
            if bridge_player_id == player_id and backend is not keep_backend:
                self._close_voice_bridge(key, reason)

    def _close_all_voice_bridges(self, reason: str) -> None:
        for key in tuple(self.voice_bridges):
            self._close_voice_bridge(key, reason)

    async def _run_voice_listener(self) -> None:
        voice_port = self.voice_port
        assert voice_port is not None  # This task only runs when voice chat is enabled
        restart_delay = VOICE_LISTENER_RESTART_DELAY
        loop = asyncio.get_running_loop()
        while True:
            transport: asyncio.DatagramTransport | None = None

            try:
                transport, protocol = await loop.create_datagram_endpoint(
                    lambda: VoiceProxy(self._queue_voice_packet, self.log),
                    local_addr=("0.0.0.0", voice_port),
                )
                self.voice_transport = transport
                restart_delay = VOICE_LISTENER_RESTART_DELAY
                await protocol.closed.wait()
            except OSError as err:
                self.log.warning("Voice UDP listener failed to start: %s", err)
            finally:
                if self.voice_transport is transport:
                    self.voice_transport = None
                if transport is not None:
                    transport.close()

            self.log.warning("Restarting voice UDP listener in %s seconds", restart_delay)
            await asyncio.sleep(restart_delay)
            restart_delay = min(restart_delay * 2, VOICE_LISTENER_MAX_RESTART_DELAY)

    async def _cleanup_voice_bridges(self) -> None:
        while True:
            await asyncio.sleep(VOICE_BRIDGE_CLEANUP_INTERVAL)
            now = time.monotonic()
            for key, voice_bridge in tuple(self.voice_bridges.items()):
                if now - voice_bridge.last_activity >= VOICE_BRIDGE_IDLE_TIMEOUT:
                    self._close_voice_bridge(key, "idle timeout")

    async def _handle_voice_packets(self) -> None:
        self.log.debug("Started voice packet handler")
        try:
            while True:
                data, player_id, addr = await self.voice_packet_queue.get()
                try:
                    voice_bridge = await self._get_or_create_voice_bridge(player_id, addr)
                    if voice_bridge is not None:
                        voice_bridge.forward(data)
                except Exception:
                    self.log.exception("Failed to route voice packet for player %s", player_id)
        finally:
            self.log.debug("Stopped voice packet handler")

    async def run(self) -> None:
        voice_tasks: list[asyncio.Task[None]] = []
        try:
            await self.start()
            if self.voice_port is not None:
                voice_tasks = [
                    asyncio.create_task(self._run_voice_listener()),
                    asyncio.create_task(self._handle_voice_packets()),
                    asyncio.create_task(self._cleanup_voice_bridges()),
                ]
            await self.proxy_server.serve_forever()
        finally:
            for task in voice_tasks:
                task.cancel()
            for task in voice_tasks:
                with suppress(asyncio.CancelledError):
                    await task

    def __repr__(self) -> str:
        return f"MCProxy('{self.name}')"
