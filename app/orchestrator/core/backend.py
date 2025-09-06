import base64
import asyncio
import logging
import jproperties
from pathlib import Path

#
# Project imports
#
from .protocol import MCVersion
from .supervisor import Supervisor, Timer
from .mcproc import MCProc
from utils.logger_adapters import PrefixLoggerAdapter

logger = logging.getLogger(__name__)

class MCBackend(Supervisor):
    def __init__(self, root: Path, listing: dict):
        super().__init__()
        self.root = root
        self.name = listing["name"]
        self.log = PrefixLoggerAdapter(logger, self.name)

        self.version = MCVersion(**listing["version"])
        self.subdomain = listing["subdomain"]
        self.sleep_properties = listing["sleep_properties"]
        self.sleep_timeout = self.sleep_properties["timeout"]

        self.properties = None
        self.icon = None

        # Create units to supervise
        self.mcproc: MCProc = MCProc(self)
        self.sleep_timer: Timer = Timer(self.sleep_timeout)
        self.add_unit(self.mcproc, self.mcproc.monitor, restart=True, stopped=False)
        self.add_unit(self.sleep_timer, self.sleep_timer.wait, restart=False, stopped=True)

        self._online_players = 0  # Keep track of number of players connected to server
        self._start_mcproc = asyncio.Event()
        self._stop_mcproc = asyncio.Event()
        self._online_player_change: asyncio.Event = asyncio.Event()
        self._online_player_change.set()

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
                self.reload_sleep_timer()
                self._online_player_change.clear()
            if self.sleep_timer in done_units and done_units[self.sleep_timer] is None:
                self.log.info("No players connected for %ss, stopping server", self.sleep_timeout)
                self.stop_mcproc()

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
        if self.mcproc_running():
            return motd_prop
        else:
            return self.sleep_properties.get("motd") or motd_prop

    @property
    def online_players(self):
        if self.mcproc_running():
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
        if self.mcproc_running():
            return max_players_prop
        else:
            return self.sleep_properties.get("max-players") or max_players_prop

    @property
    def waking_kick_msg(self):
        return self.sleep_properties.get("waking-kick-msg")

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