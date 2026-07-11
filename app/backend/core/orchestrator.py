import os
import asyncio
import logging
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Generator

#
# Project imports
#
from backend.supervisor import Supervisor
from backend.core.backend import MCBackend
from backend.core.proxy import MCProxy
from backend.models import Config, UNKNOWN_VERSION

logger = logging.getLogger(__name__)

MINECRAFT_PORT = os.environ.get("MINECRAFT_PORT", 25565)
SERVER_PORTS = os.environ.get("SERVER_PORTS", "40000:45000")

class AcquirePortError(RuntimeError):
    pass

class MCOrchestrator(Supervisor):
    def __init__(self, root: str | Path):
        super().__init__()

        self.root = Path(root)

        # Proxy and server listings
        self.config: Config
        self._config_changed = asyncio.Event()  # Notify run_servers whenever the server listing changes

        self.proxies: dict[str, MCProxy] = {}  # Proxy name -> MCProxy object
        self.backends: dict[str, MCBackend] = {}  # Server name -> MCBackend object

        # Create server port range and keep track of allocated ports
        self.server_port_range = tuple(int(p) for p in SERVER_PORTS.split(":"))
        self.acquired_server_ports: set[int] = set()

    async def _start(self) -> None:
        await super()._start()

        # Open and read config file
        self.config = Config.load(self.root / "config.json")
        self.config.dump(self.root / "config.json")
        self._config_changed.set()

    @contextmanager
    def acquire_port(self) -> Generator[int]:
        # Find available port
        port = None
        lo, hi = self.server_port_range
        for p in range(lo, hi):
            if p not in self.acquired_server_ports:
                port = p
                break

        # Allocate it
        if port is None:
            raise AcquirePortError(f"Could not acquire port in range {lo}:{hi}")

        logger.debug("Acquiring port %s", port)
        self.acquired_server_ports.add(port)

        # Yield port and deallocate after its done being used
        try:
            yield port
        finally:
            logger.debug("Releasing port %s", port)
            self.acquired_server_ports.discard(port)

    def update_config(self) -> None:
        # Dump updated config and flag change
        self.config.dump(self.root / "config.json")
        self.config.validate_semantics()
        self._config_changed.set()

    def reload_config(self) -> None:
        # Start proxies in the listing that are not currently running
        proxy_listing_names = set()
        for proxy_name, proxy_listing in self.config.proxy_listing.items():
            proxy_listing_names.add(proxy_name)
            if proxy_name not in self.proxies:
                logger.info("Found proxy listing entry %s", proxy_name)
                if not proxy_listing.valid:
                    logger.error("Skipping invalid proxy listing entry %s with the following error(s):", proxy_name)
                    for msg in proxy_listing.errors:
                        logger.error(msg)
                elif not proxy_listing.enabled:
                    logger.info("Skipping disabled proxy listing entry %s", proxy_name)
                else:
                    proxy = MCProxy(proxy_name, self.backends, proxy_listing)
                    self.proxies[proxy_name] = proxy
                    self.add_unit(proxy)

        # Cleanup any proxies in the listing that no longer exist
        removed_proxy_names = set()
        for proxy_name, proxy in self.proxies.items():
            if proxy_name not in proxy_listing_names:
                self.remove_unit_nowait(proxy)
                removed_proxy_names.add(proxy_name)
        for proxy_name in removed_proxy_names:
            del self.proxies[proxy_name]

        # Start servers in the listing that are not currently running
        backend_names = set()
        for backend_name, server_listing in self.config.server_listing.items():
            backend_names.add(backend_name)
            if backend_name not in self.backends:
                logger.info("Found server listing entry %s", backend_name)
                if not server_listing.valid:
                    logger.error("Skipping invalid server listing entry %s with the following error(s):", backend_name)
                    for msg in server_listing.errors:
                        logger.error(msg)
                elif not server_listing.enabled:
                    logger.info("Skipping disabled server listing entry %s", backend_name)
                else:
                    backend = MCBackend(
                        backend_name,
                        self.root / "servers" / backend_name,
                        self.acquire_port,
                        server_listing,
                        self
                    )
                    backend.on_listing_change(self.update_config)
                    self.backends[backend_name] = backend
                    self.add_unit(backend, restart=False, stopped=server_listing.sleep_properties.timeout is not None and server_listing.version != UNKNOWN_VERSION)

        # Cleanup any servers in the listing that no longer exist
        removed_backend_names = set()
        for backend_name, backend in self.backends.items():
            if backend_name not in backend_names:
                self.remove_unit_nowait(backend)
                removed_backend_names.add(backend_name)
        for backend_name in removed_backend_names:
            del self.backends[backend_name]

    async def run(self) -> None:
        while True:
            # Wait until the config changes or a proxy or server task is canceled or errors out
            (done_events, done_units), (_, _) = await self.supervise_until([self._config_changed], return_when=Supervisor.FIRST_EVENT_OR_UNIT)
            if self._config_changed in done_events:
                logger.debug("Config change requested")
                self.reload_config()
                self._config_changed.clear()
            for unit in done_units:
                if isinstance(unit, MCBackend):
                    result = done_units[unit]
                    if isinstance(result, AcquirePortError):
                        logger.error("Stopping %s: could not acquire backend port", unit.name)
                        self.stop_unit_nowait(unit)

    async def _stop(self, *args: Any) -> None:
        # Write server listing file
        await super()._stop(*args)