import json
import asyncio
import logging
from pathlib import Path
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from collections import defaultdict

#
# Project imports
#
from mc_server import MCServer
from mc_proxy import MCProxy

logger = logging.getLogger(__name__)

class MCOrchestrator(AbstractAsyncContextManager):
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._acm_stack = None

        # Proxy and server listings
        self.config_file = None
        self.config = None
        self._config_changed = asyncio.Event()  # Notify run_servers whenever the server listing changes

        # Hold proxy objects and background tasks
        self.proxies = {}  # Proxy name -> MCProxy object
        self._proxy_tasks = {}  # Proxy name -> running proxy task

        # Hold server objects and background tasks
        self.servers = {}  # Server name -> MCServer object
        self._server_tasks = {}  # Server name -> running server task

    async def __aenter__(self):
        if self._acm_stack is None:
            logger.debug("Entering orchestrator")
            self._acm_stack = AsyncExitStack()

            # Enter context manager stack
            await self._acm_stack.__aenter__()

            # Open and read server listing file
            self.config_file = self._acm_stack.enter_context((self.root / "config.json").open("a+"))
            self.config_file.seek(0)
            try:
                logger.info("Reading config file")
                self.config = json.load(self.config_file)
            except json.JSONDecodeError:
                logger.info("Error reading server listing. Creating default server listing")
            self.config = {
                "proxy_listing": [
                    {
                        "name": "proxy1",
                        "enabled": True,
                        "port": 25565
                    }
                ],
                "server_listing": [
                    {
                        "name": "astraeste",
                        "subdomain": "astraeste",
                        "version": {
                            "name": "1.21.4",
                            "protocol": 769
                        },
                        "enabled": True,
                        "proxy": "proxy1",
                    }
                ]
            }
            json.dump(self.config, self.config_file, indent=4)
        return self

    async def run_servers(self):
        while True:
            # Start proxies in the listing that are not currently running
            proxy_listing_names = set()
            for listing in self.config["proxy_listing"]:
                name = listing["name"]
                proxy_listing_names.add(name)
                if name not in self.proxies:
                    logger.info("Found proxy listing entry %s", name)
                    proxy = await self._acm_stack.enter_async_context(MCProxy(listing))
                    self.proxies[name] = proxy
                    proxy_task = asyncio.create_task(proxy.serve_forever())
                    self._proxy_tasks[name] = proxy_task
                    proxy_task.add_done_callback(lambda task: self._proxy_tasks.pop(proxy.name))

            # Cleanup any proxies in the listing that no longer exist
            removed_proxy_names = set()
            for name, proxy in self.proxies.items():
                if name not in proxy_listing_names:
                    removed_proxy_names.add(name)
            for name in removed_proxy_names:
                del self.proxies[name]
                if name in self._proxy_tasks:
                    proxy_task = self._proxy_tasks[name]
                    proxy_task.cancel()
                    try:
                        await proxy_task
                    except asyncio.CancelledError:
                        pass

            # Start servers in the listing that are not currently running
            server_listing_names = set()
            for listing in self.config["server_listing"]:
                name = listing["name"]
                server_listing_names.add(name)
                if name not in self.servers:
                    logger.info("Found server listing entry %s", name)
                    mc_server = await self._acm_stack.enter_async_context(MCServer(self.root / name, listing))
                    self.servers[name] = mc_server
                    mc_server_task = asyncio.create_task(mc_server.serve_forever())
                    self._server_tasks[name] = mc_server_task
                    mc_server_task.add_done_callback(lambda task: self._server_tasks.pop(mc_server.name))

            # Cleanup any servers in the listing that no longer exist
            removed_server_names = set()
            for name, mc_server in self.servers.items():
                if name not in server_listing_names:
                    removed_server_names.add(name)
            for name in removed_server_names:
                del self.servers[name]
                if name in self._server_tasks:
                    server_task = self._server_tasks[name]
                    server_task.cancel()
                    try:
                        await server_task
                    except asyncio.CancelledError:
                        pass

            # Update all proxy backends
            proxy_backends = defaultdict(list)
            for listing in self.config["server_listing"]:
                proxy_backends[listing["proxy"]].append(self.servers[listing["name"]])
            for name, proxy in self.proxies.items():
                proxy.backends = proxy_backends[name]

            # Do nothing until the config changes
            while not self._config_changed.is_set():
                await self._config_changed.wait()
            logger.info("Config change requested")
            self._config_changed.clear()

    async def __aexit__(self, *args):
        if self._acm_stack is not None:
            # Write server listing file
            self.config_file.truncate(0)
            json.dump(self.config, self.config_file)

            # Exit all open context managers
            await self._acm_stack.__aexit__(*args)

            self._acm_stack = None
            logger.debug("Exiting orchestrator")
        return False