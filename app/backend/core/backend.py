import re
import signal
import base64
import asyncio
import logging
from pathlib import Path
from contextlib import suppress, AbstractContextManager, asynccontextmanager
from mctools import AsyncPINGClient, AsyncRCONClient

#
# Project imports
#
from supervisor import Supervisor, Timer
from utils.logger_adapters import PrefixLoggerAdapter
from models import ServerListing, Version, ServerProperties
from core.backup_manager import MCBackupManager
from core.status_checker import MCStatusChecker

logger = logging.getLogger(__name__)

type PortCMFactory = callable[[], AbstractContextManager[int]]

class MCBackend(Supervisor):
    STARTING_SERVER_REGEX = re.compile(rb"^.*:(\d{5}).*$")
    DONE_REGEX = re.compile(rb"^.*Done \(.*$")

    STDOUT_LOG_LEVEL = logging.DEBUG
    STDERR_LOG_LEVEL = logging.WARNING

    def __init__(
        self,
        name: str,
        root: Path,
        port_factory: PortCMFactory,
        listing: ServerListing,
        supervisor: Supervisor,
        stop_timeout: int=90,
        sigint_timeout: int=90,
        sigterm_timeout: int=90
    ):
        super().__init__()
        self.name = name
        self.root: Path = root
        self.port_factory: PortCMFactory = port_factory
        self.listing: ServerListing = listing
        self.supervisor: Supervisor = supervisor
        self.stop_timeout: int = stop_timeout
        self.sigint_timeout: int = sigint_timeout
        self.sigterm_timeout: int = sigterm_timeout
        self.log = PrefixLoggerAdapter(logger, {"server": self.name})

        self.properties = ServerProperties.load(self.root / "server.properties")
        self.icon = None

        self.server_proc: asyncio.subprocess.Process

        # Create sleep timer unit to stop server process
        self.sleep_timer: Timer = Timer(listing.sleep_properties.timeout)
        self.add_unit(self.sleep_timer, restart=False, stopped=True)

        # Create status checker to check server status
        self.status_checker: MCStatusChecker = MCStatusChecker(
            self.name,
            self.aping_client_factory,
        )
        self.status_checker.on_server_list_ping(self.update_stats)
        self.add_unit(self.status_checker, restart=False, stopped=True)

        # Create backup manager to create backups
        self.backup_manager: MCBackupManager = MCBackupManager(
            self.name,
            self.root,
            self.listing.backup_properties,
            self.arcon_client_factory,
        )
        self.add_unit(self.backup_manager, restart=True, stopped=True)

        self._online_players = 0  # Keep track of number of players connected to server
        self._online_player_change: asyncio.Event = asyncio.Event()
        self._online_player_change.set()

        self.listing_change_cb = None

    @property
    def version(self):
        return self.listing.version

    @property
    def server_port(self):
        return self.properties.server_port

    @property
    def rcon_port(self):
        return self.properties.rcon_port

    @property
    def motd(self):
        if self.mcproc_running():
            return self.properties.motd
        else:
            return self.listing.sleep_properties.motd or f"§e{self.properties.motd}"

    @property
    def online_players(self):
        if self.mcproc_running():
            return self._online_players
        else:
            return 0

    @online_players.setter
    def online_players(self, value):
        self._online_players = value
        self._online_player_change.set()

    @property
    def max_players(self):
        return self.properties.max_players

    @property
    def waking_kick_msg(self):
        return self.listing.sleep_properties.waking_kick_msg

    def on_listing_change(self, cb):
        self.listing_change_cb = cb

    def update_stats(self, stats):
        self.online_players = stats["players"]["online"]
        version = Version(**stats["version"])
        if self.listing.version != version:
            self.listing.version = version
            if self.listing_change_cb is not None:
                self.listing_change_cb()

    async def _start(self):
        await super()._start()

        # Open and read icon if it exists
        icon_path = self.root / "world" / "icon.png"
        if icon_path.exists():
            self.log.debug("Reading server icon properties")
            with icon_path.open("rb") as f:
                data = f.read()
            self.icon = f"data:image/png;base64,{base64.b64encode(data).decode('utf-8')}"
        else:
            self.icon = None

        # Allocate ports and write server properties
        server_port = self.stack.enter_context(self.port_factory())
        rcon_port = self.stack.enter_context(self.port_factory())
        self.properties = ServerProperties.load(self.root / "server.properties")
        self.properties.server_port = server_port
        self.properties.rcon_port = rcon_port
        self.properties.enable_rcon = True
        self.properties.dump(self.root / "server.properties")

        # Start upstream server subprocess
        server_jar_file = self.root / "server.jar"
        self.server_proc = await asyncio.create_subprocess_exec(
            "java", "-Xmx2G", "-jar", server_jar_file, "nogui",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.root
        )
        self.log.info("Starting minecraft server process with pid %s", self.server_proc.pid)

        # Wait until server is done initializing
        async with asyncio.TaskGroup() as tg:
            log_stderr_task = tg.create_task(self.log_pipe(self.server_proc.stderr, self.STDERR_LOG_LEVEL))
            wait_until_done_initializing_task = tg.create_task(self.wait_until_done_initializing())
            done, pending = await asyncio.wait([wait_until_done_initializing_task, log_stderr_task], return_when=asyncio.FIRST_COMPLETED)
            if log_stderr_task in done:
                self.log.error("Server process closed stderr while starting")
            if wait_until_done_initializing_task in pending:
                self.log.error("Did not finish initializing server")
            else:
                self.log.info("Minecraft server ready to accept players")
            for task in pending:
                task.cancel()

        # Start both units
        self.start_unit_nowait(self.sleep_timer)
        self.start_unit_nowait(self.status_checker)
        self.start_unit_nowait(self.backup_manager)

    async def _stop(self, *args):
        # Stop running units
        await super()._stop(*args)

        log_stdout_task = asyncio.create_task(self.log_pipe(self.server_proc.stdout, self.STDOUT_LOG_LEVEL))
        log_stderr_task = asyncio.create_task(self.log_pipe(self.server_proc.stderr, self.STDERR_LOG_LEVEL))

        try:
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
        finally:
            with suppress(Exception):
                shutdown = self.server_proc.returncode is not None
                if not shutdown:
                    # Send SIGKILL
                    self.log.debug("Sending SIGKILL to minecraft server process %s", self.server_proc.pid)
                    self.server_proc.send_signal(signal.SIGKILL)
                    self.log.debug("Minecraft server process exited after SIGKILL")

        # Log return code
        if self.server_proc.returncode == 0:
            self.log.info("Minecraft server exited successfully")
        else:
            self.log.warning("Minecraft server exited with nonzero exit code: %s", self.server_proc.returncode)

        await asyncio.gather(log_stdout_task, log_stderr_task)

    async def read_stdout_until_done_initializing(self):
        async for msg in self.server_proc.stdout:
            self.log.log(self.STDOUT_LOG_LEVEL, msg.decode("utf-8"))
            if m := self.STARTING_SERVER_REGEX.match(msg):
                if m.group(1) == str(self.properties.server_port).encode("utf-8"):
                    self.log.debug("Read server starting msg")
                    break

        async for msg in self.server_proc.stdout:
            self.log.log(self.STDOUT_LOG_LEVEL, msg.decode("utf-8"))
            if self.DONE_REGEX.match(msg):
                self.log.debug("Read done msg")
                break

    async def ping_server_until_done_initializing(self, ping_interval=5):
        while True:
            self.log.debug("Sending ping...")
            async with AsyncPINGClient("0.0.0.0", self.properties.server_port) as client:
                with suppress(Exception):
                    await client.ping()
                    break
            await asyncio.sleep(ping_interval)
        self.log.debug("Received ping response")

    async def wait_until_done_initializing(self, stdout_timeout=300, ping_timeout=60, ping_interval=5):
        try:
            await asyncio.wait_for(self.read_stdout_until_done_initializing(), stdout_timeout)
        except asyncio.TimeoutError:
            self.log.error("Did not receive server started log")
            raise

        try:
            await asyncio.wait_for(self.ping_server_until_done_initializing(ping_interval), ping_timeout)
        except asyncio.TimeoutError:
            self.log.error("Could not ping server")
            raise
        self.log.debug("Server finished initializing")

    async def log_pipe(self, pipe: asyncio.StreamReader, level=logging.DEBUG):
        async for msg in pipe:
            self.log.log(level, msg.decode("utf-8"))

    async def request_status_continuously(self, interval=60, retry_interval=10, max_retries=3):
        retry_count = 0
        while True:
            try:
                async with AsyncPINGClient("0.0.0.0", self.properties.server_port) as client:
                    stats = await client.get_stats()
            except Exception:
                retry_count += 1
                if retry_count > max_retries:
                    self.log.error("Server process not responding to status requests")
                    raise
                else:
                    self.log.warning("Server process did not respond to status request, trying again")
                await asyncio.sleep(retry_interval)
            else:
                self.log.debug("Server process responded to status request")
                retry_count = 0
                self.update_stats(stats)
                await asyncio.sleep(interval)

    @asynccontextmanager
    async def aping_client_factory(self):
        async with AsyncPINGClient("0.0.0.0", self.properties.server_port) as client:
            yield client

    @asynccontextmanager
    async def arcon_client_factory(self):
        async with AsyncRCONClient("0.0.0.0", self.properties.rcon_port) as client:
            await client.login(self.properties.rcon_password)
            yield client

    async def supervise(self):
        while True:
            # Wait until the config changes or a proxy or server task is canceled or errors out
            (done_events, done_units), (_, _) = await self.supervise_until([self._online_player_change])
            if self._online_player_change in done_events:
                if self.online_players == 0:
                    if self.is_stopped(self.sleep_timer):
                        # The timer expired and triggered a status check, which confirmed that no players are connected
                        self.log.info("No players connected for %ss, stopping server", self.listing.sleep_properties.timeout)
                        self.supervisor.stop_unit_nowait(self)
                    elif self.sleep_timer.timeout is None:
                        # Reset sleep timer if it is suspended
                        self.log.debug("Setting sleep timer for %ss", self.listing.sleep_properties.timeout)
                        self.sleep_timer.timeout = self.listing.sleep_properties.timeout
                        self.sleep_timer.reset()
                else:
                    if self.is_stopped(self.sleep_timer):
                        # The timer expired and triggered a status check, which found that there are still players online
                        self.start_unit_nowait(self.sleep_timer)
                    if self.sleep_timer.timeout is not None:
                        self.log.debug("Suspending sleep timer")
                        self.sleep_timer.timeout = None
                self._online_player_change.clear()
            if self.sleep_timer in done_units:
                # Trigger a status check to determine the number of players online
                self.status_checker.check_nowait()
            if self.status_checker in done_units:
                err = done_units[self.status_checker]
                raise RuntimeError("Server stopped responding") from err

    async def run(self):
        self.log.debug("Starting server process monitor task for pid %s", self.server_proc.pid)
        async with asyncio.TaskGroup() as tg:
            supervise_task = tg.create_task(self.supervise())
            async with asyncio.TaskGroup() as proc_tg:
                proc_tg.create_task(self.log_pipe(self.server_proc.stdout, self.STDOUT_LOG_LEVEL))
                proc_tg.create_task(self.log_pipe(self.server_proc.stderr, self.STDERR_LOG_LEVEL))
            self.log.debug("Server process (pid %s) stopped communication", self.server_proc.pid)
            supervise_task.cancel()

    def mcproc_starting(self):
        return self.supervisor.is_starting(self)

    def mcproc_running(self):
        return self.supervisor.is_running(self)

    def mcproc_stopping(self):
        return self.supervisor.is_stopping(self)

    async def mcproc_done_starting(self):
        await self.supervisor.done_stopping(self)
        self.supervisor.start_unit_nowait(self)
        return await self.supervisor.done_starting(self)

    async def mcproc_done_stopping(self):
        self.supervisor.stop_unit_nowait(self)
        return await self.supervisor.done_stopping(self)

    def start_mcproc(self):
        self.supervisor.start_unit_nowait(self)

    def stop_mcproc(self):
        self.supervisor.stop_unit_nowait(self)

    def incr_online_players(self):
        self.online_players += 1

    def decr_online_players(self):
        self.online_players -= 1

    def __repr__(self):
        return f"MCBackend(\'{self.name}\')"