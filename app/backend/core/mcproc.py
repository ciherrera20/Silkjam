import re
import nbtlib
import signal
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from contextlib import suppress, AbstractContextManager, AsyncExitStack

#
# Project imports
#
from .baseacm import BaseAsyncContextManager
from mctools import AsyncPINGClient, AsyncRCONClient
from utils.logger_adapters import PrefixLoggerAdapter, BytesLoggerAdapter
from utils.backups import get_stale_backups
from models.serverproperties import ServerProperties
from models.config import BackupProperties

type PortCMFactory = callable[[], AbstractContextManager[int]]

class MCProc(BaseAsyncContextManager):
    STARTING_SERVER_REGEX = re.compile(rb"^.*:(\d{5}).*$")
    DONE_REGEX = re.compile(rb"^.*Done \(.*$")

    DATE_FMT = "%Y-%m-%d_%H:%M:%S"
    BACKUP_REGEX = re.compile(r".*?(\d+)\.tar\.gz")
    TICKS_PER_MINUTE = 20 * 60

    STDOUT_LOG_LEVEL = logging.DEBUG
    STDERR_LOG_LEVEL = logging.ERROR

    def __init__(
            self,
            root: Path,
            name: str,
            port_factory: PortCMFactory,
            backup_root: Path,
            backup_properties: BackupProperties,
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
        self.backup_root = backup_root
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.backup_properties = backup_properties
        self.stop_timeout = stop_timeout
        self.sigint_timeout = sigint_timeout
        self.sigterm_timeout = sigterm_timeout
        self.stack: AsyncExitStack
        self.server_proc: asyncio.Process

        self.server_list_ping_cb = None

    @property
    def tick_interval(self):
        return self.backup_properties.interval * self.TICKS_PER_MINUTE

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
        self.properties.enable_rcon = True
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

        async with asyncio.TaskGroup() as tg:
            log_stderr_task = tg.create_task(self.log_pipe(self.server_proc.stderr, "stderr", self.STDERR_LOG_LEVEL))
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

    async def backup_continuously(self, min_interval=60, retry_interval=5, max_retries=3):
        while True:
            self.log.debug("Checking if backup is needed")

            # Grab current backups from backup directory
            backups = []
            for p in self.backup_root.iterdir():
                if p.is_file():
                    if m := self.BACKUP_REGEX.fullmatch(p.name):
                        ts = int(m.group(1))
                        backups.append((ts, p))
            backups.sort()

            # Read world time
            nbtfile = nbtlib.load(self.root / "world" / "level.dat")
            total_ticks = int(nbtfile.root["Data"]["Time"])
            self.log.debug("Total ticks is %s", total_ticks)
            if len(backups) == 0:
                self.log.debug("No backups found")
            else:
                self.log.debug("Ticks since last backup is %s, tick_interval is %s", total_ticks - backups[-1][0], self.tick_interval)

            create_backup = len(backups) == 0 or ((total_ticks - backups[-1][0]) // self.tick_interval) > 0
            if create_backup:
                name = f"world_{datetime.now().strftime(self.DATE_FMT)}_{total_ticks}.tar.gz"
                backup = self.backup_root / name
                tmp = self.backup_root / (name + ".part")

                self.log.debug("Starting backup %s", backup.name)
                try:
                    tmp.touch(exist_ok=True)
                    async with AsyncRCONClient("0.0.0.0", self.properties.rcon_port) as client:
                        await client.login(self.properties.rcon_password)
                        await client.command("say Backing up world")
                        await client.command("save-off")
                        response = await client.command("save-all")
                        if "Saved the game" not in response:
                            raise RuntimeError("save-all command failed with the following response: %s", response)

                        retry_count = 0
                        while True:
                            await asyncio.sleep(retry_interval)
                            proc = await asyncio.create_subprocess_exec(
                                "tar",
                                "-czf",
                                tmp,
                                "-C",
                                self.root / "world",
                                ".",
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE
                            )
                            stdout, stderr = await proc.communicate()
                            stdout = stdout.decode("utf-8")
                            stderr = stderr.decode("utf-8")

                            self.log.debug("tar process stdout: %s", stdout)
                            self.log.debug("tar process stderr: %s", stderr)

                            if proc.returncode != 0:
                                await client.command("say Backup failed with fatal error")
                                raise RuntimeError("tar process failed with return code %s and stderr output %s", proc.returncode, stderr.decode("utf-8"))

                            if len(stderr) == 0:
                                self.log.debug("Backup %s created successfully", backup.name)
                                await client.command(f"say Backup created successfully")
                                break

                            if retry_count > max_retries:
                                await client.command("say Backup failed, max retries exceeded")
                                raise RuntimeError("Exceeded the maximum number of backup retries: %s", max_retries)
                            else:
                                self.log.debug("Retrying backup")
                                await client.command("say Backup failed with warning, retrying backup")
                                tmp.unlink(missing_ok=True)
                                retry_count += 1

                        tmp.rename(backup)  # atomic "commit"
                        await client.command("save-on")
                    self.log.info("Created backup %s", backup)
                except Exception as err:
                    self.log.exception("Exception caught while creating backup: %s", err)
                finally:
                    if tmp.is_file():
                        self.log.warning("Failed to create backup %s", backup)
                        tmp.unlink()  # cleanup leftover partials

                # Remove stale backups
                stale_backups = get_stale_backups(backups, total_ticks, self.backup_properties.max_backups, self.tick_interval, key=lambda backup: backup[0])
                for _, p in stale_backups:
                    self.log.info("Removing stale backup %s", p.name)
                    p.unlink(missing_ok=True)

                sleep_time = max(min_interval, self.backup_properties.interval * 60)  # Convert to seconds
            else:
                self.log.debug("No backup needed")
                sleep_time = max(min_interval, (self.tick_interval - (total_ticks - backups[-1][0])) // 20)  # Calculate time until next backup

            # Sleep until next backup check
            self.log.debug("Next backup check scheduled in %ss", sleep_time)
            await asyncio.sleep(sleep_time)

    async def monitor(self):
        self.log.debug("Starting server process monitor task for pid %s", self.server_proc.pid)
        async with asyncio.TaskGroup() as tg:
            status_task = tg.create_task(self.request_status_continuously())
            if self.backup_properties is not None:
                backup_task = tg.create_task(self.backup_continuously())
            async with asyncio.TaskGroup() as proc_tg:
                proc_tg.create_task(self.log_pipe(self.server_proc.stdout, "stdout", self.STDOUT_LOG_LEVEL))
                proc_tg.create_task(self.log_pipe(self.server_proc.stderr, "stderr", self.STDERR_LOG_LEVEL))
            self.log.debug("Server process (pid %s) stopped communication", self.server_proc.pid)
            status_task.cancel()
            if self.backup_properties is not None:
                backup_task.cancel()

    def on_server_list_ping(self, cb):
        self.server_list_ping_cb = cb

    def __repr__(self):
        return f"MCProc(\'{self.name}\')"