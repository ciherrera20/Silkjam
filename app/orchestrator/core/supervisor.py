if __name__ == "__main__":
    import os, sys, subprocess
    ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True).stdout.decode("utf-8").strip()
    sys.path.append(os.path.join(ROOT, 'app', 'orchestrator'))

import asyncio
from enum import IntEnum
from typing import AsyncContextManager, Iterable, Callable, Coroutine, Any
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
import logging
logger = logging.getLogger(__name__)

#
# Project imports
#
from core.baseacm import BaseAsyncContextManager
from utils.logger_adapters import PrefixLoggerAdapter

class Status(IntEnum):
    """Unit status"""
    READY = 0
    ENTERING = 1
    RUNNING = 2
    EXITING = 3
    STOPPED = 4

class Supervisor(BaseAsyncContextManager):
    """Manages the lifecycle of asynchronous units.

    Each unit is an asynchronous context manager with main coroutine to
    run within the unit's context. The supervisor is responsible for
    starting, monitoring, and stopping these units. It also handles and
    propagating or suppressing exceptions as appropriate, and restarting
    failed units.

    Lifecycle:
    A unit can have one of the following statuses:
    - READY: Unit is waiting to be started.
    - ENTERING: Unit's `aenter` coroutine is running.
    - RUNNING: Unit's main coroutine is running.
    - EXITING: Unit's `aexit` coroutine is running.
    - STOPPED: Unit is stopped and will not be started again (e.g. after
        failure or non-restart).

    Units move through the statuses as follows:
        READY -> ENTERING -> RUNNING -> EXITING -> (READY | STOPPED)

    Units can be configured to restart automatically upon exiting, or stay
    stopped until explicitly started again.

    When a unit fails at any stage, the exception is caught and stored for
    later retrieval. Only `KeyboardInterrupt` and `SystemExit` propagate.

    The supervisor is itself an asynchronous context manager: upon
    exiting, all running units are canceled at whatever stage of their
    lifecycle they are in. Afterwards, the supervisor can be reused.
    """

    @dataclass
    class State:
        """Unit state"""
        runfunc: Callable[[], Coroutine[Any, Any, None]]
        restart: bool
        status: Status = Status.READY
        task: asyncio.Task = None
        pending_stop: bool = False
        result: Any = None

        done_starting: asyncio.Event = asyncio.Event()
        done_stopping: asyncio.Event = asyncio.Event()

    class Command(IntEnum):
        """Internal commands used by the Supervisor"""
        START = 0
        STOP = 1

    FIRST_EVENT = 'FIRST_EVENT'
    FIRST_UNIT = 'FIRST_UNIT'
    FIRST_EVENT_OR_UNIT = 'FIRST_EVENT_OR_UNIT'
    ALL_EVENTS = 'ALL_EVENTS'
    ALL_UNITS = 'ALL_UNITS'
    ALL_EVENTS_AND_UNITS = 'ALL_EVENTS_AND_UNITS'

    def __init__(self):
        super().__init__()
        self.stack: AsyncExitStack
        self.log = PrefixLoggerAdapter(logger, 'supervisor')

        self._units: dict[AsyncContextManager, Supervisor.State] = {}  # unit -> Supervisor.State
        self._running_unit_tasks: dict[asyncio.Task, AsyncContextManager] = {}  # task -> unit
        self._lock: asyncio.Lock = asyncio.Lock()  # synchronize access to _running_unit_tasks
        self._command_queue: asyncio.Queue[Supervisor.Command] = asyncio.Queue()  # hold pending commands

    async def _start(self):
        """Enter supervisor context"""
        self.log.info("Entering")
        self.stack = AsyncExitStack()
        await self.stack.__aenter__()

    async def _stop(self, *args):
        """Exit supervisor context"""
        self.log.info("Exiting")
        self._handle_command_queue(allowed={})
        for unit, state in self._units.items():
            if state.task is not None:
                self._stop_unit(unit)
        await asyncio.gather(*self._running_unit_tasks)
        await self.stack.__aexit__(*args)
        del self.stack

    def add_unit(
            self,
            unit: AsyncContextManager,
            runfunc: Callable[[], Coroutine[Any, Any, None]],
            restart: bool = True,
            stopped: bool = False
        ) -> bool:
        """Add a unit. Can be called before outside of the supervisor's
        context. If the unit has already been added, nothing happens.

        Args:
            unit (AsyncContextManager): Unit to add.
            runfunc (Callable[[], Coroutine[Any, Any, None]]): Unit's main
                coroutine that runs within the unit's context and executes
                whatever functionality the unit is meant to perform.
            restart (bool, optional): Whether to restart the unit when it
                exits. Defaults to True.
            stopped (bool, optional): Whether to add the unit as stopped,
                in which case it must be explicitly started before it first
                runs. Defaults to False.

        Returns:
            bool: Whether the unit was added or not.
        """
        if unit in self._units:
            return False

        # Add unit
        self.log.debug("Adding %s", unit)
        if not stopped:
            self._units[unit] = Supervisor.State(runfunc, restart, Status.READY)
            self._command_queue.put_nowait((Supervisor.Command.START, unit))
        else:
            self._units[unit] = Supervisor.State(runfunc, restart, Status.STOPPED)
        return True

    def remove_unit_nowait(
            self,
            unit: AsyncContextManager
        ) -> bool:
        """Remove a unit. Can be called before outside of the supervisor's
        context. If the unit has not been added, or is still running, nothing
        happens.

        Args:
            unit (AsyncContextManager): Unit to remove.

        Returns:
            bool: Whether the unit was removed or not.
        """
        if unit not in self._units or self._units[unit].task is not None:
            return False
        self.log.debug("Removing %s", unit)
        del self._units[unit]
        return True

    def start_unit_nowait(
            self,
            unit: AsyncContextManager,
        ) -> bool:
        """Schedules a unit to be started by the supervisor. If the
        unit has not been added or is currently being stopped, nothing
        happens.

        Args:
            unit (AsyncContextManager): Unit to start.

        Returns:
            bool: Whether the unit will be started or not.
        """
        if unit not in self._units or self._units[unit].pending_stop:
            return False
        self._command_queue.put_nowait((Supervisor.Command.START, unit))
        return True

    async def start_unit(self, unit):
        """Start a unit and wait for it to finish starting while running all
        other units.

        Args:
            unit (AsyncContextManager): Unit to start and wait for.

        Returns:
            bool: Whether the unit was started successfully or not.
        """
        if not self.start_unit_nowait(unit):
            return False
        state = self._units[unit]
        state.done_starting.clear()
        (done_events, _), (_, _) = await self.supervise_until([state.done_starting])
        if state.done_starting in done_events:
            return True
        else:
            return False

    def stop_unit_nowait(
            self,
            unit: AsyncContextManager
        ) -> bool:
        """Schedules a unit to be stopped by the supervisor. If the unit has
        not been added, nothing happens.

        Args:
            unit (AsyncContextManager): Unit to stop.

        Returns:
            bool: Whether or not the the unit will be stopped.
        """
        if unit not in self._units:
            return False
        self._command_queue.put_nowait((Supervisor.Command.STOP, unit))
        return True

    async def stop_unit(self, unit):
        """Stop a unit and wait for it to finish stopping while running all
        other units.

        Args:
            unit (AsyncContextManager): Unit to stop and wait for.

        Returns:
            bool: Whether the unit was stopped successfully or not.
        """
        if not self.stop_unit_nowait(unit):
            return False
        state = self._units[unit]
        state.done_stopping.clear()
        (done_events, _), (_, _) = await self.supervise_until([state.done_stopping])
        if state.done_stopping in done_events:
            return True
        else:
            return False

    def _start_unit(
            self,
            unit: AsyncContextManager
        ):
        """Create background task to run unit through its lifecycle. Must be
        called within supervisor context, and unit must have been added.

        Args:
            unit (AsyncContextManager): Unit to start.
        """
        # Unit must exist
        self.log.info("Starting %s", unit)
        state = self._units[unit]
        task = asyncio.create_task(self._run_unit(unit))
        self._running_unit_tasks[task] = unit
        state.task = task

    async def _run_unit(
            self,
            unit: AsyncContextManager
        ) -> Any:
        """Coroutine to run a unit through its lifecycle. Exceptions at any
        stage are caught and stored, except for `KeyboardInterrupt` and
        `SystemExit`, which are propagated.
        
        Args:
            unit (AsyncContextManager): Unit to run.

        Returns:
            Any: Return value of the unit's main coroutine, or exception
                caught if the unit failed.
        """
        self.log.debug("Running %s", unit)
        state = self._units[unit]
        state.result = None
        state.done_starting.clear()
        state.done_stopping.clear()

        try:  # Enter unit
            state.status = Status.ENTERING
            self.log.debug("Entering %s", unit)
            await self.stack.enter_async_context(unit)
        except BaseException as err:  # Enter failed
            if isinstance(err, asyncio.CancelledError):
                self.log.error("%s canceled while entering", unit)
            else:
                self.log.exception("Exception caught while entering %s: %s", unit, err)
            if isinstance(KeyboardInterrupt, SystemExit):
                raise
            state.result = err
        else:  # Unit entered successfully
            self.log.debug("Finished entering %s", unit)
            state.done_starting.set()
            try:  # Run unit
                state.status = Status.RUNNING
                self.log.debug("Running %s", unit)
                state.result = await state.runfunc()
            except BaseException as err:  # Run failed
                if isinstance(err, asyncio.CancelledError):
                    self.log.error("%s canceled", unit)
                else:
                    self.log.exception("Exception caught while running %s: %s", unit, err)
                if isinstance(KeyboardInterrupt, SystemExit):
                    raise
                state.result = err
            else:  # Unit finished successfully
                self.log.debug("Finished running %s", unit)
        finally:  # Unit finished running or failed
            try:  # Exit unit
                state.status = Status.EXITING
                self.log.debug("Exiting %s", unit)
                if isinstance(state.result, BaseException):  # Propagate exception to unit's aexit
                    await unit.__aexit__(type(state.result), state.result, state.result.__traceback__)
                else:
                    await unit.__aexit__(None, None, None)
            except BaseException as err:  # Exit failed
                if isinstance(err, asyncio.CancelledError):
                    self.log.error("%s canceled while exiting", unit)
                else:
                    self.log.exception("Exception caught while exiting %s: %s", unit, err)
                if isinstance(KeyboardInterrupt, SystemExit):
                    raise
                state.result = err
            else:  # Unit exited successfully
                self.log.debug("Finished exiting %s", unit)
                state.done_stopping.set()
            state.status = Status.STOPPED if state.pending_stop else Status.READY
            if not self._lock.locked():
                del self._running_unit_tasks[state.task]
            state.task = None
            state.pending_stop = False
        return state.result

    def _stop_unit(
            self,
            unit: AsyncContextManager
        ):
        """Cancel the background task running a unit. Must be called within
        supervisor context, unit must have been added, and unit must currently
        be running.

        Args:
            unit (AsyncContextManager): Unit to stop.
        """
        # Unit must exist
        # Task must be in running tasks
        self.log.debug("Stopping %s", unit)
        state = self._units[unit]
        state.task.cancel()
        state.pending_stop = True

    def _handle_command_queue(
            self,
            item: tuple[Command, AsyncContextManager]=None,
            empty: bool=True,
            allowed: set[Command]={Command.START, Command.STOP}
        ):
        """Handles executing commands in the command queue.

        Args:
            item (tuple[Command, AsyncContextManager], optional): Command to
                start with. Defaults to None.
            empty (bool, optional): Whether or not to empty the command queue.
                Defaults to True.
            allowed (dict, optional): Commands to allow. Defaults to
                {Command.START, Command.STOP}.
        """
        with suppress(asyncio.QueueEmpty):
            if item is None:
                item = self._command_queue.get_nowait()
            while True:
                command, unit = item
                if command not in allowed:
                    self.log.debug("Discarding command %s %s", command.name, unit)
                else:
                    self.log.debug("Handling command %s %s", command.name, unit)
                    if command == Supervisor.Command.START:
                        if unit not in self._units:
                            self.log.debug("%s has not been added or has been removed, ignoring START command", unit)
                        elif self._units[unit].task is not None:
                            self.log.debug("%s is running, ignoring START command", unit)
                        else:
                            self._start_unit(unit)
                    elif command == Supervisor.Command.STOP:
                        if unit not in self._units:
                            self.log.debug("%s has not been added or has been removed, ignoring STOP command", unit)
                        elif self._units[unit].task is None:
                            self.log.debug("%s is not running, ignoring STOP command", unit)
                        else:
                            self._stop_unit(unit)
                if empty:
                    item = self._command_queue.get_nowait()
                else:
                    break

    async def _supervise_until(
            self,
            events: Iterable[asyncio.Event]=[],
            return_when: str=FIRST_EVENT_OR_UNIT
        ) -> tuple[
            tuple[
                set[asyncio.Event],
                dict[AsyncContextManager, None | Exception]],
            tuple[
                set[asyncio.Event],
                set[AsyncContextManager]
            ]
        ]:
            # Set up return values
            done_events = set()
            done_units = {}  # Unit -> exception?
            pending_events = set(event for event in events)
            pending_units = set(unit for unit in self._running_unit_tasks.values())

            # Set up initial tasks
            monitor_command_queue_task = asyncio.create_task(self._command_queue.get())
            pending_event_tasks = {asyncio.create_task(event.wait()): event for event in pending_events}  # task -> event

            try:
                ret = False
                while not ret:
                    # Protect critical part of supervise loop
                    async with self._lock:
                        done, pending = await asyncio.wait([monitor_command_queue_task, *pending_event_tasks, *self._running_unit_tasks], return_when=asyncio.FIRST_COMPLETED)

                    if monitor_command_queue_task in done:
                        self._handle_command_queue(monitor_command_queue_task.result())
                        monitor_command_queue_task = asyncio.create_task(self._command_queue.get())

                    for t in done:
                        if t in pending_event_tasks:
                            event = pending_event_tasks[t]
                            done_events.add(event)
                            del pending_event_tasks[t]
                            pending_events.discard(event)
                        elif t in self._running_unit_tasks:
                            unit = self._running_unit_tasks[t]
                            done_units[unit] = t.result()
                            pending_units.discard(unit)
                            if unit in self._units:
                                state = self._units[unit]
                                self.log.info('%s is done with status %s', unit, state.status.name)
                                if state.restart and state.status == Status.READY:
                                    self.log.info('Restarting %s', unit)
                                    self._command_queue.put_nowait((Supervisor.Command.START, unit))
                                state.task = None
                            del self._running_unit_tasks[t]

                    for t in pending:
                        if t in pending_event_tasks:
                            pending_events.add(pending_event_tasks[t])
                        elif t in self._running_unit_tasks:
                            pending_units.add(self._running_unit_tasks[t])

                    for unit in self._running_unit_tasks.values():
                        pending_units.add(unit)

                    # Determine whether the return condition is met
                    if return_when == Supervisor.FIRST_EVENT:
                        ret = len(done_events) > 0
                    elif return_when == Supervisor.FIRST_UNIT:
                        ret = len(done_units) > 0
                    elif return_when == Supervisor.FIRST_EVENT_OR_UNIT:
                        ret = len(done_events) > 0 or len(done_units) > 0
                    elif return_when == Supervisor.ALL_EVENTS:
                        ret = len(pending_events) == 0
                    elif return_when == Supervisor.ALL_UNITS:
                        ret = len(pending_units) == 0
                    elif return_when == Supervisor.ALL_EVENTS_AND_UNITS:
                        ret = len(pending_events) == 0 and len(pending_units) == 0
            finally:
                monitor_command_queue_task.cancel()
                with suppress(asyncio.CancelledError):
                    self._handle_command_queue(await monitor_command_queue_task, empty=False)
                for t in pending_event_tasks:
                    t.cancel()
                await asyncio.gather(*pending_event_tasks, return_exceptions=True)

            self.log.debug('Supervise until return condition met: %s', return_when)
            return (done_events, done_units), (pending_events, pending_units)

    async def supervise_until(
        self,
        events: Iterable[asyncio.Event]=[],
        return_when: str=FIRST_EVENT_OR_UNIT
    ) -> tuple[
        tuple[
            set[asyncio.Event],
            dict[AsyncContextManager, None | Exception]],
        tuple[
            set[asyncio.Event],
            set[AsyncContextManager]
        ]
    ]:
        """Supervise units until some condition is met. A set of events can be
        passed in as part of the condition to return.

        The allowed return conditions are:
            FIRST_EVENT: first event set.
            FIRST_UNIT: first unit finished.
            FIRST_EVENT_OR_UNIT: first event set or unit finished.
            ALL_EVENTS: all events set.
            ALL_UNITS: all units finished.
            ALL_EVENTS_AND_UNITS: all events set and all units finished.

        Args:
            events (Iterable[asyncio.Event], optional): Events that the
                supervisor can return at when set. Defaults to [].
            return_when (str, optional): Condition to return at. Defaults to
                FIRST_EVENT_OR_UNIT.

        Raises:
            ValueError: Invalid return condition.

        Returns:
            tuple[
                tuple[
                    set[asyncio.Event],
                    dict[AsyncContextManager, None | Exception]
                ],
                tuple[
                    set[asyncio.Event],
                    set[AsyncContextManager]
                ]
            ]: (done_events, done_units), (pending_events, pending_units).
                done_units is a dictionary from units to their results or
                exceptions caught.
        """
        # Validate return_when condition
        if return_when not in {
            Supervisor.FIRST_EVENT,
            Supervisor.FIRST_UNIT,
            Supervisor.FIRST_EVENT_OR_UNIT,
            Supervisor.ALL_EVENTS,
            Supervisor.ALL_UNITS,
            Supervisor.ALL_EVENTS_AND_UNITS
        }:
            raise ValueError(f"Invalid return_when value: {return_when}")
        await self.start()
        return await self._supervise_until(events, return_when)

    async def supervise_forever(self):
        """Supervises units forever"""
        while True:
            await self.supervise_until()

class Timer(BaseAsyncContextManager):
    def __init__(self, timeout):
        super().__init__()
        self.timeout = timeout

    async def _start(self):
        pass

    async def _stop(self, *args):
        pass

    async def wait(self):
        await asyncio.sleep(self.timeout)

    def __repr__(self):
        return f"Timer({self.timeout})"

if __name__ == "__main__":
    import random

    class Unit(BaseAsyncContextManager):
        def __init__(self, name):
            super().__init__()
            self.name = name
            self.log = PrefixLoggerAdapter(logger, name)

        async def _start(self):
            self.log.info("Entering")
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
                if wait > 0.5:
                    raise Exception("Uh oh, something went wrong")
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
            return f'Unit(\'{self.name}\')'

    async def main():
        supervisor = Supervisor()
        async with supervisor:
            units = [Unit(f'unit {i}') for i in range(3)]
            for unit in units:
                supervisor.add_unit(unit, unit.run_forever, restart=False)
            await supervisor.supervise_until(return_when=Supervisor.ALL_UNITS)
        return supervisor

    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    supervisor = asyncio.run(main())