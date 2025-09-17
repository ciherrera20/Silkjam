import time
import asyncio
from contextlib import suppress
import logging

#
# Project imports
#
from .base_unit import BaseUnit

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

class Timer(BaseUnit):
    def __init__(self, timeout):
        super().__init__()
        self._timeout = timeout
        self._timeout_changed = asyncio.Event()
        self._timer_reset = asyncio.Event()
        self._start_time: float

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        self._timeout_changed.set()
        self._timeout = value
        return value

    @property
    def remaining(self):
        if self.timeout is None:
            return None
        else:
            return max((self._start_time + self.timeout) - time.perf_counter(), 0)

    def reset(self):
        self._timer_reset.set()

    async def _start(self):
        self._start_time = time.perf_counter()

    async def _stop(self, *args):
        del self._start_time

    async def run(self):
        with suppress(asyncio.TimeoutError):
            while self.remaining is None or self.remaining > 0:
                logger.debug("Time remaining is %ss", self.remaining)
                await asyncio.wait_for(
                    asyncio.wait(
                        (
                            asyncio.create_task(self._timeout_changed.wait()),
                            asyncio.create_task(self._timer_reset.wait())
                        ),
                        return_when=asyncio.FIRST_COMPLETED
                    ),
                    self.remaining
                )
                if self._timeout_changed.is_set():
                    logger.debug("Timeout changed to %ss", self.timeout)
                    self._timeout_changed.clear()
                if self._timer_reset.is_set():
                    logger.debug("Timer reset")
                    self._start_time = time.perf_counter()
                    self._timer_reset.clear()
        logger.debug("Timer done")

    def __repr__(self):
        return f"Timer({self.timeout})"