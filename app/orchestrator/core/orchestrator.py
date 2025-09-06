import os
import json
import asyncio
import logging
from pathlib import Path
from collections import defaultdict

#
# Project imports
#
from .supervisor import Supervisor
from .backend import MCBackend
from .proxy import MCProxy
from utils.logger_adapters import PrefixLoggerAdapter
from contextlib import contextmanager

logger = logging.getLogger(__name__)

MINECRAFT_PORT = os.environ.get("MINECRAFT_PORT", 25565)
SERVER_PORTS = os.environ.get("SERVER_PORTS", "40000:45000")

class PortSlotError(RuntimeError):
    pass

class MCOrchestrator(Supervisor):
    def __init__(self, root: str | Path):
        super().__init__()
        self.log = PrefixLoggerAdapter(logger, 'orchestrator')

        self.root = Path(root)

        # Proxy and server listings
        self.config_file = None
        self.config = None
        self._config_changed = asyncio.Event()  # Notify run_servers whenever the server listing changes

        self.proxies = {}  # Proxy name -> MCProxy object
        self.backends = {}  # Server name -> MCBackend object

        # Create server port range and keep track of allocated ports
        self.server_port_range = tuple(int(p) for p in SERVER_PORTS.split(":"))
        self.acquired_server_ports = set()

    async def _start(self):
        await super()._start()

        # Open and read config file
        self._config_changed.set()
        self.config_file = self.stack.enter_context((self.root / "config.json").open("a+"))
        self.config_file.seek(0)
        try:
            self.log.info("Reading config file")
            self.config = json.load(self.config_file)
            self.log.info("%s", self.config)
        except json.JSONDecodeError:
            self.log.info("Error reading config. Creating default config")
            self.config = {
                "proxy_listing": [
                    {
                        "name": "proxy1",
                        "enabled": True,
                        "port": MINECRAFT_PORT
                    }
                ],
                "server_listing": []
            }
            self.config_file.truncate(0)
            json.dump(self.config, self.config_file, indent=4)
            self.config_file.flush()

    @contextmanager
    def acquire_port(self):
        # Find available port
        port = None
        lo, hi = self.server_port_range
        for p in range(lo, hi):
            if p not in self.acquired_server_ports:
                port = p
                break

        # Allocate it
        if port is None:
            raise PortSlotError(f"Could not acquire port in range {lo}:{hi}")

        self.log.debug("Acquiring port %s", port)
        self.acquired_server_ports.add(port)

        # Yield and deallocate after
        try:
            yield port
        finally:
            self.log.debug("Releasing port %s", port)
            self.acquired_server_ports.discard(port)

    def reload_config(self):
        # Start proxies in the listing that are not currently running
        proxy_listing_names = set()
        for listing in self.config["proxy_listing"]:
            name = listing["name"]
            proxy_listing_names.add(name)
            if name not in self.proxies:
                self.log.info("Found proxy listing entry %s", name)
                proxy = MCProxy(listing)
                self.proxies[name] = proxy
                self.add_unit(proxy, proxy.serve_forever)

        # Cleanup any proxies in the listing that no longer exist
        removed_proxy_names = set()
        for name, proxy in self.proxies.items():
            if name not in proxy_listing_names:
                self.remove_unit_nowait(proxy)
                removed_proxy_names.add(name)
        for name in removed_proxy_names:
            del self.proxies[name]

        # Start servers in the listing that are not currently running
        backend_names = set()
        for listing in self.config["server_listing"]:
            name = listing["name"]
            backend_names.add(name)
            if name not in self.backends:
                self.log.info("Found server listing entry %s", name)
                backend = MCBackend(self.root / name, self.acquire_port, listing)
                self.backends[name] = backend
                self.add_unit(backend, backend.serve_forever)

        # Cleanup any servers in the listing that no longer exist
        removed_backend_names = set()
        for name, backend in self.backends.items():
            if name not in backend_names:
                self.remove_unit_nowait(backend)
                removed_backend_names.add(name)
        for name in removed_backend_names:
            del self.backends[name]

        # Update all proxy backends
        proxy_backends = defaultdict(list)
        for listing in self.config["server_listing"]:
            proxy_backends[listing["proxy"]].append(self.backends[listing["name"]])
        for name, proxy in self.proxies.items():
            proxy.backends = proxy_backends[name]

    async def run_servers(self):
        while True:
            # Wait until the config changes or a proxy or server task is canceled or errors out
            (done_events, done_units), (_, _) = await self.supervise_until([self._config_changed], return_when=Supervisor.FIRST_EVENT_OR_UNIT)
            if self._config_changed in done_events:
                self.log.debug("Config change requested")
                self.reload_config()
                self._config_changed.clear()
            for unit in done_units:
                if isinstance(unit, MCBackend):
                    result = done_units[unit]
                    if isinstance(result, PortSlotError):
                        self.log.error("Stopping %s: could not acquire backend port", unit.name)
                        self.stop_unit_nowait(unit)

    async def _stop(self, *args):
        # Write server listing file
        self.config_file.truncate(0)
        json.dump(self.config, self.config_file, indent=4)
        await super()._stop(*args)