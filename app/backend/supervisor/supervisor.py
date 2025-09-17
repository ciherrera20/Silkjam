import asyncio
from enum import IntEnum
from typing import Iterable, Any
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
import logging
logger = logging.getLogger(__name__)

#
# Project imports
#
from .base_unit import BaseUnit
from utils.logger_adapters import PrefixLoggerAdapter

class Status(IntEnum):
    """Unit status"""
    READY = 0
    ENTERING = 1
    RUNNING = 2
    EXITING = 3
    STOPPED = 4

class Supervisor(BaseUnit):
    """Manages the lifecycle of asynchronous units.

    Each unit is an asynchronous context manager with main coroutine to
    run within the unit's context. The supervisor is responsible for
    starting, monitoring, and stopping these units. It also handles and
    propagating or suppressing exceptions as appropriate, and restarting
    failed units.

    Lifecycle:
    A unit can have one of the following statuses:
    - READY: Unit is waiting to be started.
    - ENTERING: Unit's `aenter` coroutine is running, i.e. the unit is
        starting.
    - RUNNING: Unit's main coroutine is running.
    - EXITING: Unit's `aexit` coroutine is running, i.e. the unit is
        stopping.
    - STOPPED: Unit is stopped and will not be restarted automatically
        (e.g. if restart=False, or the unit was stopped explicitly).

    Units move through the statuses as follows:
        READY -> ENTERING -> RUNNING -> EXITING -> (READY | STOPPED)

    Units can be configured to restart automatically upon exiting, or stay
    stopped until explicitly started again.

    When a unit fails at any stage, the exception is caught and stored for
    later retrieval. Only `KeyboardInterrupt` and `SystemExit` propagate.

    The supervisor is itself an asynchronous context manager: upon
    exiting, all running units are stopped. A unit will be interrupted if
    it is entering or running, but not if it is exiting: it will be
    allowed to exit fully. Afterwards, the supervisor can be reused.
    """

    @dataclass
    class State:
        """Unit state"""
        restart: bool
        status: Status = Status.READY
        task: asyncio.Task = None
        result: Any = None

        # Set to true when a START command is in the queue, or the unit is
        # starting after the START command has been processed.
        pending_start: bool = False

        # Set to true when a STOP command is in the queue, or the unit is
        # stopping after the STOP command has been processed. However, it is
        # not set when a unit finishes running on its own.
        pending_stop: bool = False
        force_pending_stop: bool = False  # Whether or not to force a pending stop.

        # Set when the unit's enter is finished. Cleared when the unit starts
        # and when it finishes.
        done_entering: asyncio.Event = field(default_factory=asyncio.Event)

        # Set when the unit's exit is finished. Cleared when the unit starts.
        done_exiting: asyncio.Event = field(default_factory=asyncio.Event)

    class Command(IntEnum):
        """Internal commands used by the Supervisor"""
        START = 0
        STOP = 1

    FIRST_EVENT = "FIRST_EVENT"
    FIRST_UNIT = "FIRST_UNIT"
    FIRST_EVENT_OR_UNIT = "FIRST_EVENT_OR_UNIT"
    ALL_EVENTS = "ALL_EVENTS"
    ALL_UNITS = "ALL_UNITS"
    ALL_EVENTS_AND_UNITS = "ALL_EVENTS_AND_UNITS"

    def __init__(self):
        """Initialize supervisor"""
        super().__init__()
        self.stack: AsyncExitStack
        self.log = PrefixLoggerAdapter(logger, "supervisor")

        self._units: dict[BaseUnit, Supervisor.State] = {}  # unit -> Supervisor.State
        self._running_unit_tasks: dict[asyncio.Task, BaseUnit] = {}  # task -> unit
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
        for unit in self._units:
            self.stop_unit_nowait(unit, force=True)
        if len(self._running_unit_tasks) > 0 or not self._command_queue.empty():
            await self._supervise_until(return_when=self.ALL_UNITS)
        await self.stack.__aexit__(*args)
        del self.stack

    def _start_unit(
            self,
            unit: BaseUnit
        ):
        """Create background task to run unit through its lifecycle. Must be
        called within supervisor context, and unit must have been added.

        Args:
            unit (BaseUnit): Unit to start.
        """
        # Unit must exist
        self.log.info("Starting %s", unit)
        state = self._units[unit]
        task = asyncio.create_task(self._run_unit(unit))
        self._running_unit_tasks[task] = unit
        state.task = task

    async def _run_unit(
            self,
            unit: BaseUnit
        ) -> Any:
        """Coroutine to run a unit through its lifecycle. Exceptions at any
        stage are caught and stored, except for `KeyboardInterrupt` and
        `SystemExit`, which are propagated.
        
        Args:
            unit (BaseUnit): Unit to run.

        Returns:
            Any: Return value of the unit's main coroutine, or exception
                caught if the unit failed.
        """
        self.log.debug("Running %s", unit)
        state = self._units[unit]
        state.result = None
        state.done_entering.clear()
        state.done_exiting.clear()

        try:  # Enter unit
            state.status = Status.ENTERING

            # Handle forced pending stops before entering
            if state.pending_stop and state.force_pending_stop:
                self.log.info("%s force stopped before entering", unit)
                self._stop_unit(unit)
                await asyncio.sleep(0)

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
            state.done_entering.set()
            try:  # Run unit
                state.pending_start = False
                state.status = Status.RUNNING

                # Handle pending stops after entering
                if state.pending_stop:
                    self.log.info("%s stopped before running", unit)
                    self._stop_unit(unit)
                    await asyncio.sleep(0)

                self.log.debug("Running %s", unit)
                state.result = await unit.run()
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
            state.done_entering.set()
            try:  # Exit unit
                state.pending_start = False
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

            # Cleanup state
            state.done_exiting.set()
            state.status = Status.STOPPED if state.pending_stop else Status.READY
            state.pending_stop = state.force_pending_stop = False
            if not self._lock.locked():
                del self._running_unit_tasks[state.task]
            state.task = None
            state.done_entering.clear()
        return state.result

    def _stop_unit(
            self,
            unit: BaseUnit
        ):
        """Cancel the background task running a unit. Must be called within
        supervisor context, unit must have been added, and unit must currently
        be running.

        Args:
            unit (BaseUnit): Unit to stop.
        """
        # Unit must exist
        # Task must be in running tasks
        self.log.debug("Stopping %s", unit)
        state = self._units[unit]
        state.task.cancel()

    def _handle_command_queue(
            self,
            item: tuple[Command, BaseUnit]=None,
            allowed: set[Command]={Command.START, Command.STOP}
        ):
        """Handles executing commands in the command queue.

        Args:
            item (tuple[Command, BaseUnit], optional): Command to
                start with. Defaults to None.
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
                    if command == self.Command.START:
                        if unit not in self._units:
                            raise RuntimeError(f"{unit} has not been added or has been removed")
                        else:
                            state = self._units[unit]
                            if state.task is not None:
                                raise RuntimeError(f"{unit} is already running")
                            else:
                                self._start_unit(unit)
                    elif command == self.Command.STOP:
                        if unit not in self._units:
                            raise RuntimeError(f"{unit} has not been added or has been removed")
                        else:
                            state = self._units[unit]
                            if state.status == Status.ENTERING:
                                if state.force_pending_stop:
                                    self.log.debug("%s is currently starting, forcing STOP", unit)
                                    self._stop_unit(unit)
                                else:
                                    self.log.debug("%s is currently starting, scheduling STOP for after it finishes starting", unit)
                            elif state.pending_start:
                                if state.force_pending_stop:
                                    self.log.debug("%s is pending start, scheduling forced STOP for when it starts", unit)
                                else:
                                    self.log.debug("%s is pending start, scheduling STOP for after it finishes starting", unit)
                            elif state.task is None:
                                raise RuntimeError(f"{unit} is not running")
                            else:
                                self._stop_unit(unit)
                item = self._command_queue.get_nowait()

    async def _supervise_until(
            self,
            events: Iterable[asyncio.Event]=[],
            return_when: str=FIRST_EVENT_OR_UNIT
        ) -> tuple[
            tuple[
                set[asyncio.Event],
                dict[BaseUnit, None | BaseException]],
            tuple[
                set[asyncio.Event],
                set[BaseUnit]
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

            Returns:
                tuple[
                    tuple[
                        set[asyncio.Event],
                        dict[BaseUnit, None | BaseException]
                    ],
                    tuple[
                        set[asyncio.Event],
                        set[BaseUnit]
                    ]
                ]: (done_events, done_units), (pending_events, pending_units).
                    done_units is a dictionary from units to their results or
                    exceptions caught.
            """
            # Set up return values
            done_events = set()
            done_units = {}  # Unit -> exception?
            pending_events = set(event for event in events)
            pending_units = set(unit for unit in self._running_unit_tasks.values())

            # Use task group to handle cancellations cleanly
            async with asyncio.TaskGroup() as tg:

                # Set up initial tasks
                monitor_command_queue_task = tg.create_task(self._command_queue.get())
                pending_event_tasks = {tg.create_task(event.wait()): event for event in pending_events}  # task -> event

                # Main supervise loop
                ret = False
                while not ret:
                    # Start/restart any READY units
                    for unit, state in self._units.items():
                        if state.restart and state.status == Status.READY and not state.pending_start:
                            self.log.info("Restarting %s", unit)
                            self.start_unit_nowait(unit)

                    # Protect critical part of supervise loop
                    async with self._lock:
                        done, pending = await asyncio.wait([monitor_command_queue_task, *pending_event_tasks, *self._running_unit_tasks], return_when=asyncio.FIRST_COMPLETED)

                    # Handle any commands received
                    if monitor_command_queue_task in done:
                        self._handle_command_queue(monitor_command_queue_task.result())
                        monitor_command_queue_task = tg.create_task(self._command_queue.get())

                    # Handle done events and units
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
                                self.log.info("%s is done with status %s", unit, state.status.name)
                                state.task = None
                            del self._running_unit_tasks[t]

                    # Handle pending events and units
                    for t in pending:
                        if t in pending_event_tasks:
                            pending_events.add(pending_event_tasks[t])
                        elif t in self._running_unit_tasks:
                            pending_units.add(self._running_unit_tasks[t])

                    # Add any new running unit tasks
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

                # Cancel remaining tasks
                monitor_command_queue_task.cancel()
                for t in pending_event_tasks:
                    t.cancel()

            # Return information about done and pending events and units
            self.log.debug("supervise_until return condition met: %s", return_when)
            return (done_events, done_units), (pending_events, pending_units)

    def add_unit(
            self,
            unit: BaseUnit,
            restart: bool = True,
            stopped: bool = False
        ) -> bool:
        """Add a unit. Can be called before the supervisor is started either
        by entering it using async with or by directly calling start.

        Args:
            unit (BaseUnit): Unit to add.
            restart (bool, optional): Whether to restart the unit when it
                exits. Defaults to True.
            stopped (bool, optional): Whether to add the unit as stopped,
                in which case it must be explicitly started before it first
                runs. Defaults to False.

        Returns:
            bool: True if the unit was successfully added. False if the unit
                was already added.
        """
        if unit in self._units:
            return False

        # Add unit
        self.log.debug("Adding %s", unit)
        if not stopped:
            self._units[unit] = self.State(restart, Status.READY)
            self.start_unit_nowait(unit)
        else:
            self._units[unit] = self.State(restart, Status.STOPPED)
        return True

    def remove_unit_nowait(
            self,
            unit: BaseUnit
        ) -> bool:
        """Remove a unit. Can be called before the supervisor is started
        either by entering it using async with or by directly calling start.

        Args:
            unit (BaseUnit): Unit to remove.

        Returns:
            bool: True if the unit was successfully removed. False if the unit
                has not been added or the unit is currently starting, running,
                or stopping.
        """
        if unit not in self._units or self._units[unit].task is not None or self._units[unit].pending_start:
            return False
        self.log.debug("Removing %s", unit)
        del self._units[unit]
        return True

    def start_unit_nowait(
            self,
            unit: BaseUnit,
        ) -> bool:
        """Schedule a unit to be started by the supervisor.

        A unit currently scheduled to be stopped or currently stopping will
        not be scheduled to be started.

        Args:
            unit (BaseUnit): Unit to start.

        Returns:
            bool: True if the unit was successfully scheduled to be started,
                was already scheduled to be started, or is currently starting
                or running. False if the unit has not been added, has a stop
                scheduled, or is currently being stopped.
        """
        if unit not in self._units or self._units[unit].pending_stop:
            return False

        state = self._units[unit]
        if state.status in {Status.READY, Status.STOPPED}:
            if not state.pending_start:
                self._command_queue.put_nowait((self.Command.START, unit))
                state.pending_start = True
            state.status = Status.READY
            return True
        elif state.status in {Status.ENTERING, Status.RUNNING}:
            return True
        elif state.status == Status.EXITING:
            return False

    async def supervise_until_done_starting(
            self,
            unit: BaseUnit
        ) -> bool:
        """Wait for a unit to finish starting while running all other units.

        Args:
            unit (BaseUnit): Unit to wait for.

        Returns:
            bool: True if the unit finished starting successfully or was
                already started and is currently running. False if the unit
                has not been added, does not have a start scheduled and is not
                currently starting, or errored out while starting.
        """
        if unit not in self._units:
            return False

        state = self._units[unit]
        if state.status == Status.RUNNING:
            return True
        elif state.pending_start or state.status == Status.ENTERING:
            while True:
                (done_events, done_units), (_, _) = await self.supervise_until([state.done_entering])
                if unit in done_units and isinstance(done_units[unit], BaseException):
                    return False
                elif state.done_entering in done_events:
                    return True
        else:
            return False

    async def done_starting(
        self,
        unit: BaseUnit
    ) -> bool:
        """Wait for a unit to finish starting without running other units.

        Meant to be called by tasks running parallel to supervise_until, which
        cannot be run in more than one task at a time.

        Args:
            unit (BaseUnit): Unit to wait for.

        Returns:
            bool: True if the unit finished starting successfully or was
                already started and is currently running. False if the unit
                has not been added, does not have a start scheduled and is not
                currently starting, or errored out while starting.
        """
        if unit not in self._units:
            return False

        state = self._units[unit]
        if state.status == Status.RUNNING:
            return True
        elif state.pending_start or state.status == Status.ENTERING:
            await state.done_entering.wait()
            if isinstance(state.result, BaseException):
                return False
            else:
                return True
        else:
            return False

    async def start_unit(
            self,
            unit: BaseUnit
        ) -> bool:
        """Schedule a unit to start if it does not already have a start
        scheduled and wait for it to finish starting while running all other
        units.

        Args:
            unit (BaseUnit): Unit to start and wait for.

        Returns:
            bool: True if the unit started or was already started and is
                currently running. False if the unit has not been added, has a
                stop scheduled, is currently being stopped, or errored out
                while starting.
        """
        if not self.start_unit_nowait(unit):
            return False
        return await self.supervise_until_done_starting(unit)

    def stop_unit_nowait(
            self,
            unit: BaseUnit,
            force: bool=False
        ) -> bool:
        """Schedule a unit to be stopped by the supervisor.

        A unit not currently stopped will always be scheduled to be stopped,
        no matter its current state. If a unit is currently starting, or is
        not yet starting but has a start scheduled, the stop will be scheduled
        after the unit finishes starting, unless the unit is force stopped, in
        which case the unit starting will be interrupted. If the unit is
        currently running, the stop is scheduled immediately.

        Args:
            unit (BaseUnit): Unit to stop.
            force (bool, optional): Whether or not to force stop the unit.

        Returns:
            bool: True if the unit was successfully scheduled to be stopped.
                False if the unit has not been added.
        """
        if unit not in self._units:
            return False

        state = self._units[unit]
        if state.status in {Status.READY, Status.STOPPED}:  # Unit is not currently running
            if state.pending_start:  # But it has a start pending, so we schedule a stop for after it has started
                if not state.pending_stop:
                    self._command_queue.put_nowait((self.Command.STOP, unit))
                    state.pending_stop = True
                    state.force_pending_stop |= force
            else:
                # Update state to STOPPED
                state.status = Status.STOPPED
            return True
        elif state.status in {Status.ENTERING, Status.RUNNING}:
            if not state.pending_stop:
                self._command_queue.put_nowait((self.Command.STOP, unit))
                state.pending_stop = True
                state.force_pending_stop |= force
            return True
        elif state.status == Status.EXITING:
            state.pending_stop = True
            return True

    async def supervise_until_done_stopping(
            self,
            unit: BaseUnit
        ) -> bool:
        """Wait for a unit to finish stopping while running all other units.

        Args:
            unit (BaseUnit): Unit to wait for.

        Returns:
            bool: True if the unit finished stopping successfully or was
                already stopped. False if the unit has not been added, does
                not have a stop scheduled and is not currently stopped, or
                errored out while stopping.
        """
        if unit not in self._units:
            return False

        state = self._units[unit]
        if state.status == Status.STOPPED:
            return True
        elif state.pending_stop or state.status == Status.EXITING:
            while True:
                (done_events, done_units), (_, _) = await self.supervise_until([state.done_exiting])
                if unit in done_units and isinstance(done_units[unit], BaseException):
                    return False
                elif state.done_exiting in done_events:
                    return True
        else:
            return False

    async def done_stopping(
            self,
            unit: BaseUnit
        ) -> bool:
        """Wait for a unit to finish stopping without running other units.

        Meant to be called by tasks running parallel to supervise_until, which
        cannot be run in more than one task at a time.

        Args:
            unit (BaseUnit): Unit to wait for.

        Returns:
            bool: True if the unit finished stopping successfully or was
                already stopped. False if the unit has not been added, does
                not have a stop scheduled and is not currently stopped, or
                errored out while stopping.
        """
        if unit not in self._units:
            return False

        state = self._units[unit]
        if state.status == Status.STOPPED:
            return True
        elif state.pending_stop or state.status == Status.EXITING:
            await state.done_exiting.wait()
            if isinstance(state.result, BaseException):
                return False
            else:
                return True
        else:
            return False

    async def stop_unit(
            self,
            unit: BaseUnit,
            force: bool=False
        ) -> bool:
        """Schedule a unit to stop if it does not already have a stop
        scheduled and wait for it to finish stopping while running all other
        units.

        If a unit is currently starting, or is not yet starting but has a
        start scheduled, the stop will be scheduled after the unit finishes
        starting, unless the unit is force stopped, in which case the unit
        starting will be interrupted. If the unit is currently running, the
        stop is scheduled immediately.

        Args:
            unit (BaseUnit): Unit to stop and wait for.
            force (bool, optional): Whether or not to force stop the unit.

        Returns:
            bool: True if the unit stopped or was already stopped. False if
                the unit has not been added.
        """
        if not self.stop_unit_nowait(unit, force=force):
            return False
        return await self.supervise_until_done_stopping(unit)

    def status(
            self,
            unit: BaseUnit
        ) -> Status | None:
        """Get the status of a unit.

        The possible statuses are:
        - READY: Unit is waiting to be started.
        - ENTERING: Unit's `aenter` coroutine is running, i.e. the unit is
            starting.
        - RUNNING: Unit's main coroutine is running.
        - EXITING: Unit's `aexit` coroutine is running, i.e. the unit is
            stopping.
        - STOPPED: Unit is stopped and will not be restarted automatically
            (e.g. if restart=False, or the unit was stopped explicitly).

        Args:
            unit (BaseUnit): Unit to return the status for.

        Returns:
            Status | None: Unit's status, or None if the unit has not
                been added.
        """
        if unit not in self._units:
            return None
        return self._units[unit].status

    def is_ready(
            self,
            unit: BaseUnit
        ) -> bool | None:
        """Check wether a unit is currently ready.

        Args:
            unit (BaseUnit): Unit to check.

        Returns:
            bool | None: Whether the unit is currently ready, or None if the
                unit has not been added.
        """
        if unit not in self._units:
            return None
        return self._units[unit].status == Status.STOPPED

    def is_starting(
            self,
            unit: BaseUnit
        ) -> bool | None:
        """Check wether a unit is currently starting.

        Args:
            unit (BaseUnit): Unit to check.

        Returns:
            bool | None: Whether the unit is currently starting, or None if
                the unit has not been added.
        """
        if unit not in self._units:
            return None
        return self._units[unit].status == Status.ENTERING

    def is_running(
            self,
            unit: BaseUnit
        ) -> bool | None:
        """Check wether a unit is currently running.

        Args:
            unit (BaseUnit): Unit to check.

        Returns:
            bool | None: Whether the unit is currently running, or None if the
                unit has not been added.
        """
        if unit not in self._units:
            return None
        return self._units[unit].status == Status.RUNNING

    def is_stopping(
            self,
            unit: BaseUnit
        ) -> bool | None:
        """Check wether a unit is currently stopping.

        Args:
            unit (BaseUnit): Unit to check.

        Returns:
            bool | None: Whether the unit is currently stopping, or None if
                the unit has not been added.
        """
        if unit not in self._units:
            return None
        return self._units[unit].status == Status.EXITING

    def is_stopped(
            self,
            unit: BaseUnit
        ) -> bool | None:
        """Check wether a unit is currently stopped.

        Args:
            unit (BaseUnit): Unit to check.

        Returns:
            bool | None: Whether the unit is currently stopped, or None if the
                unit has not been added.
        """
        if unit not in self._units:
            return None
        return self._units[unit].status == Status.STOPPED

    async def supervise_until(
        self,
        events: Iterable[asyncio.Event]=[],
        return_when: str=FIRST_EVENT_OR_UNIT
    ) -> tuple[
        tuple[
            set[asyncio.Event],
            dict[BaseUnit, None | BaseException]],
        tuple[
            set[asyncio.Event],
            set[BaseUnit]
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
                    dict[BaseUnit, None | BaseException]
                ],
                tuple[
                    set[asyncio.Event],
                    set[BaseUnit]
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

    async def run(self):
        """Supervises units forever"""
        while True:
            await self.supervise_until()