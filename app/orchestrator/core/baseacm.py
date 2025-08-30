from typing import Self
from contextlib import AbstractAsyncContextManager
from abc import ABC, abstractmethod

class BaseAsyncContextManager(AbstractAsyncContextManager, ABC):
    def __init__(self):
        self._started: bool = False

    @property
    def started(self):
        return self._started

    # Intended to be called directly by users of this class
    async def start(self):
        if not self._started:
            await self._start()
            self._started = True

    async def stop(self):
        if self._started:
            self._started = False
            await self._stop(None, None, None)

    # Intended to be overridden by derived classes
    @abstractmethod
    async def _start(self):
        pass

    @abstractmethod
    async def _stop(self, *args):
        pass

    # Async context manager methods, designed to be reusable
    async def __aenter__(self) -> Self:
        if not self._started:
            await self._start()
            self._started = True
        return self

    async def __aexit__(self, *args):
        if self._started:
            self._started = False
            await self._stop(*args)
        return False