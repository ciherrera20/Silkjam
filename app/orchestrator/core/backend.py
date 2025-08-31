import signal
import base64
import asyncio
import logging
import jproperties
from pathlib import Path
from enum import IntEnum
from contextlib import asynccontextmanager, suppress

#
# Project imports
#
from .baseacm import BaseAsyncContextManager
from .protocol import MCVersion
from .supervisor import Supervisor
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
        self.version = MCVersion(**listing["version"])
        self.subdomain = listing["subdomain"]
        self.status = MCBackend.Status.SLEEPING

        self.properties = None
        self.icon = None

        self.server_proc: MCServerProc = None
        self._status_change = asyncio.Event()

        self.log = PrefixLoggerAdapter(logger, self.name)

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

    async def serve_forever(self):
        while True:
            # Start up or shut down server proc
            if self.status == MCBackend.Status.RUNNING and not self.is_running():
                self.server_proc = MCServerProc(self)
                self.add_unit(self.server_proc, self.server_proc.monitor)
            elif self.status == MCBackend.Status.SLEEPING and not self.is_sleeping():
                self.remove_unit_nowait(self.server_proc)
                self.server_proc = None

            # Wait until the config changes or a proxy or server task is canceled or errors out
            (done_events, _), (_, _) = await self.supervise_until([self._status_change])
            if self._status_change in done_events:
                self.log.debug("Status change requested")
                self._status_change.clear()

    async def _stop(self, *args):
        await super()._stop(*args)

    @property
    def port(self):
        if "server-port" in self.properties:
            return int(self.properties["server-port"].data)
    
    @property
    def max_players(self):
        if "max-players" in self.properties:
            return int(self.properties["max-players"].data)

    @property
    def motd(self):
        if "motd" in self.properties:
            return self.properties["motd"].data

    def is_sleeping(self):
        return self.server_proc is None or self.server_proc.returncode is not None

    def is_running(self):
        return self.server_proc is not None and self.server_proc.returncode is None

    def set_sleeping(self):
        if not self.is_sleeping():
            self.log.debug("Set sleeping")
            self.status = MCBackend.Status.SLEEPING
            self._status_change.set()

    def set_running(self):
        if not self.is_running():
            self.log.debug("Set running")
            self.status = MCBackend.Status.RUNNING
            self._status_change.set()

    def __repr__(self):
        return f"MCBackend(\'{self.name}\')"

class MCServerProc(BaseAsyncContextManager):
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

    @property
    def returncode(self):
        return self.server_proc.returncode

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

    async def monitor(self):
        self.log.debug("Starting backend monitor task for pid %s", self.server_proc.pid)
        async def log_pipe(pipe, pipe_name="pipe", level=logging.DEBUG):
            pipe_logger = BytesLoggerAdapter() | PrefixLoggerAdapter(f"{pipe_name}") | self.log
            task_logger = PrefixLoggerAdapter(f"{pipe_name} logger") | self.log
            task_logger.debug("Starting backend monitor logging task")
            while True:
                msg = None
                try:
                    msg = await pipe.readuntil()
                except asyncio.LimitOverrunError as err:
                    task_logger.warning("LimitOverrunError caught after %s bytes", err.consumed)
                    msg = await pipe.read(err.consumed)
                except asyncio.IncompleteReadError as err:
                    task_logger.warning("IncompleteReadError caught after %s bytes", len(err.partial))
                    msg = err.partial
                except asyncio.CancelledError:
                    task_logger.debug("Task canceled")
                    break
                except Exception as err:
                    task_logger.exception("Exception caught: %s", err)
                if msg is not None:
                    if len(msg) == 0:
                        task_logger.debug("Pipe closed")
                        break
                    pipe_logger.log(level, msg)
            task_logger.debug("Exiting")
        try:
            await asyncio.gather(log_pipe(self.server_proc.stdout, "stdout"), log_pipe(self.server_proc.stderr, "stderr"))
        except asyncio.CancelledError:
            self.log.debug("Backend monitor task canceled")

    def __repr__(self):
        return f"MCServerProc(\'{self.name}\')"