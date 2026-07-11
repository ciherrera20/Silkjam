from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Literal, Self


class BaseUnit(AbstractAsyncContextManager['BaseUnit'], ABC):
    def __init__(self) -> None:
        self._started: bool = False

    @property
    def started(self) -> bool:
        return self._started

    # Intended to be called directly by users of this class
    async def start(self) -> None:
        if not self._started:
            self._started = True
            await self._start()

    async def stop(self) -> None:
        if self._started:
            self._started = False
            await self._stop(None, None, None)

    # Intended to be overridden by derived classes
    @abstractmethod
    async def _start(self) -> None:
        pass

    @abstractmethod
    async def _stop(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
        pass

    @abstractmethod
    async def run(self) -> None:
        pass

    # Async context manager methods, designed to be reusable
    async def __aenter__(self) -> Self:
        if not self._started:
            self._started = True
            await self._start()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> Literal[False]:
        if self._started:
            self._started = False
            await self._stop(exc_type, exc_val, exc_tb)
        return False