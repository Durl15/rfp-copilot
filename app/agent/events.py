"""Run events: every event gets a seq and timestamp, goes to an optional sink, and is kept for storage and replay."""
import uuid
from datetime import datetime, timezone
from typing import Callable


def noop(event: str, **data) -> None:
    """Default emit callback: the plain HTTP endpoints emit nothing."""


class Emitter:
    def __init__(self, sink: Callable[[dict], None] | None = None):
        self.run_id = uuid.uuid4().hex
        self.events: list[dict] = []
        self._sink = sink
        self._seq = 0

    def __call__(self, event: str, **data) -> None:
        self._seq += 1
        ev = {"seq": self._seq, "timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        self.events.append(ev)
        if self._sink:
            self._sink(ev)
