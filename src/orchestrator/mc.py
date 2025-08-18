import time
import signal
import base64
import psutil
import asyncio
import logging
import jproperties
from pathlib import Path
from orchestrator.mcproxy import mc_proxy_server
from enum import IntEnum
logger = logging.getLogger(__name__)

class MCServer:
    class Status(IntEnum):
        SLEEPING = 0
        STARTING = 1
        RUNNING = 2
        STOPPING = 3

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.name = root.stem
        self.proc = None
        self.status = MCServer.Status.SLEEPING
        self.load_properties()

    def load_properties(self):
        self.properties = jproperties.Properties()
        with (self.root / "server.properties").open("r") as f:
            self.properties.load(f.read())
        self.port = int(self.properties["server-port"].data)
        self.max_players = int(self.properties["max-players"].data)
        self.motd = self.properties["motd"].data

        # Get favicon data if it exists
        icon_path = self.root / "world" / "icon.png"
        if icon_path.exists():
            with icon_path.open("rb") as f:
                data = f.read()
            self.favicon_data = f"data:image/png;base64,{base64.b64encode(data).decode('utf-8')}"
        else:
            self.favicon_data = None

    def is_sleeping(self):
        return self.status == MCServer.Status.SLEEPING

    def is_starting(self):
        return self.status == MCServer.Status.STARTING

    def is_running(self):
        return self.status == MCServer.Status.RUNNING

    def is_stopping(self):
        return self.status == MCServer.Status.STOPPING

    async def stop(
            self, stop_timeout: None | int = 90,
            sigint_timeout: None | int = 90, sigterm_timeout: None | int = 90, sigkill: bool = True,
            polling_interval: None | int = 1
        ) -> bool:
        """Attempts to gracefully shut down the Minecraft server process
        by first sending a SIGINT, then a SIGTERM, then a SIGKILL.

        Args:
            stop_timeout (None | int, optional): Timeout after sending
                stop command to the server before progressing to signals.
                If None, or if the process is a psutil Process, the stop
                command is skipped.
                Defaults to 90.
            sigint_timeout (None | int, optional): Timeout after sending
                SIGINT before progressing to the next signal. If None, the
                SIGINT signal is skipped.
                Defaults to 90.
            sigterm_timeout (None | int, optional): Timeout after sending
                SIGTERM before progressing to the next signal. If None,
                the SIGTERM signal is skipped.
                Defaults to 90.
            sigkill (bool, optional): Whether to send a SIGKILL signal.
                Defaults to True.
            polling_interval (None | int, optional): Polling interval when
                checking if the process shut down. If None, no polling is
                performed, the process is checked only after the given
                timeout. Only applicable for psutil Process instances.
                Defaults to 1.

        Returns:
            bool: Whether the server process was stopped successfully.
        """
        if not self.is_running():
            return False

        try:
            self.status = MCServer.Status.STOPPING
            if isinstance(self.proc, asyncio.subprocess.Process) and self.proc.is_running():
                # Send stop command and wait before progressing to SIGINT
                logger.debug(f"Sending stop command to server {self.proc.pid}")
                self.proc.stdin.write(b"stop\n")
                await self.proc.stdin.drain()
                await asyncio.wait_for(self.proc.wait(), stop_timeout)

                # Send SIGINT and wait before progressing to SIGTERM
                logger.debug(f"Sending SIGINT to server {self.proc.pid}")
                self.proc.send_signal(signal.SIGINT)
                await asyncio.wait_for(self.proc.wait(), stop_timeout)

                # Send SIGTERM and wait before progressing to SIGKILL
                logger.debug(f"Sending SIGTERM to server {self.proc.pid}")
                self.proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self.proc.wait(), stop_timeout)

                # Send SIGKILL
                logger.debug(f"Sending SIGKILL to server {self.proc.pid}")
                self.proc.send_signal(signal.SIGKILL)
            else:
                if sigint_timeout is not None:
                    # Send SIGINT and wait before progressing to SIGTERM
                    logger.debug(f"Sending SIGINT to process {self.proc.pid}")
                    start_time = time.perf_counter()
                    self.proc.send_signal(signal.SIGINT)
                    if polling_interval is None:
                        await asyncio.sleep(sigint_timeout)
                    else:
                        while time.perf_counter() - start_time < sigint_timeout:
                            if not self.proc.is_running():
                                break
                            await asyncio.sleep(min(polling_interval, start_time + sigint_timeout - time.perf_counter()))

                if self.proc.is_running() and sigterm_timeout is not None:
                    # Send SIGTERM and wait before progressing to SIGKILL
                    logger.debug(f"Sending SIGTERM to process {self.proc.pid}")
                    start_time = time.perf_counter()
                    self.proc.send_signal(signal.SIGTERM)
                    if polling_interval is None:
                        await asyncio.sleep(sigterm_timeout)
                    else:
                        while time.perf_counter() - start_time < sigterm_timeout:
                            if not self.proc.is_running():
                                break
                            await asyncio.sleep(min(polling_interval, start_time + sigterm_timeout - time.perf_counter()))

                if self.proc.is_running() and sigkill:
                    # Send SIGKILL
                    logger.debug(f"Sending SIGKILL to process {self.proc.pid}")
                    self.proc.send_signal(signal.SIGTERM)
            self.status = MCServer.Status.SLEEPING
            self.proc = None
        except PermissionError:
            logger.debug(f"Failed to kill process {self.proc.pid} with PermissionError")
            self.status = MCServer.Status.RUNNING
        except Exception as err:
            logger.exception(err)
            self.status = MCServer.Status.RUNNING
        return True

    async def start(self) -> bool:
        if not self.is_sleeping():
            return False

        # Figure out if server is actually running and stop it if it is
        pid_file = Path("/tmp") / "mc" / self.root.parts[-1] / "server.pid"
        if pid_file.exists():
            try:
                with pid_file.open("r") as f:
                    pid = int(f.read(100))
                if psutil.pid_exists(pid):
                    proc = psutil.Process(pid)
                    if "java" in proc.name().lower() or "java" in " ".join(proc.cmdline()).lower():
                        logger.info(f"Found running java process with pid {pid}, attempting to stop it")
                        self.proc = proc
                        await self.stop()
            except ValueError:
                logger.debug(f"Invalid pid in {pid_file}")
            except FileNotFoundError:
                logger.debug(f"Could not find {pid_file}")
        else:
            pid_file.parent.mkdir(parents=True)

        # Start mc server
        logger.info(f"Starting server {self.name}")
        server_jar_file = self.root / "server.jar"
        self.proc = await asyncio.create_subprocess_exec(
            "java", "-Xmx2G", "-jar", server_jar_file, "nogui",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.root
        )
        with pid_file.open("w") as f:
            f.write(str(self.proc.pid))
        self.status = MCServer.Status.RUNNING
        return True

class MCOrchestrator:
    def __init__(self, root: str | Path, tmp: str | Path = "/tmp/mc"):
        self.root = Path(root)
        self.tmp = Path(tmp)
        self.servers = {}
        for child in Path(self.root).iterdir():
            if child.is_dir() and (child / "server.jar").exists():
                self.servers[child.stem] = MCServer(child)

    async def run_servers(self):
        tasks = []
        for server in self.servers.values():
            tasks.append(asyncio.create_task(self.run_server(server)))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            interrupted = [task for task in tasks if not task.done()]
            for task in interrupted:
                task.cancel()
            await asyncio.gather(*interrupted, return_exceptions=True)

    async def run_server(self, server: MCServer):
        async with mc_proxy_server(server) as proxy_server:
            await proxy_server.serve_forever()