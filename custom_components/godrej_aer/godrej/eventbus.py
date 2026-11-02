import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

Listener = Callable[[Any], Coroutine[Any, Any, None]]

class EventBus:
  def __init__(self) -> None:
    self.listeners: dict[str, set[Listener]] = {}

  def add_listener(self, event_name: str, listener: Listener) -> None:
    if not self.listeners.get(event_name, None):
      self.listeners[event_name] = {listener}
    else:
      self.listeners[event_name].add(listener)

  def remove_listener(self, event_name: str, listener: Listener) -> None:
    listeners = self.listeners.get(event_name)
    if not listeners:
      return

    listeners.discard(listener)
    if len(listeners) == 0:
      del self.listeners[event_name]

  def send(self, event_name: str, event_data: Any = None) -> None:
    listeners = self.listeners.get(event_name, set())
    for listener in listeners:
      asyncio.create_task(listener(event_data))