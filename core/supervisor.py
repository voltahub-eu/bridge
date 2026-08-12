import asyncio
import logging
from datetime import datetime, timezone

import config
from core.bus import Bus
from core.health import HealthTracker
from core.plugin import DevicePlugin

log = logging.getLogger("supervisor")


class Supervisor:
    """Runs each plugin in its own asyncio.Task, catches all exceptions
    (never propagates to core) and restarts with backoff. The supervisor
    itself runs in a separate task with its own exception handler — must
    never stop."""

    def __init__(self, bus: Bus, health: HealthTracker):
        self.bus = bus
        self.health = health
        self._tasks: dict[str, asyncio.Task] = {}
        self._restart_counts: dict[str, int] = {}
        self._stopped: set[str] = set()

    def start_plugin(self, plugin: DevicePlugin, collect_interval_s: int) -> None:
        plugin_id = plugin.plugin_id
        self._stopped.discard(plugin_id)  # otherwise a restart immediately stops again (see _run_plugin)
        self._restart_counts.setdefault(plugin_id, 0)
        self._tasks[plugin_id] = asyncio.create_task(
            self._run_plugin(plugin, collect_interval_s), name=f"plugin:{plugin_id}"
        )

    def is_running(self, plugin_id: str) -> bool:
        return plugin_id in self._tasks

    def running_plugin_ids(self) -> list[str]:
        return list(self._tasks.keys())

    async def stop_plugin(self, plugin_id: str) -> None:
        self._stopped.add(plugin_id)
        task = self._tasks.pop(plugin_id, None)
        if task:
            task.cancel()

    async def _run_plugin(self, plugin: DevicePlugin, collect_interval_s: int) -> None:
        plugin_id = plugin.plugin_id
        await plugin.on_start()
        try:
            while True:
                if plugin_id in self._stopped:
                    return
                try:
                    readings = await asyncio.wait_for(
                        plugin.collect(), timeout=collect_interval_s * 3
                    )
                    self.health.mark_ok(plugin_id)
                    self._restart_counts[plugin_id] = 0
                    log.debug("plugin %s: %d reading(s) collected", plugin_id, len(readings))
                    for reading in readings:
                        await self.bus.publish("reading", reading)
                    await asyncio.sleep(collect_interval_s)
                except asyncio.TimeoutError:
                    log.warning("plugin %s: timeout after %ss", plugin_id, collect_interval_s * 3)
                    if not await self._handle_failure(plugin, plugin_id, timeout=True):
                        return
                except Exception as e:
                    log.exception("plugin %s: unexpected error in collect()", plugin_id)
                    if not await self._handle_failure(plugin, plugin_id, error=str(e)):
                        return
        finally:
            try:
                await plugin.on_stop()
            except Exception:
                log.exception("plugin %s: error during on_stop()", plugin_id)

    async def _handle_failure(self, plugin: DevicePlugin, plugin_id: str,
                               timeout: bool = False, error: str | None = None) -> bool:
        """Returns False if the plugin should be stopped permanently
        (degraded, after MAX_RESTART_ATTEMPTS failed attempts)."""
        count = self._restart_counts.get(plugin_id, 0) + 1
        self._restart_counts[plugin_id] = count

        if timeout:
            self.health.mark_timeout(plugin_id, count)
        else:
            self.health.mark_error(plugin_id, error or "unknown error", count)

        if count > config.MAX_RESTART_ATTEMPTS:
            self.health.mark_degraded(plugin_id, error or "timeout", count)
            log.error("plugin %s: %d failed restart attempts — marked as degraded, stopping further restarts",
                       plugin_id, count)
            await self.bus.publish("health_alert", {"plugin_id": plugin_id, "status": "degraded"})
            return False

        backoff = config.RESTART_BACKOFF_S[min(count - 1, len(config.RESTART_BACKOFF_S) - 1)]
        log.info("plugin %s: restarting in %ss (attempt %d)", plugin_id, backoff, count)
        await asyncio.sleep(backoff)
        return True
