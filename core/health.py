from datetime import datetime, timezone

from core import database
from core.plugin import PluginHealth


class HealthTracker:
    """Keeps track of the in-memory + SQLite health status per plugin."""

    def __init__(self):
        self._health: dict[str, PluginHealth] = {}

    def mark_ok(self, plugin_id: str) -> None:
        now = datetime.now(timezone.utc)
        prev = self._health.get(plugin_id)
        restart_count = prev.restart_count if prev else 0
        self._set(plugin_id, "ok", last_reading_at=now, last_error=None, restart_count=restart_count)

    def mark_error(self, plugin_id: str, error: str, restart_count: int) -> None:
        prev = self._health.get(plugin_id)
        last_reading_at = prev.last_reading_at if prev else None
        self._set(plugin_id, "error", last_reading_at=last_reading_at, last_error=error, restart_count=restart_count)

    def mark_timeout(self, plugin_id: str, restart_count: int) -> None:
        prev = self._health.get(plugin_id)
        last_reading_at = prev.last_reading_at if prev else None
        self._set(plugin_id, "timeout", last_reading_at=last_reading_at, last_error="collect() timeout", restart_count=restart_count)

    def mark_degraded(self, plugin_id: str, error: str, restart_count: int) -> None:
        prev = self._health.get(plugin_id)
        last_reading_at = prev.last_reading_at if prev else None
        self._set(plugin_id, "degraded", last_reading_at=last_reading_at, last_error=error, restart_count=restart_count)

    def mark_paused(self, plugin_id: str) -> None:
        """Plugin is deliberately disabled (paused via the platform) — stays
        visible on the local status page (unlike clear(), for an instance
        that's fully removed), but is clearly marked as inactive."""
        prev = self._health.get(plugin_id)
        last_reading_at = prev.last_reading_at if prev else None
        self._set(plugin_id, "paused", last_reading_at=last_reading_at, last_error=None,
                   restart_count=prev.restart_count if prev else 0)

    def _set(self, plugin_id: str, status: str, last_reading_at, last_error, restart_count: int) -> None:
        updated_at = datetime.now(timezone.utc)
        self._health[plugin_id] = PluginHealth(
            plugin_id=plugin_id, status=status, last_reading_at=last_reading_at,
            last_error=last_error, restart_count=restart_count, updated_at=updated_at,
        )
        database.upsert_plugin_health(
            plugin_id, status,
            last_reading_at.isoformat() if last_reading_at else None,
            last_error, restart_count,
        )

    def clear(self, plugin_id: str) -> None:
        """Removes the health status of a plugin that was deliberately
        stopped (disabled/removed via config push) — otherwise the last
        known status (e.g. green 'ok') stays visible forever on the local
        status page, even though the plugin hasn't been running for a while."""
        self._health.pop(plugin_id, None)
        database.delete_plugin_health(plugin_id)

    def snapshot(self) -> list[dict]:
        return [
            {
                "id": h.plugin_id,
                "status": h.status,
                "last_reading_at": h.last_reading_at.isoformat() if h.last_reading_at else None,
                "last_error": h.last_error,
                "restart_count": h.restart_count,
            }
            for h in self._health.values()
        ]
