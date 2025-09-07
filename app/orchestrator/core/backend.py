import base64
import asyncio
import logging
import jproperties
from pathlib import Path
from contextlib import AbstractContextManager

#
# Project imports
#
from .supervisor import Supervisor, Timer
from .mcproc import MCProc
from utils.logger_adapters import PrefixLoggerAdapter
from models.config import ServerListing
from models.serverproperties import ServerProperties

logger = logging.getLogger(__name__)

type PortCMFactory = callable[[], AbstractContextManager[int]]

class MCBackend(Supervisor):
    def __init__(
            self,
            root: Path,
            port_factory: PortCMFactory,
            listing: ServerListing
        ):
        super().__init__()
        self.root = root
        self.listing = listing
        self.log = PrefixLoggerAdapter(logger, listing.name)

        self.sleep_timer: Timer | None
        self.icon = None

        # Create units to supervise
        self.mcproc: MCProc = MCProc(self.root, listing.name, port_factory, self.log)
        if listing.sleep_properties.timeout is None:
            self.sleep_timer = None
            self.add_unit(self.mcproc, self.mcproc.monitor, restart=True, stopped=False)
        else:
            self.sleep_timer = Timer(listing.sleep_properties.timeout)
            self.add_unit(self.mcproc, self.mcproc.monitor, restart=True, stopped=True)
            self.add_unit(self.sleep_timer, self.sleep_timer.wait, restart=False, stopped=True)

        self._online_players = 0  # Keep track of number of players connected to server
        self._start_mcproc = asyncio.Event()
        self._stop_mcproc = asyncio.Event()
        self._online_player_change: asyncio.Event = asyncio.Event()
        self._online_player_change.set()

    @property
    def name(self):
        return self.listing.name

    @property
    def subdomain(self):
        return self.listing.subdomain

    @property
    def version(self):
        return self.listing.version

    @property
    def server_port(self):
        return self.mcproc.properties.server_port

    @property
    def rcon_port(self):
        return self.mcproc.properties.rcon_port

    @property
    def motd(self):
        if self.mcproc_running():
            return self.mcproc.properties.motd
        else:
            return self.listing.sleep_properties.motd or f"§e{self.mcproc.properties.motd}"

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
        return self.mcproc.properties.max_players

    @property
    def waking_kick_msg(self):
        return self.listing.sleep_properties.waking_kick_msg

    async def _start(self):
        await super()._start()

        # Open and read icon if it exists
        icon_path = self.root / "world" / "icon.png"
        if icon_path.exists():
            self.log.info("Reading server icon properties")
            with icon_path.open("rb") as f:
                data = f.read()
            self.icon = f"data:image/png;base64,{base64.b64encode(data).decode('utf-8')}"
        else:
            self.icon = None

    async def _stop(self, *args):
        await super()._stop(*args)

    def reload_sleep_timer(self):
        # Start or stop sleep timer
        if self.mcproc_running() and self.online_players == 0:
            self.log.debug("Starting sleep timer")
            self.start_unit_nowait(self.sleep_timer)
        else:
            self.log.debug("Stopping sleep timer")
            self.stop_unit_nowait(self.sleep_timer)

    async def serve_forever(self):
        await self.done_starting(self.mcproc)  # Await initial start
        while True:
            # Wait until the config changes or a proxy or server task is canceled or errors out
            (done_events, done_units), (_, _) = await self.supervise_until([self._start_mcproc, self._stop_mcproc, self._online_player_change])
            if self._start_mcproc in done_events:
                self.log.debug("Start server requested")
                await self.done_stopping(self.mcproc)
                await self.start_unit(self.mcproc)
                self.online_players = 0
                self._start_mcproc.clear()
            if self._stop_mcproc in done_events:
                self.log.debug("Stop server requested")
                await self.stop_unit(self.mcproc)
                self.online_players = 0
                self._stop_mcproc.clear()
            if self._online_player_change in done_events:
                self.log.debug("Online players is %s", self.online_players)
                if self.sleep_timer is not None:
                    self.reload_sleep_timer()
                self._online_player_change.clear()
            if self.sleep_timer is not None and self.sleep_timer in done_units and done_units[self.sleep_timer] is None:
                self.log.info("No players connected for %ss, stopping server", self.listing.sleep_properties.timeout)
                self.stop_mcproc()

    def mcproc_starting(self):
        return self.is_starting(self.mcproc)

    def mcproc_running(self):
        return self.is_running(self.mcproc)

    def mcproc_stopping(self):
        return self.is_stopping(self.mcproc)

    def start_mcproc(self):
        self._start_mcproc.set()

    def stop_mcproc(self):
        self._stop_mcproc.set()

    def incr_online_players(self):
        self.online_players += 1

    def decr_online_players(self):
        self.online_players -= 1

    def __repr__(self):
        return f"MCBackend(\'{self.name}\')"