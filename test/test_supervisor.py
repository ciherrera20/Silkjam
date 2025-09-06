import asyncio
from contextlib import suppress
import logging
logger = logging.getLogger(__name__)


#
# Project imports
#
import os, sys, subprocess
ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True).stdout.decode("utf-8").strip()
sys.path.append(os.path.join(ROOT, "app", "orchestrator"))
from core.baseacm import BaseAsyncContextManager
from core.supervisor import Supervisor
from utils.logger_adapters import PrefixLoggerAdapter

if __name__ == "__main__":
    import time
    import random

    class Unit(BaseAsyncContextManager):
        def __init__(self, name):
            super().__init__()
            self.name = name
            self.log = PrefixLoggerAdapter(logger, name)

        async def _start(self):
            self.log.info("Entering")
            self._started = True
            await asyncio.sleep(1)

        async def _stop(self, *args):
            self.log.info("Exiting")
            await asyncio.sleep(1)

        async def run_forever(self):
            i = 1
            while True:
                self.log.info("Working... [%s]", i)
                wait = random.uniform(0, 1)
                await asyncio.sleep(wait)
                # if wait > 0.5:
                #     raise Exception("Uh oh, something went wrong")
                i += 1

        async def runfunc1(self):
            while True:
                self.log.info("Running runfunc 1...")
                await asyncio.sleep(1)

        async def runfunc2(self):
            while True:
                self.log.info("Running runfunc 2...")
                await asyncio.sleep(1)

        def __repr__(self):
            return f"Unit(\'{self.name}\')"

    async def main():
        start_time = time.perf_counter()
        supervisor = Supervisor()
        async with supervisor:
            units = [Unit(f"unit {i}") for i in range(1)]
            for unit in units:
                supervisor.add_unit(unit, unit.run_forever, restart=False, stopped=False)
            await supervisor.done_starting(unit)
        logger.info("Duration: %ss", time.perf_counter() - start_time)
        return supervisor

    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    supervisor = asyncio.run(main())