import os
import json
import asyncio
import logging
from pathlib import Path
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from collections import defaultdict
from weakref import WeakKeyDictionary

#
# Project imports
#
from .backend import MCBackend
from .proxy import MCProxy

logger = logging.getLogger(__name__)

MINECRAFT_PORT = os.environ.get("MINECRAFT_PORT", 25565)

class MCOrchestrator(AbstractAsyncContextManager):
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._acm_stack = None

        # Proxy and server listings
        self.config_file = None
        self.config = None
        self._config_changed = asyncio.Event()  # Notify run_servers whenever the server listing changes

        self.proxies = {}  # Proxy name -> MCProxy object
        self.backends = {}  # Server name -> MCBackend object

    async def __aenter__(self):
        if self._acm_stack is None:
            logger.debug("Entering orchestrator")
            self._acm_stack = AsyncExitStack()

            # Enter context manager stack
            await self._acm_stack.__aenter__()

            # Open and read config file
            self.config_file = self._acm_stack.enter_context((self.root / "config.json").open("a+"))
            self.config_file.seek(0)
            try:
                logger.info("Reading config file")
                self.config = json.load(self.config_file)
                logger.info("%s", self.config)
            except json.JSONDecodeError as err:
                logger.error(err)
                logger.info("Error reading config. Creating default config")
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
        return self

    async def run_servers(self):
        # Keep track of async context managers being run and the tasks used to run them
        task_to_acm = WeakKeyDictionary()  # task -> async context manager
        acm_to_task = WeakKeyDictionary()  # async context manager -> task

        while True:
            # Recreate task list when config changes
            task_list = []

            # Start proxies in the listing that are not currently running
            proxy_listing_names = set()
            for listing in self.config["proxy_listing"]:
                name = listing["name"]
                proxy_listing_names.add(name)
                if name not in self.proxies:
                    logger.info("Found proxy listing entry %s", name)
                    proxy = await self._acm_stack.enter_async_context(MCProxy(listing))
                    self.proxies[name] = proxy
                    run_proxy_task = asyncio.create_task(proxy.serve_forever(), name=name)
                    acm_to_task[proxy] = run_proxy_task
                    task_to_acm[run_proxy_task] = proxy
                    task_list.append(run_proxy_task)

            # Cleanup any proxies in the listing that no longer exist
            removed_proxy_names = set()
            for name, proxy in self.proxies.items():
                if name not in proxy_listing_names:
                    acm_to_task[proxy].cancel()  # Will be awaited later
                    removed_proxy_names.add(name)
            for name in removed_proxy_names:
                del self.proxies[name]

            # Start servers in the listing that are not currently running
            backend_names = set()
            for listing in self.config["server_listing"]:
                name = listing["name"]
                backend_names.add(name)
                if name not in self.backends:
                    logger.info("Found server listing entry %s", name)
                    backend = await self._acm_stack.enter_async_context(MCBackend(self.root / name, listing))
                    self.backends[name] = backend
                    run_backend_task = asyncio.create_task(backend.serve_forever(), name=name)
                    acm_to_task[backend] = run_backend_task
                    task_to_acm[run_backend_task] = backend
                    task_list.append(run_backend_task)

            # Cleanup any servers in the listing that no longer exist
            removed_backend_names = set()
            for name, backend in self.backends.items():
                if name not in backend_names:
                    acm_to_task[backend].cancel()  # Will be awaited later
                    removed_backend_names.add(name)
            for name in removed_backend_names:
                del self.backends[name]

            # Update all proxy backends
            proxy_backends = defaultdict(list)
            for listing in self.config["server_listing"]:
                proxy_backends[listing["proxy"]].append(self.backends[listing["name"]])
            for name, proxy in self.proxies.items():
                proxy.backends = proxy_backends[name]

            # Do nothing until the config changes or a proxy or server task errors out
            while not self._config_changed.is_set():
                monitor_config_task = asyncio.create_task(self._config_changed.wait())
                done, task_list = await asyncio.wait([monitor_config_task] + task_list, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task is not monitor_config_task:
                        try:
                            await task
                        except Exception as err:
                            logger.exception("Exception caught while running %s: %s", task.get_name(), err)
                            await task_to_acm[task].__aexit__(err, None, None)
                        else:
                            await task_to_acm[task].__aexit__(None, None, None)
            logger.info("Config change requested")
            self._config_changed.clear()

    async def __aexit__(self, *args):
        if self._acm_stack is not None:
            # Write server listing file
            self.config_file.truncate(0)
            json.dump(self.config, self.config_file, indent=4)

            # Exit all open context managers
            await self._acm_stack.__aexit__(*args)

            self._acm_stack = None
            logger.debug("Exiting orchestrator")
        return False