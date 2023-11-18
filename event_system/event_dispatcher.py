import asyncio
import logging

from typing import Callable, Any, Set, List, Dict

from .event_listener import EventListener, Priority
from .event import Event


class EventDispatcher:
    """
    EventDispatcher handles event listeners, triggers events, and manages asynchronous execution of listeners.
    """
    UNLIMITED_RESPONDERS = -1

    _busy_listeners: Set['Callable'] = set()

    def __init__(self, debug_mode: bool = False):
        """
        Initialize the EventDispatcher.

        :param debug_mode: Enable debug mode for logging.
        """
        self.debug_mode = debug_mode
        self._listeners: Dict[str, List['EventListener']] = {}
        self._cancel_events = False

        self._event_loop = asyncio.get_event_loop()
        self._event_queue = asyncio.Queue()
        self._is_event_loop_running = False

    def start(self):
        """
        Start the event loop if not already running.
        """
        if not self._is_event_loop_running:
            self._event_loop.create_task(self._event_loop_runner())
            self._is_event_loop_running = True

    async def close(self):
        """
        Close the event loop and wait for queued events to be processed and ran.
        """

        # wait for all events in the queue to be processed
        while self._event_queue.qsize():
            await asyncio.sleep(0.15)

        tasks = []
        for task in asyncio.all_tasks(loop=self._event_loop):
            if task.get_coro().__name__ == "_async_trigger":
                tasks.append(task)
        # wait for all the running events to finish
        await asyncio.gather(*tasks)
        self._is_event_loop_running = False

    def add_listener(self, event_name: str, listener: Callable, priority: Priority = Priority.NORMAL) -> None:
        """
        Add a listener to the event.

        :param event_name: Name of the event.
        :param listener: Callable object representing the listener function.
        :param priority: Priority of the listener.
        """
        if callable(listener):
            self._register_event_listener(event_name, listener, priority)
            self._sort_listeners(event_name)
        else:
            raise ValueError("Listener must be callable (a function or method).")

    def remove_listener(self, event_name: str, listener: Callable) -> None:
        """
        Remove a listener from the event.

        :param event_name: Name of the event.
        :param listener: Callable object representing the listener function.
        """
        for event_listener in self._listeners.get(event_name):
            if event_listener.callback == listener:
                self._listeners.get(event_name).remove(event_listener)
                return  # To ensure only one instance is removed

    def trigger(self, event: Event, *args, **kwargs) -> None:
        """
        Trigger the event and notify all registered listeners.

        :param event: The event to trigger.
        :param args: Additional arguments to pass to listeners.
        :param kwargs: Additional keyword arguments to pass to listeners.
        """
        if not self._is_event_loop_running:
            raise Exception("No event loop running")
        self._event_queue.put_nowait((self._trigger, event, args, kwargs))

    def _trigger(self, event: Event, *args, **kwargs) -> None:
        """
        Internal method to trigger the event and notify all registered listeners.

        :param event: The event to trigger.
        :param args: Additional arguments to pass to listeners.
        :param kwargs: Additional keyword arguments to pass to listeners.
        """
        if event.event_name not in self._listeners or self._cancel_events:
            return

        responses = 0
        for listener in self._listeners[event.event_name]:
            if event.max_responders != EventDispatcher.UNLIMITED_RESPONDERS and responses >= event.max_responders:
                return

            if self.debug_mode:
                self._log_listener_call(listener, event, False)

            listener.callback(event, *args, **kwargs)
            responses += 1

    async def async_trigger(self, event: Event, *args: Any, **kwargs: Any) -> None:
        """
        Asynchronously trigger the event and notify registered listeners.

        :param event: The event to trigger.
        :param args: Additional arguments to pass to listeners.
        :param kwargs: Additional keyword arguments to pass to listeners.
        """
        if not self._is_event_loop_running:
            raise Exception("No event loop running")
        self._event_queue.put_nowait((self._async_trigger, event, args, kwargs))

    def async_trigger_nw(self, event: Event, *args: Any, **kwargs: Any) -> None:
        """
        Asynchronously trigger the event and notify registered listeners without waiting.

        :param event: The event to trigger.
        :param args: Additional arguments to pass to listeners.
        :param kwargs: Additional keyword arguments to pass to listeners.
        """
        if not self._is_event_loop_running:
            raise Exception("No event loop running")
        self._event_queue.put_nowait((self._async_trigger, event, args, kwargs))

    async def _async_trigger(self, event: Event, *args: Any, **kwargs: Any) -> None:
        """
        Internal method to asynchronously trigger the event and notify registered listeners.

        :param event: The event to trigger.
        :param args: Additional arguments to pass to listeners.
        :param kwargs: Additional keyword arguments to pass to listeners.
        """
        if self._cancel_events:
            return

        listeners = self._listeners.get(event.event_name, [])

        # Determine the maximum number of responders to process.
        # If event.max_responders is not set to an unlimited amount of responders,
        # use the max_responders value specified in the event. Otherwise, set the
        # value to the total number of listeners for this event.
        max_responders = event.max_responders if event.max_responders != EventDispatcher.UNLIMITED_RESPONDERS else len(
            listeners)

        # Asynchronously execute listeners for the event.
        await asyncio.gather(
            *[self._run_async_listener(listener, event, *args, **kwargs) for listener in listeners[:max_responders]])

    async def _run_async_listener(self, listener: EventListener, event: Event, *args, **kwargs):
        """
        Asynchronously run the specified listener for the given event.

        :param listener: The listener to run.
        :param event: The event being processed.
        :param args: Additional arguments to pass to the listener.
        :param kwargs: Additional keyword arguments to pass to the listener.
        """
        if self.debug_mode:
            self._log_listener_call(listener, event, True)

        if listener.callback not in self._busy_listeners or event.allow_busy_trigger:
            self._busy_listeners.add(listener.callback)
            await listener.callback(event, *args, **kwargs)
            self._remove_busy_listener(listener.callback)

    def disable_all_events(self) -> None:
        """
        Disable all events from being triggered.
        """
        self._cancel_events = True

    def enable_all_events(self) -> None:
        """
        Enable all events to be triggered.
        """
        self._cancel_events = False

    def is_queue_empty(self) -> bool:
        """
        Check if the event queue is empty.

        :return: True if the event queue is empty, False otherwise.
        """
        return self._event_queue.empty()

    def queue_size(self) -> int:
        """
        Get the size of the event queue.

        :return: The number of events in the queue.
        """
        return self._event_queue.qsize()

    def _register_event_listener(self, event_name: str, callback: Callable, priority: Priority) -> None:
        """
        Register an event listener for the specified event.

        :param event_name: Name of the event.
        :param callback: Callable object representing the listener function.
        :param priority: Priority of the listener.
        """
        listener = EventListener(callback=callback, priority=priority)

        # if the callback is already registered in the event, return
        if listener.callback in [lstener for lstener in self._listeners.get(event_name, [])]:
            return

        if event_name in self._listeners:
            self._listeners[event_name].append(listener)
        else:
            self._listeners.update({event_name: [listener]})

    def _sort_listeners(self, event_name: str) -> None:
        """
        Sort the listeners for the specified event based on their priorities.

        :param event_name: Name of the event.
        """
        if event_name not in self._listeners:
            return
        self._listeners[event_name] = sorted(self._listeners[event_name],
                                             key=lambda event_listener: event_listener.priority.value)

    def _remove_busy_listener(self, callback: Callable) -> None:
        """
        Remove a busy listener from the set of busy listeners.

        :param callback: Callable object representing the listener function.
        """
        if callback in self._busy_listeners:
            self._busy_listeners.remove(callback)

    @classmethod
    def _log_listener_call(cls, listener: EventListener, event: Event, is_async: bool) -> None:
        """
        Log the invocation of an event listener, including whether it's synchronous or asynchronous.

        :param listener: The event listener being invoked.
        :param event: The event associated with the listener.
        :param is_async: True if the listener is asynchronous; False if synchronous.
        """
        message_front = "async calling" if is_async else "calling"

        logging.info(f"{message_front}: [{listener.callback.__name__}] from event: [{event.event_name}]")

        if is_async and listener.callback in cls._busy_listeners:
            logging.info(f"skipping call to: [{listener.callback.__name__}] as it's busy")

    async def _event_loop_runner(self):
        """
        Run the event loop to process queued events.
        """
        while self._is_event_loop_running:
            task = await self._event_queue.get()
            func, event, args, kwargs = task
            if asyncio.iscoroutinefunction(func):
                self._event_loop.create_task(func(event, *args, **kwargs))
            else:
                func(event, *args, **kwargs)
            self._event_queue.task_done()
