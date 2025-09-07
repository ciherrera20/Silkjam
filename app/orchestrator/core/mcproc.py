import re
import signal
import asyncio
import logging
from pathlib import Path
from contextlib import suppress, AbstractContextManager, AsyncExitStack

#
# Project imports
#
from .baseacm import BaseAsyncContextManager
# from .client import MCClient
from mctools import AsyncPINGClient
from utils.logger_adapters import PrefixLoggerAdapter, BytesLoggerAdapter
from models.serverproperties import ServerProperties

type PortCMFactory = callable[[], AbstractContextManager[int]]

class MCProc(BaseAsyncContextManager):
    STARTING_SERVER_REGEX = re.compile(rb"^.*:(\d{5}).*$")
    DONE_REGEX = re.compile(rb"^.*Done \(.*$")

    STDOUT_LOG_LEVEL = logging.DEBUG
    STDERR_LOG_LEVEL = logging.ERROR

    def __init__(
            self,
            root: Path,
            name: str,
            port_factory: PortCMFactory,
            logger: logging.Logger | logging.LoggerAdapter | None=None,
            stop_timeout: int=90,
            sigint_timeout: int=90,
            sigterm_timeout: int=90
        ):
        super().__init__()
        if logger is None:
            logger = logging.getLogger(__name__)
        self.log = PrefixLoggerAdapter(logger, 'server_proc')
        self.root = root
        self.name = name
        self.port_factory = port_factory
        self.properties = ServerProperties.load(self.root / "server.properties")
        self.stop_timeout = stop_timeout
        self.sigint_timeout = sigint_timeout
        self.sigterm_timeout = sigterm_timeout
        self.stack: AsyncExitStack
        self.server_proc: asyncio.Process

        self.server_list_ping_cb = None

    async def _start(self):
        await super()._start()

        self.stack = AsyncExitStack()
        await self.stack.__aenter__()

        # Allocate ports and write server properties
        server_port = self.stack.enter_context(self.port_factory())
        rcon_port = self.stack.enter_context(self.port_factory())
        self.properties = ServerProperties.load(self.root / "server.properties")
        self.properties.server_port = server_port
        self.properties.rcon_port = rcon_port
        self.properties.dump(self.root / "server.properties")

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
        log_stdout_task = asyncio.create_task(self.log_pipe(self.server_proc.stdout, "stdout", self.STDOUT_LOG_LEVEL))
        log_stderr_task = asyncio.create_task(self.log_pipe(self.server_proc.stderr, "stderr", self.STDERR_LOG_LEVEL))

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

        await asyncio.gather(log_stdout_task, log_stderr_task)
        await self.stack.__aexit__(*args)
        await super()._stop(*args)

    async def read_stdout_until_done_initializing(self):
        pipe_logger = BytesLoggerAdapter() | PrefixLoggerAdapter(f"stdout") | self.log
        async for msg in self.server_proc.stdout:
            pipe_logger.log(self.STDOUT_LOG_LEVEL, msg)
            if m := self.STARTING_SERVER_REGEX.match(msg):
                if m.group(1) == str(self.properties.server_port).encode("utf-8"):
                    self.log.debug("Read server starting msg")
                    break

        async for msg in self.server_proc.stdout:
            pipe_logger.log(self.STDOUT_LOG_LEVEL, msg)
            if self.DONE_REGEX.match(msg):
                self.log.debug("Read done msg")
                break

    async def ping_server_until_done_initializing(self, ping_interval=1):
        while True:
            self.log.debug("Sending ping...")
            async with AsyncPINGClient("0.0.0.0", self.properties.server_port) as client:
                with suppress(Exception):
                    await client.ping()
                    break
            await asyncio.sleep(ping_interval)
        self.log.debug("Received ping response")

    async def wait_until_done_initializing(self, stdout_timeout=60, ping_timeout=60, ping_interval=5):
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
        pipe_logger = BytesLoggerAdapter() | PrefixLoggerAdapter(f"{pipe_name}") | self.log
        async for msg in pipe:
            pipe_logger.log(level, msg)

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
                if self.server_list_ping_cb is not None:
                    self.server_list_ping_cb(stats)
                await asyncio.sleep(interval)

    async def monitor(self):
        self.log.debug("Starting server process monitor task for pid %s", self.server_proc.pid)
        try:
            tasks = [
                asyncio.create_task(self.log_pipe(self.server_proc.stdout, "stdout", self.STDOUT_LOG_LEVEL)),
                asyncio.create_task(self.log_pipe(self.server_proc.stderr, "stderr", self.STDERR_LOG_LEVEL)),
                asyncio.create_task(self.request_status_continuously())
            ]
            await asyncio.gather(*tasks)
            self.log.debug("Server process (pid %s) stopped communication", self.server_proc.pid)
        except asyncio.CancelledError:
            self.log.debug("Backend monitor task canceled")
            raise
        finally:
            interrupted = []
            for task in tasks:
                if not task.done():
                    interrupted.append(task)
            await asyncio.gather(*tasks, return_exceptions=True)

    def on_server_list_ping(self, cb):
        self.server_list_ping_cb = cb

    def __repr__(self):
        return f"MCProc(\'{self.name}\')"