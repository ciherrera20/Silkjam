import asyncio
from pathlib import Path
import time
import signal
import psutil
import logging
logger = logging.getLogger(__name__)

async def monitor_mc_servers(datapath: str):
    tasks = []
    for item in Path(datapath).iterdir():
        if item.is_dir():
            tasks.append(asyncio.create_task(monitor_mc_server(item)))
    await asyncio.gather(*tasks)

async def stop_mc_server(proc: psutil.Process | asyncio.subprocess.Process, stop_timeout: None | int = 90, sigint_timeout: None | int = 90, sigterm_timeout: None | int = 90, sigkill: bool = True, polling_interval: None | int = 1):
    """Attempts to gracefully shut down a given process by first
    sending a SIGINT, then a SIGTERM, then a SIGKILL.

    Args:
        proc (psutil.Process | asyncio.subprocess.Process): Process to
            shutdown. Can be a psutil Process instance, or an asyncio
            Process instance. The latter can be communicated with via
            stdin.
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
    """
    try:
        if isinstance(proc, asyncio.subprocess.Process):
            # Send stop command and wait before progressing to SIGINT
            logger.debug(f"Sending stop command to server {proc.pid}")
            proc.stdin.write(b"stop\n")
            await proc.stdin.drain()
            await asyncio.wait_for(proc.wait(), stop_timeout)

            # Send SIGINT and wait before progressing to SIGTERM
            logger.debug(f"Sending SIGINT to server {proc.pid}")
            proc.send_signal(signal.SIGINT)
            await asyncio.wait_for(proc.wait(), stop_timeout)

            # Send SIGTERM and wait before progressing to SIGKILL
            logger.debug(f"Sending SIGTERM to server {proc.pid}")
            proc.send_signal(signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), stop_timeout)

            # Send SIGKILL
            logger.debug(f"Sending SIGKILL to server {proc.pid}")
            proc.send_signal(signal.SIGKILL)
        else:
            if sigint_timeout is not None:
                # Send SIGINT and wait before progressing to SIGTERM
                logger.debug(f"Sending SIGINT to process {proc.pid}")
                start_time = time.perf_counter()
                proc.send_signal(signal.SIGINT)
                if polling_interval is None:
                    await asyncio.sleep(sigint_timeout)
                else:
                    while time.perf_counter() - start_time < sigint_timeout:
                        if not proc.is_running():
                            return
                        await asyncio.sleep(min(polling_interval, start_time + sigint_timeout - time.perf_counter()))
                if not proc.is_running():
                    return

            if sigterm_timeout is not None:
                # Send SIGTERM and wait before progressing to SIGKILL
                logger.debug(f"Sending SIGTERM to process {proc.pid}")
                start_time = time.perf_counter()
                proc.send_signal(signal.SIGTERM)
                if polling_interval is None:
                    await asyncio.sleep(sigterm_timeout)
                else:
                    while time.perf_counter() - start_time < sigterm_timeout:
                        if not proc.is_running():
                            return
                        await asyncio.sleep(min(polling_interval, start_time + sigterm_timeout - time.perf_counter()))
                if not proc.is_running():
                    return

            if sigkill:
                # Send SIGKILL
                logger.debug(f"Sending SIGKILL to process {proc.pid}")
                proc.send_signal(signal.SIGTERM)
    except PermissionError:
        logger.debug(f"Failed to kill process {proc.pid} with PermissionError")
    except Exception as ex:
        logger.exception(ex)

async def start_mc_server(serverpath: Path) -> asyncio.subprocess.Process:
    # Figure out if server is running already and stop it if it is
    pid_file = Path("/tmp") / "mc" / serverpath.parts[-1] / "server.pid"
    if pid_file.exists():
        try:
            with pid_file.open("r") as f:
                pid = int(f.read(100))
            if psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                if "java" in proc.name().lower() or "java" in " ".join(proc.cmdline()).lower():
                    logger.info(f"Found java process with pid {pid}, attempting to stop it")
                    await stop_mc_server(proc)
        except ValueError:
            logger.debug(f"Invalid pid in {pid_file}")
        except FileNotFoundError:
            logger.debug(f"Could not find {pid_file}")
    else:
        pid_file.parent.mkdir(parents=True)

    # Start mc server
    logger.info(f"Starting server {serverpath.stem}")
    server_jar_file = serverpath / "server.jar"
    proc = await asyncio.create_subprocess_exec(
        "java", "-Xmx2G", "-jar", server_jar_file, "nogui",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=serverpath
    )
    with pid_file.open("w") as f:
        f.write(str(proc.pid))
    return proc

async def monitor_mc_server(serverpath: Path):
    while True:
        proc = await start_mc_server(serverpath)
        # await proc.wait()
        # Read stdout line by line asynchronously
        while True:
            line = await proc.stdout.readline()
            if not line:
                break  # EOF
            print(line.decode().rstrip())  # decode bytes and strip newline

        returncode = await proc.wait()
        print(f"Process exited with code {returncode}")