import re
import signal
import asyncio
import logging
from contextlib import suppress

#
# Project imports
#
from .baseacm import BaseAsyncContextManager
from .client import MCClient
from utils.logger_adapters import PrefixLoggerAdapter

class MCProc(BaseAsyncContextManager):
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
        self._started = True  # Mark unit as started so that cleanup will still run if start gets interrupted after this point
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
            raise

    def __repr__(self):
        return f"MCProc(\'{self.name}\')"