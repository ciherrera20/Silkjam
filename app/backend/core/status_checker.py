import asyncio
import logging
from contextlib import AbstractContextManager
from mctools import AsyncPINGClient

#
# Project imports
#
from supervisor import Timer
from utils.logger_adapters import PrefixLoggerAdapter

logger = logging.getLogger(__name__)

type AsyncPINGClientFactory = callable[[], AbstractContextManager[AsyncPINGClient]]

class MCStatusChecker(Timer):
    MAX_RETRIES = 3
    RETRY_INTERVAL = 10
    INTERVAL = 1

    def __init__(
        self,
        server_name: str,
        aping_client_factory: AsyncPINGClientFactory,
    ):
        self.server_name = server_name
        self.aping_client_factory: AsyncPINGClientFactory = aping_client_factory
        self.server_list_ping_cb = None
        self.log = PrefixLoggerAdapter(logger, {"server": server_name})
        super().__init__(self.INTERVAL)

    def on_server_list_ping(self, cb):
        self.server_list_ping_cb = cb

    async def _start(self):
        await super()._start()
        self.check_nowait()  # Schedule status check immediately

    async def _stop(self, *args):
        await super()._stop(*args)

    async def run(self):
        retry_count = 0
        while True:
            await super().run()
            try:
                async with self.aping_client_factory() as client:
                    stats = await client.get_stats()
            except Exception:
                retry_count += 1
                if retry_count > self.MAX_RETRIES:
                    self.log.error("Server process not responding to status requests")
                    raise
                else:
                    self.log.warning("Server process did not respond to status request, trying again")
                await asyncio.sleep(self.RETRY_INTERVAL)
            else:
                self.log.debug("Server process responded to status request")
                retry_count = 0
                self.reset()
                if self.server_list_ping_cb is not None:
                    self.server_list_ping_cb(stats)

    def check_nowait(self):
        self.log.debug("Status check requested immediately, setting remaining time to 0")
        self.remaining = 0

    def __repr__(self):
        return "MCStatusChecker"