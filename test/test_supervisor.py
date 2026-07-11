import asyncio
import logging
from typing import Any

from backend.supervisor import BaseUnit, Supervisor
from backend.utils.logger_adapters import PrefixLoggerAdapter


logger = logging.getLogger(__name__)

if __name__ == "__main__":
    import random
    import time

    class Unit(BaseUnit):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name
            self.log = PrefixLoggerAdapter(logger, {"unit": name})

        async def _start(self) -> None:
            self.log.info("Entering")
            self._started = True
            await asyncio.sleep(1)

        async def _stop(self, *args: Any) -> None:
            self.log.info("Exiting")
            await asyncio.sleep(1)

        async def run(self) -> None:
            i = 1
            while True:
                self.log.info("Working... [%s]", i)
                wait = random.uniform(0, 1)
                await asyncio.sleep(wait)
                # if wait > 0.5:
                #     raise Exception("Uh oh, something went wrong")
                i += 1

        async def runfunc1(self) -> None:
            while True:
                self.log.info("Running runfunc 1...")
                await asyncio.sleep(1)

        async def runfunc2(self) -> None:
            while True:
                self.log.info("Running runfunc 2...")
                await asyncio.sleep(1)

        def __repr__(self) -> str:
            return f"Unit(\'{self.name}\')"

    async def main() -> Supervisor:
        start_time = time.perf_counter()
        supervisor = Supervisor()
        async with supervisor:
            units = [Unit(f"unit {i}") for i in range(1)]
            for unit in units:
                supervisor.add_unit(unit, restart=False, stopped=False)
            await supervisor.supervise_until_done_starting(unit)
        logger.info("Duration: %ss", time.perf_counter() - start_time)
        return supervisor

    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    supervisor = asyncio.run(main())