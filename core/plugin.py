import asyncio
import functools
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass
class Reading:
    device_id: str
    metric: str        # "power_w" | "energy_kwh" | "soc_pct" | "flow_lpm"
    value: float
    unit: str
    timestamp: datetime
    source: str         # "homewizard" | "modbus" | "p1" | "mqtt"
    direction: str       # "import" | "export" | "production" | "consumption"


@dataclass
class Command:
    id: str
    plugin_id: str
    action: str
    params: dict


@dataclass
class PluginHealth:
    plugin_id: str
    status: Literal["ok", "degraded", "error", "timeout", "paused"]
    last_reading_at: datetime | None
    last_error: str | None
    restart_count: int
    updated_at: datetime


class DevicePlugin(ABC):
    """Base interface for all plugins. `collect()` is always called by the
    supervisor via `asyncio.wait_for(..., timeout=...)` — so plugins must
    never block indefinitely. Blocking I/O (serial, sync HTTP) should run via
    `run_blocking()`, never directly in `collect()`."""

    def __init__(self, device_id: str, config: dict):
        self.device_id = device_id
        self.config = config

    @property
    @abstractmethod
    def plugin_id(self) -> str: ...

    @property
    def capabilities(self) -> list[str]:
        return ["read"]

    @abstractmethod
    async def collect(self) -> list[Reading]: ...

    async def execute(self, command: Command) -> dict:
        raise NotImplementedError("Actuation not yet supported")

    async def on_start(self): ...
    async def on_stop(self): ...

    async def run_blocking(self, fn, *args, **kwargs):
        """Runs a blocking (synchronous) call in the default executor, so
        slow/blocking I/O (serial port, sync requests) never freezes the
        event loop. Required for plugins with non-async I/O."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))
