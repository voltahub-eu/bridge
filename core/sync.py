import asyncio
import inspect
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import aiohttp

import config
from core import database
from core.bus import Bus
from core.health import HealthTracker
from core.plugin import Reading
from core.version import get_agent_version

log = logging.getLogger("sync")

INSTALL_DIR = Path(__file__).resolve().parent.parent


class SyncClient:
    """Persistent WebSocket connection to the platform. Four channels:
    config (platform->agent), data (agent->platform), health (agent->platform),
    command (platform->agent) + ack (agent->platform). Platform being offline
    at startup/during use is not a problem — readings/health stay locally in
    SQLite until the next successful flush."""

    def __init__(self, bus: Bus, health: HealthTracker, device_id: str, on_config=None, on_command=None):
        self.bus = bus
        self.health = health
        self.device_id = device_id
        self.on_config = on_config
        self.on_command = on_command
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._started_at = time.monotonic()
        self._reading_queue: asyncio.Queue[Reading] = bus.subscribe("reading")
        # Separate flag alongside self._ws: the WS handshake (TCP+HTTP upgrade)
        # can succeed while the platform still rejects the token afterwards —
        # only the platform's "connected" confirmation means truly authenticated.
        self.authenticated = False
        self.auth_error: str | None = None

    async def run(self) -> None:
        # Without a token, every connection attempt (WS + config/health
        # flushes) is guaranteed to be pointless and only generates noise in
        # the logs (a "connection failed" warning every 10s) — calmly wait
        # until a token has been stored via the local status page.
        # api_token() restarts the service after storing it, so this wait
        # loop is mainly a safety net; it also ensures startup without a
        # token doesn't show error messages.
        if not config.AGENT_KEY:
            log.info("no agent token configured — platform connection is skipped until a token is stored via the local status page")
            while not config.AGENT_KEY:
                await asyncio.sleep(5)
            log.info("agent token found, starting platform connection")

        await asyncio.gather(
            self._connection_loop(),
            self._readings_flush_loop(),
            self._health_flush_loop(),
            self._reading_intake_loop(),
            self._config_refresh_loop(),
            self._readings_cleanup_loop(),
        )

    async def _connection_loop(self) -> None:
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    headers = {"X-Api-Key": config.AGENT_KEY}
                    async with session.ws_connect(
                        f"{config.PLATFORM_WS_URL}?token={config.AGENT_KEY}", headers=headers
                    ) as ws:
                        self._ws = ws
                        log.debug("WS handshake succeeded, waiting for authentication confirmation")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_message(json.loads(msg.data))
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
                        if not self.authenticated and not self.auth_error:
                            # Connection dropped before "connected" or "error" ever
                            # arrived — no clear reason, but worth noting.
                            self.auth_error = "Connection dropped before authentication confirmation"
            except Exception as e:
                log.warning("WS connection failed/dropped: %s — reconnecting in %ss", e, config.WS_RECONNECT_DELAY_S)
            self._ws = None
            self.authenticated = False
            await asyncio.sleep(config.WS_RECONNECT_DELAY_S)

    async def _handle_message(self, msg: dict) -> None:
        msg_type = msg.get("type")
        if msg_type == "connected":
            self.authenticated = True
            self.auth_error = None
            log.info("connected and authenticated with platform (device_id=%s)", msg.get("device_id"))
            # Send a heartbeat right away instead of waiting for the next
            # _health_flush_loop iteration: that loop often sends its first
            # attempt before this WS connection even existed (in which case
            # it's silently ignored by _send(), see there), which could leave
            # the agent_version/update_status reconciliation on the platform
            # hanging for up to HEALTH_FLUSH_INTERVAL_S (60s) after a restart
            # — noticeable as an "updating" status that takes much longer
            # than the Pi's actual restart.
            await self._send_health()
            return
        if msg_type == "error":
            self.auth_error = msg.get("detail") or "unknown error"
            self.authenticated = False
            log.error("authentication with platform failed: %s — check AGENT_KEY", self.auth_error)
            return

        channel = msg.get("channel")
        if channel == "config" and self.on_config:
            plugins = msg.get("plugins", msg.get("integrations", []))
            log.info("config received: %d plugin(s) — %s", len(plugins),
                     ", ".join(p.get("plugin_id") or p.get("integration_id", "?") for p in plugins))
            await self.on_config(msg)
        elif channel == "command" and self.on_command:
            command_id = msg.get("id") or msg.get("request_id")
            log.info("command received: id=%s type=%s plugin=%s", command_id, msg.get("type"), msg.get("plugin_id"))
            result = await self.on_command(msg)
            ack: dict = {"channel": "ack", "command_id": command_id}
            if isinstance(result, dict):
                ack["result"] = result
            else:
                ack["status"] = result or "received"
            await self._send(ack)
        elif msg.get("type") == "test_integration":
            # Legacy command type (no "channel" field, same as v1.0.26):
            # platform asks for a one-off connection test with a config that
            # hasn't been saved yet, used by the "Test connection" button in the UI.
            log.info("test_integration received for %s", msg.get("integration_id"))
            await self._handle_test_integration(msg)
        elif msg.get("type") == "config_update":
            # Legacy signal (no "channel" field, same as v1.0.26): platform
            # sends this on every change to a sub-integration (add, edit
            # config, pause), but without sending the plugin list itself —
            # the agent has to fetch that separately via GET /agent/config.
            log.info("config_update signal received, refetching config")
            await self._refetch_config()
        elif msg.get("type") == "update":
            # Legacy command type (no "channel" field, same as v1.0.26):
            # platform requests an OTA update of the agent core itself to the
            # given git tag/version (independent of plugin versions, see
            # core/plugin_download.py for that side).
            version = msg.get("version")
            log.warning("update command received: agent is being updated to %s", version)
            self._trigger_update(version)
        elif msg.get("type") == "flush_now":
            # "Fetch now" button in the web app: sends the already locally
            # buffered, not-yet-synced measurements immediately, without
            # waiting for the next READINGS_FLUSH_INTERVAL_S cycle.
            # Deliberately does not trigger a new collect() on the plugins
            # themselves — that stays on their own poll interval.
            log.info("flush_now received, sending buffered readings immediately")
            await self._flush_readings()

    def _trigger_update(self, version: str) -> None:
        """Copies the update script to /tmp and runs it as a standalone
        process — the script itself does a `git checkout` in the working tree
        this script ORIGINALLY came from; if it kept running in place from
        that working tree, bash could end up executing a mix of old/new
        script content halfway through (bash reads scripts buffered from
        disk). Running from /tmp, the script itself is no longer part of
        what git overwrites."""
        src = INSTALL_DIR / "scripts" / "update.sh"
        tmp = Path("/tmp/voltahub-bridge-update.sh")
        try:
            shutil.copy(src, tmp)
            os.chmod(tmp, 0o755)
            subprocess.Popen(["bash", str(tmp), version], env=os.environ.copy())
        except Exception as e:
            log.error("could not start update script: %s", e)

    async def _refetch_config(self) -> None:
        if not self.on_config:
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{config.PLATFORM_API_URL}/agent/config",
                    headers={"X-Api-Key": config.AGENT_KEY},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    payload = await resp.json()
        except Exception as e:
            log.warning("fetching config failed: %s", e)
            return

        plugins = payload.get("plugins", payload.get("integrations", []))
        log.info("config received (via fetch): %d plugin(s) — %s", len(plugins),
                 ", ".join(p.get("plugin_id") or p.get("integration_id", "?") for p in plugins) or "none")
        await self.on_config(payload)

    async def _readings_cleanup_loop(self) -> None:
        """readings accumulates forever otherwise (mark_synced() only flags
        rows, never deletes them) — on a Pi's limited storage that
        eventually fills the disk, which in turn breaks syncing too."""
        while True:
            await asyncio.sleep(config.READINGS_CLEANUP_INTERVAL_S)
            deleted = database.purge_synced_readings(config.READINGS_RETENTION_DAYS)
            if deleted:
                log.info("cleaned up %d synced reading(s) older than %d day(s)",
                          deleted, config.READINGS_RETENTION_DAYS)

    async def _config_refresh_loop(self) -> None:
        """Safety net alongside the event-driven config_update pushes:
        periodically fetches the full config again, in case a push signal
        never arrives for whatever reason."""
        while True:
            await asyncio.sleep(config.CONFIG_REFRESH_INTERVAL_S)
            log.info("periodic config refresh")
            await self._refetch_config()

    async def _handle_test_integration(self, msg: dict) -> None:
        from core.agent import _load_plugin_class  # lazy: avoids circular import with core.agent

        request_id = msg.get("request_id")
        integration_id = msg.get("integration_id", "")
        test_config = msg.get("config") or {}
        target_version = msg.get("target_version")
        start = time.monotonic()
        try:
            # New integration: the plugin might not be available locally yet
            # (not vendored, never downloaded). Download it specifically
            # based on the version/checksum the backend sends, instead of
            # waiting for the next config sync (see _on_config_push for the
            # same flow).
            if target_version and database.get_installed_version(integration_id) != target_version:
                from core.plugin_download import ensure_plugin_version
                if await ensure_plugin_version(integration_id, target_version, msg.get("target_sha256")):
                    database.upsert_plugin(integration_id, installed_version=target_version)
                else:
                    log.warning("plugin %s: download of version %s failed before test", integration_id, target_version)

            plugin_cls = _load_plugin_class(integration_id)
            test_fn = plugin_cls.test_connection
            if inspect.iscoroutinefunction(test_fn):
                device_info = await test_fn(test_config)
            else:
                loop = asyncio.get_running_loop()
                device_info = await loop.run_in_executor(None, test_fn, test_config)
            await self._send({
                "type": "test_result", "request_id": request_id, "success": True,
                "response_ms": int((time.monotonic() - start) * 1000), "device": device_info,
            })
        except Exception as e:
            log.warning("test_integration for %s failed: %s", integration_id, e)
            await self._send({
                "type": "test_result", "request_id": request_id, "success": False,
                "response_ms": int((time.monotonic() - start) * 1000), "error": str(e),
            })

    async def _send(self, payload: dict) -> bool:
        if not self._ws:
            return False
        try:
            await self._ws.send_json(payload)
            return True
        except Exception as e:
            log.warning("sending failed: %s", e)
            return False

    async def _reading_intake_loop(self) -> None:
        """Immediately stores readings from the bus locally in SQLite
        (durable); the flush loop then periodically sends them to the
        platform."""
        while True:
            event = await self._reading_queue.get()
            r: Reading = event.payload
            database.store_reading(r.device_id, r.metric, r.value, r.unit, r.direction, r.source, r.timestamp.isoformat())

    async def _readings_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(config.READINGS_FLUSH_INTERVAL_S)
            await self._flush_readings()

    async def _flush_readings(self) -> None:
        rows = database.unsynced_readings()
        if not rows:
            return
        # Group by (source, timestamp): measurements fetched in the same
        # collect() cycle belong together (e.g. all OBIS fields from one
        # P1 telegram) and must arrive as one item at the platform
        # normalization step, not separately per metric.
        grouped: dict[tuple[str, str], dict] = {}
        ids_by_group: dict[tuple[str, str], list[int]] = {}
        for r in rows:
            key = (r["source"], r["timestamp"])
            grouped.setdefault(key, {})[r["metric"]] = {"value": r["value"], "unit": r["unit"]}
            ids_by_group.setdefault(key, []).append(r["id"])

        readings = [
            {"integration_id": source, "timestamp": ts, "data": data}
            for (source, ts), data in grouped.items()
        ]
        if self.authenticated and await self._send({"channel": "data", "readings": readings}):
            database.mark_synced([i for ids in ids_by_group.values() for i in ids])
            log.info("readings sent: %d item(s), %d reading(s)", len(readings), len(rows))
        else:
            log.warning("readings NOT sent (not authenticated with platform) — staying buffered locally (%d reading(s))", len(rows))

    async def _send_health(self) -> None:
        plugins = self.health.snapshot()
        sent = await self._send({
            "channel": "health",
            "agent_version": get_agent_version(),
            "uptime_s": int(time.monotonic() - self._started_at),
            "plugins": plugins,
        })
        if sent:
            log.info("health sent: %d plugin(s) — %s", len(plugins),
                     ", ".join(f"{p['id']}={p['status']}" for p in plugins) or "none")
        else:
            log.warning("health NOT sent (no WS connection)")

    async def _health_flush_loop(self) -> None:
        while True:
            await self._send_health()
            await asyncio.sleep(config.HEALTH_FLUSH_INTERVAL_S)
