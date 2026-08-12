import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    topic: str          # "reading" | "health" | "command"
    payload: Any


class Bus:
    """Internal pub/sub bus between plugins/supervisor and the sync loop.
    One shared asyncio.Queue per topic subscriber — deliberately kept simple
    (no external dependency) because everything runs within a single
    process/event loop."""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(topic, []).append(q)
        return q

    async def publish(self, topic: str, payload: Any) -> None:
        for q in self._subscribers.get(topic, []):
            await q.put(Event(topic=topic, payload=payload))
