import json
import asyncio
import logging
from pathlib import Path
from contextlib import AbstractAsyncContextManager, AsyncExitStack

#
# Project imports
#
from mc_server import MCServer

logger = logging.getLogger(__name__)

class MCOrchestrator(AbstractAsyncContextManager):
    def __init__(self, root: str | Path):
        self.root = Path(root)

        # Create context manager stack to handle all entering and exiting
        self._acm_stack = AsyncExitStack()

        # Server listing
        self.server_listing_file = None
        self.server_listing = None

        # Hold server objects and background tasks
        self.servers = {}  # Server name -> MCServer object
        self._server_tasks = {}  # Server name -> running server task
        self._server_listing_changed = asyncio.Event()  # Notify run_servers whenever the server listing changes

    async def __aenter__(self):
        logger.debug(f"Entering")

        # Enter context manager stack
        await self._acm_stack.__aenter__()

        # Open and read server listing file
        self.server_listing_file = self._acm_stack.enter_context((self.root / "server_listing.json").open("a+"))
        self.server_listing_file.seek(0)
        try:
            logger.info("Reading server listing")
            self.server_listing = json.load(self.server_listing_file)
        except json.JSONDecodeError:
            logger.info("Error reading server listing. Creating default server listing")
            self.server_listing = {
                "server_listing": [
                    {
                        "name": "astraeste",
                        "version": {
                            "name": "1.21.4",
                            "protocol": 769
                        },
                        "enabled": True
                    }
                ]
            }
            json.dump(self.server_listing, self.server_listing_file, indent=4)

        return self

    async def __aexit__(self, *args):
        # Write server listing file
        self.server_listing_file.truncate(0)
        json.dump(self.server_listing, self.server_listing_file)

        # Exit all open context managers
        await self._acm_stack.__aexit__(*args)

        logger.debug(f"Exiting")
        return False

    async def run_servers(self):
        while True:
            # Start servers in the listing that are not currently running
            server_listing_names = set()
            for entry in self.server_listing["server_listing"]:
                name = entry["name"]
                server_listing_names.add(name)
                if name not in self.servers:
                    logger.info(f"Found server listing entry {name}")
                    mc_server = await self._acm_stack.enter_async_context(MCServer(self.root / name, entry))
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

            # Do nothing until the server listing changes
            while not self._server_listing_changed.is_set():
                await self._server_listing_changed.wait()
            self._server_listing_changed.clear()