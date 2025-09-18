import re
import nbtlib
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from contextlib import AbstractContextManager, asynccontextmanager
from mctools import AsyncRCONClient

#
# Project imports
#
from supervisor import Timer
from utils.logger_adapters import PrefixLoggerAdapter
from utils.backup_strategies import get_stale_backups
from models import BackupProperties

logger = logging.getLogger(__name__)

type AsyncRCONClientFactory = callable[[], AbstractContextManager[AsyncRCONClient]]

class MCBackupManager(Timer):
    DATE_FMT = "%Y-%m-%d_%H:%M:%S"
    BACKUP_REGEX = re.compile(r".*?(\d+)\.tar\.gz")
    PARTIAL_BACKUP_REGEX = re.compile(rf"{BACKUP_REGEX.pattern}\.part")
    TICKS_PER_SECOND = 20
    TICKS_PER_MINUTE = TICKS_PER_SECOND * 60
    MIN_INTERVAL = 60

    def __init__(
            self,
            server_name: str,
            root: Path,
            backup_root: Path,
            properties: BackupProperties,
            arcon_client_factory: AsyncRCONClientFactory,
        ):
        self.server_name = server_name
        self.root = root
        self.backup_root = backup_root
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.properties = properties
        self.arcon_client_factory = arcon_client_factory
        self.log = PrefixLoggerAdapter(logger, {"server": server_name})
        super().__init__(max(self.MIN_INTERVAL, self.properties.interval * 60))

    @property
    def tick_interval(self):
        return self.properties.interval * self.TICKS_PER_MINUTE

    async def _start(self):
        await super()._start()
        self.check_nowait()  # Schedule status check immediately

    async def _stop(self, *args):
        await super()._stop(*args)

    def load_play_time(self):
        # Read play time from level.dat
        nbtfile = nbtlib.load(self.root / "world" / "level.dat")
        return int(nbtfile.root["Data"]["Time"])

    def load_backups_list(self) -> list[tuple[int, Path]]:
        # Grab current backups from backup directory
        backups = []
        self.backup_root.mkdir(parents=True, exist_ok=True)
        for p in self.backup_root.iterdir():
            if p.is_file():
                if m := self.BACKUP_REGEX.fullmatch(p.name):
                    ts = int(m.group(1))
                    backups.append((ts, p))
                elif m := self.PARTIAL_BACKUP_REGEX.fullmatch(p.name):
                    self.log.warning("Removing partial backup %s", p.name)
                    p.unlink(missing_ok=True)
        backups.sort()
        return backups

    @asynccontextmanager
    async def autosave_off(self, client: AsyncRCONClient=None):
        if client is None:
            # Create client if it was not provided and close it when done
            async with self.arcon_client_factory() as client:
                async with self.autosave_off(client) as client:
                    yield client
        else:
            # Check if auto save is on and turn it off if it is
            response = await client.command("save-off")
            if "Automatic saving is now disabled" in response:
                prev_autosave_on = True
            elif "Saving is already turned off" in response:
                prev_autosave_on = False
            else:
                raise RuntimeError("save-off command failed with the following response: %s", response)

            # Do stuff with auto save off
            yield client

            # Turn auto save back on if it was on
            if prev_autosave_on:
                await client.command("save-on")

    async def create_world_archive(
            self,
            name: str,
            retry_interval: int=5,
            max_retries: int=3
        ):
        backup: Path = self.backup_root / name
        tmp: Path = self.backup_root / (name + ".part")
        tmp.touch(exist_ok=True)

        try:
            for _ in range(max_retries):
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
                self.log.debug("tar process returncode=%s, stdout=%s, stderr=%s", proc.returncode, stdout, stderr)

                # Check for errors
                if proc.returncode != 0:
                    # Fatal error ocurred
                    raise RuntimeError(f"Creating backup {name} failed with return code {proc.returncode} and error message: {stderr}")
                elif len(stderr) > 0:
                    # Non fatal error ocurred
                    self.log.warning("Retrying backup %s: stdout=%s, stderr=%s", name, stdout, stderr)
                    await asyncio.sleep(retry_interval)
                else:
                    # Backup succeeded
                    tmp.rename(backup)  # atomic "commit"
                    return

            # Loop finished, meaning all retries failed
            raise RuntimeError(f"Creating backup {name} failed: exceeded the maximum number of backup retries ({max_retries})")
        finally:
            if tmp.is_file():
                self.log.warning("Removing partial backup %s", tmp.name)
                tmp.unlink(missing_ok=True)  # cleanup leftover partials

    async def save_and_backup(self, name: str):
        self.log.debug("Starting backup %s", name)
        try:
            async with self.autosave_off() as client:
                await client.command("say Backing up world")

                # Save all chunks to disk
                response = await client.command("save-all")
                if "Saved the game" not in response:
                    raise RuntimeError("save-all command failed with the following response: %s", response)

                # Try creating backup
                try:
                    await self.create_world_archive(name)
                except RuntimeError as err:
                    await client.command("say §4Backup failed")
                else:
                    await client.command("say §2Backup succeeded")
                    self.log.info("Created backup %s", name)
        except Exception as err:
            self.log.exception("Exception caught while creating backup: %s", err)

    def remove_stale_backups(
            self,
            backups: list[tuple[int, Path]],
            total_ticks: int
        ):
        # Remove stale backups
        stale_backups = get_stale_backups(backups, total_ticks, self.properties.max_backups, self.tick_interval, key=lambda backup: backup[0])
        for _, p in stale_backups:
            self.log.info("Removing stale backup %s", p.name)
            p.unlink(missing_ok=True)

    async def run(self):
        while True:
            # Sleep until next backup check
            self.log.debug("Next backup check scheduled in %ss", self.remaining)
            await super().run()

            self.log.debug("Checking if backup is needed")
            backups = self.load_backups_list()

            # Read world time
            total_ticks = self.load_play_time()
            self.log.debug("Total ticks is %s", total_ticks)
            if len(backups) == 0:
                self.log.debug("No backups found")
            else:
                self.log.debug("Ticks since last backup is %s, tick_interval is %s", total_ticks - backups[-1][0], self.tick_interval)

            if self.properties.max_backups > 0:
                if len(backups) == 0 or (total_ticks - backups[-1][0]) // self.tick_interval > 0:
                    await self.save_and_backup(f"world_{datetime.now().strftime(self.DATE_FMT)}_{total_ticks}.tar.gz")
                    self.reset()
                else:
                    self.log.debug("No backup needed")
                    self.remaining = max(self.MIN_INTERVAL, (self.tick_interval - (total_ticks - backups[-1][0])) // 20)  # Calculate time until next backup
            else:
                self.log.debug("Backups disabled")
                self.timeout = None

            # Remove any stale backups
            self.remove_stale_backups(backups, total_ticks)

    def check_nowait(self):
        self.log.debug("Backup check requested immediately, setting remaining time to 0")
        self.remaining = 0

    def __repr__(self):
        return "MCBackupManager"