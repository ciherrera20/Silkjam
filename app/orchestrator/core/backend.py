import re
import signal
import base64
import asyncio
import logging
import jproperties
from pathlib import Path
from enum import IntEnum
from contextlib import suppress

#
# Project imports
#
from .baseacm import BaseAsyncContextManager
from .protocol import MCVersion
from .supervisor import Supervisor, Timer
from .client import MCClient
from utils.logger_adapters import PrefixLoggerAdapter, BytesLoggerAdapter

logger = logging.getLogger(__name__)

class MCBackend(Supervisor):
    class Status(IntEnum):
        SLEEPING = 0
        RUNNING = 1

    def __init__(self, root: Path, listing: dict):
        super().__init__()
        self.root = root
        self.name = listing["name"]
        self.log = PrefixLoggerAdapter(logger, self.name)

        self.version = MCVersion(**listing["version"])
        self.subdomain = listing["subdomain"]
        self.sleep_properties = listing["sleep_properties"]
        self.sleep_timeout = self.sleep_properties["timeout"]
        self.status = MCBackend.Status.SLEEPING

        self.properties = None
        self.icon = None

        # Create units to supervise
        self.server_proc: MCServerProc = MCServerProc(self)
        self.sleep_timer: Timer = Timer(self.sleep_timeout)
        self.add_unit(self.server_proc, self.server_proc.monitor, restart=True, stopped=False)
        self.add_unit(self.sleep_timer, self.sleep_timer.wait, restart=False, stopped=True)

        self._online_players = 0  # Keep track of number of players connected to server
        self._status_change = asyncio.Event()
        self._online_player_change: asyncio.Event = asyncio.Event()

    async def _start(self):
        await super()._start()

        # Open and read server properties
        self.properties = jproperties.Properties()
        properties_path = (self.root / "server.properties")
        if properties_path.exists():
            self.log.info("Reading server properties")
            with properties_path.open("r") as f:
                self.properties.load(f.read())
        else:
            self.log.info("Server properties does not exist!")

        # Open and read icon if it exists
        icon_path = self.root / "world" / "icon.png"
        if icon_path.exists():
            self.log.info("Reading server icon properties")
            with icon_path.open("rb") as f:
                data = f.read()
            self.icon = f"data:image/png;base64,{base64.b64encode(data).decode('utf-8')}"
        else:
            self.icon = None

    async def reload_server_proc(self):
        # Start up or shut down server proc
        if self.status == MCBackend.Status.RUNNING and not self.server_proc.is_running():
            self.online_players = 0
            self.log.debug("Starting server proc")
            await self.start_unit(self.server_proc)
        elif self.status == MCBackend.Status.SLEEPING and not self.server_proc.is_sleeping():
            self.log.debug("Stopping server proc")
            await self.stop_unit(self.server_proc)
            self.online_players = 0

    def reload_sleep_timer(self):
        # Start or stop sleep timer
        self.log.debug("server_proc is running: %s, online_players: %s", self.server_proc.is_running(), self.online_players)
        if self.server_proc.is_running() and self.online_players == 0:
            self.log.debug("Starting sleep timer")
            self.start_unit_nowait(self.sleep_timer)
        elif self.server_proc.is_sleeping() or self.online_players > 0:
            self.log.debug("Stopping sleep timer")
            self.stop_unit_nowait(self.sleep_timer)

    async def serve_forever(self):
        while True:
            # Wait until the config changes or a proxy or server task is canceled or errors out
            (done_events, done_units), (_, _) = await self.supervise_until([self._status_change, self._online_player_change])
            if self._status_change in done_events:
                self.log.debug("Status change requested")
                await self.reload_server_proc()
                self._status_change.clear()
            if self._online_player_change in done_events:
                self.log.debug("Online players is %s", self.online_players)
                self.reload_sleep_timer()
                self._online_player_change.clear()
            if self.sleep_timer in done_units and done_units[self.sleep_timer] is None:
                self.log.info("No players connected for %ss, stopping server", self.sleep_timeout)
                self.set_sleeping()

    async def _stop(self, *args):
        await super()._stop(*args)

    @property
    def port(self):
        if "server-port" in self.properties:
            return int(self.properties["server-port"].data)

    @property
    def motd(self):
        if "motd" in self.properties:
            motd_prop = self.properties["motd"].data
        else:
            motd_prop = None
        if self.server_proc.is_running():
            return motd_prop
        else:
            return self.sleep_properties.get("motd") or motd_prop

    @property
    def online_players(self):
        if self.server_proc.is_running():
            return self._online_players
        else:
            return self.sleep_properties.get("online-players") or self._online_players

    @online_players.setter
    def online_players(self, value):
        self._online_players = value
        self._online_player_change.set()

    @property
    def max_players(self):
        if "max-players" in self.properties:
            max_players_prop = int(self.properties["max-players"].data)
        else:
            max_players_prop = None
        if self.server_proc.is_running():
            return max_players_prop
        else:
            return self.sleep_properties.get("max-players") or max_players_prop

    @property
    def waking_kick_msg(self):
        return self.sleep_properties.get("waking-kick-msg")

    def is_running(self):
        return self.server_proc.is_running()

    def is_sleeping(self):
        return self.server_proc.is_sleeping()

    def set_sleeping(self):
        if not self.server_proc.is_sleeping():
            self.log.debug("Set sleeping")
            self.status = MCBackend.Status.SLEEPING
            self._status_change.set()

    def set_running(self):
        if not self.server_proc.is_running():
            self.log.debug("Set running")
            self.status = MCBackend.Status.RUNNING
            self._status_change.set()

    def incr_online_players(self):
        self.online_players += 1

    def decr_online_players(self):
        self.online_players -= 1

    def __repr__(self):
        return f"MCBackend(\'{self.name}\')"

class MCServerProc(BaseAsyncContextManager):
    STARTING_SERVER_REGEX = re.compile(r"^.*:(\d{5}).*$")
    DONE_REGEX = re.compile(r"^.*Done \(.*$")

    STDOUT_LOG_LEVEL = logging.DEBUG
    STDERR_LOG_LEVEL = logging.ERROR

    def __init__(self, backend, stop_timeout=90, sigint_timeout=90, sigterm_timeout=90):
        super().__init__()
        self.backend = backend
        self.log = PrefixLoggerAdapter(backend.log, 'server_proc')
        self.root = backend.root
        self.name = backend.name
        self.stop_timeout = stop_timeout
        self.sigint_timeout = sigint_timeout
        self.sigterm_timeout = sigterm_timeout
        self.server_proc: asyncio.Process

    def is_sleeping(self):
        return not self._started or self.server_proc.returncode is not None

    def is_running(self):
        return self._started and self.server_proc.returncode is None

    async def _start(self):
        await super()._start()
        server_jar_file = self.root / "server.jar"
        self.server_proc = await asyncio.create_subprocess_exec(
            "java", "-Xmx2G", "-jar", server_jar_file, "nogui",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.root
        )
        self.log.info("Starting minecraft server process with pid %s", self.server_proc.pid)

        log_stderr_task = asyncio.create_task(self.log_pipe(self.server_proc.stderr, "stderr", self.STDERR_LOG_LEVEL))
        wait_until_done_initializing_task = asyncio.create_task(self.wait_until_done_initializing())

        done, pending = await asyncio.wait([wait_until_done_initializing_task, log_stderr_task], return_when=asyncio.FIRST_COMPLETED)
        if log_stderr_task in done:
            self.log.error("Server process closed stderr")
        if wait_until_done_initializing_task in pending:
            self.log.error("Did not finish initializing server")
        else:
            self.log.info("Minecraft server ready to accept players")

        # Cancel remaining task(s)
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _stop(self, *args):
        self.log.info("Closing minecraft server process")
        shutdown = self.server_proc.returncode is not None
        if shutdown:
            self.log.debug("Minecraft server process has already exited")

        if not shutdown:
            with suppress(asyncio.TimeoutError):
                # Send stop command and wait before progressing to SIGINT
                self.log.debug("Sending stop command to minecraft server process %s", self.server_proc.pid)
                self.server_proc.stdin.write(b"stop\n")
                await self.server_proc.stdin.drain()
                await asyncio.wait_for(self.server_proc.wait(), self.stop_timeout)
                shutdown = True
                self.log.debug("Minecraft server process exited after stop command")

        if not shutdown:
            with suppress(asyncio.TimeoutError):
                # Send SIGINT and wait before progressing to SIGTERM
                self.log.debug("Sending SIGINT to minecraft server process %s", self.server_proc.pid)
                self.server_proc.send_signal(signal.SIGINT)
                await asyncio.wait_for(self.server_proc.wait(), self.sigint_timeout)
                shutdown = True
                self.log.debug("Minecraft server process exited after SIGINT")

        if not shutdown:
            with suppress(asyncio.TimeoutError):
                # Send SIGTERM and wait before progressing to SIGKILL
                self.log.debug("Sending SIGTERM to minecraft server process %s", self.server_proc.pid)
                self.server_proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self.server_proc.wait(), self.sigterm_timeout)
                shutdown = True
                self.log.debug("Minecraft server process exited after SIGTERM")

        if not shutdown:
            # Send SIGKILL
            self.log.debug("Sending SIGKILL to minecraft server process %s", self.server_proc.pid)
            self.server_proc.send_signal(signal.SIGKILL)
            self.log.debug("Minecraft server process exited after SIGKILL")
        await super()._stop(*args)

    async def read_pipe(self, pipe):
        msg = b""
        while True:
            try:
                msg += await pipe.readuntil()
            except asyncio.LimitOverrunError as err:
                msg += await pipe.read(err.consumed)
            except asyncio.IncompleteReadError as err:
                msg += err.partial
            else:
                if len(msg) == 0:
                    break
                yield msg.decode("utf-8")
                msg = b""

    async def read_stdout_until_done_initializing(self):
        pipe_logger = PrefixLoggerAdapter(f"stdout") | self.log
        async for msg in self.read_pipe(self.server_proc.stdout):
            pipe_logger.log(self.STDOUT_LOG_LEVEL, msg)
            if m := self.STARTING_SERVER_REGEX.match(msg):
                if m.group(1) == str(self.backend.port):
                    self.log.debug("Read server starting msg")
                    break

        async for msg in self.read_pipe(self.server_proc.stdout):
            pipe_logger.log(self.STDOUT_LOG_LEVEL, msg)
            if self.DONE_REGEX.match(msg):
                self.log.debug("Read done msg")
                break

    async def ping_server_until_done_initializing(self, ping_interval=1):
        while True:
            self.log.debug("Sending server listing ping...")
            async with MCClient("0.0.0.0", self.backend.port) as client:
                with suppress(OSError):
                    await client.request_status()
                    break
            asyncio.sleep(ping_interval)
        self.log.debug("Received server listing ping response")

    async def wait_until_done_initializing(self, stdout_timeout=60, ping_timeout=60, ping_interval=1):
        try:
            await asyncio.wait_for(self.read_stdout_until_done_initializing(), stdout_timeout)
        except asyncio.TimeoutError:
            self.log.error("Did not receive server started log")

        try:
            await asyncio.wait_for(self.ping_server_until_done_initializing(ping_interval), ping_timeout)
        except asyncio.TimeoutError:
            self.log.error("Could not ping server")
            raise
        self.log.debug("Server finished initializing")

    async def log_pipe(self, pipe, pipe_name="pipe", level=logging.DEBUG):
        pipe_logger = PrefixLoggerAdapter(f"{pipe_name}") | self.log
        async for msg in self.read_pipe(pipe):
            pipe_logger.log(level, msg)

    async def monitor(self):
        self.log.debug("Starting backend monitor task for pid %s", self.server_proc.pid)
        try:
            await asyncio.gather(
                self.log_pipe(self.server_proc.stdout, "stdout", self.STDOUT_LOG_LEVEL),
                self.log_pipe(self.server_proc.stderr, "stderr", self.STDERR_LOG_LEVEL)
            )
            self.log.debug("Server process (pid %s) stopped communication", self.server_proc.pid)
        except asyncio.CancelledError:
            self.log.debug("Backend monitor task canceled")

    def __repr__(self):
        return f"MCServerProc(\'{self.name}\')"