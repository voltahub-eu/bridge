import asyncio
import importlib.util
import ipaddress
import json
import logging
import socket
import sys
import time
import urllib.parse
from pathlib import Path

import aiohttp

import config
from core import database
from core.bus import Bus
from core.health import HealthTracker
from core.plugin import DevicePlugin
from core.supervisor import Supervisor
from core.sync import SyncClient

log = logging.getLogger("agent")


MAX_PROBE_BODY_CHARS = 200_000
MAX_LOG_CHARS = 300_000


async def _get_logs(payload: dict) -> dict:
    """Fetches the latest lines from the local systemd journal — on demand
    from the admin portal, only after a problem has already been noticed there
    (e.g. a device going offline or a failed update). Deliberately no active
    log monitoring/streaming, just a snapshot on request."""
    lines = max(1, min(int(payload.get("lines") or 200), 2000))
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", "voltahub-bridge", "-n", str(lines), "--no-pager", "-o", "short-iso",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return {"success": False, "error": stderr.decode(errors="replace")[:2000] or "journalctl returned an error code"}
        text_out = stdout.decode(errors="replace")
        return {
            "success": True,
            "log": text_out[-MAX_LOG_CHARS:],
            "truncated": len(text_out) > MAX_LOG_CHARS,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _check_local_only(hostname: str, port: int) -> tuple[bool, str | None]:
    """Resolves hostname to IP(s) and rejects public addresses — shared
    SSRF guard for both _run_probe and _run_ping."""
    try:
        loop = asyncio.get_running_loop()
        addr_info = await loop.getaddrinfo(hostname, port)
        for family, _, _, _, sockaddr in addr_info:
            ip = ipaddress.ip_address(sockaddr[0])
            if not (ip.is_private or ip.is_loopback or ip.is_link_local):
                return False, (f"Only the local network is allowed, {ip} is public "
                                f"(check 'allow public internet' to override this)")
    except socket.gaierror as e:
        return False, f"Could not resolve hostname: {e}"
    return True, None


async def _run_probe(payload: dict) -> dict:
    """Performs a generic HTTP call from the Pi itself, for an admin who wants
    to try out from the portal what an as-yet-unknown device returns (e.g.
    when building a new integration). Restricted by default to the local
    network (RFC1918/loopback/link-local) — this prevents the probe function
    from being abused as a generic open proxy from a customer's Pi to the
    public internet. That check is only skipped if the admin explicitly sends
    payload["allow_public"] (e.g. for a cloud login step like Enphase
    Enlighten)."""
    method = (payload.get("method") or "GET").upper()
    url = payload.get("url") or ""
    headers = payload.get("headers") or {}
    body = payload.get("body")
    timeout_s = min(float(payload.get("timeout_s") or 10), 30)
    allow_public = bool(payload.get("allow_public"))

    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname
    if not hostname or method not in ("GET", "POST"):
        return {"success": False, "error": "Invalid or missing URL/method"}

    if not allow_public:
        ok, err = await _check_local_only(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        if not ok:
            return {"success": False, "error": err}

    start = time.monotonic()
    try:
        # ssl=False: local devices (e.g. Enphase Envoy) often use a
        # self-signed certificate. This stays safe enough because the SSRF
        # guard above already guarantees this only goes to the local network.
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, url, headers=headers, data=body,
                timeout=aiohttp.ClientTimeout(total=timeout_s), ssl=False,
            ) as resp:
                text_body = await resp.text()
                return {
                    "success": True,
                    "status_code": resp.status,
                    "headers": dict(resp.headers),
                    "body": text_body[:MAX_PROBE_BODY_CHARS],
                    "truncated": len(text_body) > MAX_PROBE_BODY_CHARS,
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                }
    except Exception as e:
        return {"success": False, "error": str(e), "elapsed_ms": int((time.monotonic() - start) * 1000)}


async def _run_ping_icmp(host: str, timeout_s: float) -> dict:
    """Real ICMP ping via the system `ping` command (subprocess) — unlike a
    TCP connect test, this doesn't fail just because a device has no service
    on the tested port (e.g. an Enphase Envoy that doesn't listen on port 80
    but is otherwise perfectly reachable). Works without the agent itself
    needing to run as root: the `ping` binary has the cap_net_raw capability
    by default on Raspbian/Debian."""
    wait_s = max(1, int(round(timeout_s)))
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(wait_s), host,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=wait_s + 2)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if proc.returncode == 0:
            return {"success": True, "elapsed_ms": elapsed_ms}
        detail = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
        return {"success": False, "error": detail[:500] or "No response to ICMP ping", "elapsed_ms": elapsed_ms}
    except Exception as e:
        return {"success": False, "error": str(e), "elapsed_ms": int((time.monotonic() - start) * 1000)}


async def _run_ping_tcp(host: str, port: int, timeout_s: float) -> dict:
    """TCP connect test to host:port — checks whether something specifically
    listens on a chosen port, rather than whether the IP responds in general
    (see _run_ping_icmp for that)."""
    start = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {"success": True, "elapsed_ms": int((time.monotonic() - start) * 1000)}
    except Exception as e:
        return {"success": False, "error": str(e), "elapsed_ms": int((time.monotonic() - start) * 1000)}


async def _run_ping(payload: dict) -> dict:
    """Checks whether a device is online, without making an HTTP call.
    mode="icmp" (default): real ping. mode="tcp": connect test on a
    specific port. Same local-network restriction as _run_probe."""
    host = (payload.get("host") or "").strip()
    mode = (payload.get("mode") or "icmp").lower()
    port = payload.get("port")
    timeout_s = min(float(payload.get("timeout_s") or 5), 30)
    allow_public = bool(payload.get("allow_public"))

    if not host:
        return {"success": False, "error": "Missing host"}
    if mode == "tcp" and not port:
        return {"success": False, "error": "Port is missing for a TCP port test"}

    if not allow_public:
        ok, err = await _check_local_only(host, int(port) if port else 0)
        if not ok:
            return {"success": False, "error": err}

    if mode == "tcp":
        return await _run_ping_tcp(host, int(port), timeout_s)
    return await _run_ping_icmp(host, timeout_s)


# Vendored plugins shipped with the agent (in the repo itself, under
# bridge/plugins/) — the baseline version a Pi starts with without a GitHub
# dependency. PLUGIN_DIR (default /data/plugins) takes precedence as soon as
# core/plugin_download.py has placed a (newer) version there.
VENDORED_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _plugin_dir_for(plugin_id: str) -> Path:
    downloaded = Path(config.PLUGIN_DIR) / plugin_id
    return downloaded if (downloaded / "plugin.py").exists() else VENDORED_PLUGIN_DIR / plugin_id


def _load_plugin_class(plugin_id: str) -> type[DevicePlugin]:
    """Dynamically loads plugins/{plugin_id}/plugin.py via importlib and looks
    for the first DevicePlugin subclass in it — no registration/decorator
    needed."""
    module_path = _plugin_dir_for(plugin_id) / "plugin.py"
    spec = importlib.util.spec_from_file_location(f"plugins.{plugin_id}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    for attr in vars(module).values():
        if isinstance(attr, type) and issubclass(attr, DevicePlugin) and attr is not DevicePlugin:
            return attr
    raise RuntimeError(f"No DevicePlugin subclass found in {module_path}")


def _read_manifest_version(plugin_id: str) -> str | None:
    manifest_path = _plugin_dir_for(plugin_id) / "manifest.json"
    try:
        return json.loads(manifest_path.read_text()).get("version")
    except Exception:
        return None


class Agent:
    def __init__(self):
        self.bus = Bus()
        self.health = HealthTracker()
        self.supervisor = Supervisor(self.bus, self.health)
        self.device_id = None
        self.sync: SyncClient | None = None

    async def bootstrap(self) -> None:
        database.init_db()
        self.device_id = database.get_device_config("device_id")

        installed = database.load_installed_plugins()
        for row in installed:
            await self._start_plugin_from_row(row)

        self.sync = SyncClient(
            self.bus, self.health, self.device_id,
            on_config=self._on_config_push, on_command=self._on_command,
        )
        asyncio.create_task(self.sync.run(), name="sync")

    async def _start_plugin_from_row(self, row) -> None:
        plugin_id = row["plugin_id"]
        try:
            plugin_config = json.loads(row["config"] or "{}")
            plugin_cls = _load_plugin_class(plugin_id)
            plugin = plugin_cls(self.device_id, plugin_config)
            collect_interval = plugin_config.get("collect_interval_s", 60)
            self.supervisor.start_plugin(plugin, collect_interval)
            log.info("plugin %s started (interval %ss)", plugin_id, collect_interval)
            if not database.get_installed_version(plugin_id):
                # No version registered yet (e.g. the vendored version, never
                # downloaded via GitHub) — read it from manifest.json so the
                # status page doesn't stay empty.
                vendored_version = _read_manifest_version(plugin_id)
                if vendored_version:
                    database.upsert_plugin(plugin_id, installed_version=vendored_version)
        except FileNotFoundError:
            # Common, expected case (not vendored in this agent version and
            # never downloaded via OTA) — a full traceback adds nothing here
            # and only makes the logs unnecessarily hard to read.
            log.warning("could not start plugin %s: plugin.py is missing locally "
                        "(not yet vendored/downloaded)", plugin_id)
            database.upsert_plugin(plugin_id, status="failed")
        except Exception:
            log.exception("could not start plugin %s", plugin_id)
            database.upsert_plugin(plugin_id, status="failed")

    async def _on_config_push(self, msg: dict) -> None:
        """The platform pushes which plugins the agent should have (config
        channel, type=plugin_sync). Immediately starts/restarts the relevant
        plugin on the supervisor instead of only updating the config in
        SQLite — otherwise a newly created sub-integration only takes effect
        after a manual agent restart. If target_version differs from what's
        installed locally, the plugin is first downloaded from GitHub
        (independent of the agent core, only this one plugin) before it
        (re)starts."""
        plugins = msg.get("plugins", msg.get("integrations", []))
        seen_plugin_ids: set[str] = set()

        for plugin in plugins:
            plugin_id = plugin.get("plugin_id") or plugin.get("integration_id")
            if not plugin_id:
                continue
            seen_plugin_ids.add(plugin_id)

            plugin_config = dict(plugin.get("config") or {})
            if plugin.get("poll_interval"):
                plugin_config["collect_interval_s"] = plugin["poll_interval"]

            # Internal, locally cached state (e.g. an Enphase cloud token) does
            # not come from the platform and must not be lost on every
            # config update (pause/edit/etc.) — explicitly preserved instead
            # of fully overwriting the config with what the platform sends.
            for key, value in database.get_plugin_config(plugin_id).items():
                if key.startswith("_"):
                    plugin_config.setdefault(key, value)

            enabled = plugin.get("enabled", True)
            target_version = plugin.get("target_version")

            if target_version and enabled and database.get_installed_version(plugin_id) != target_version:
                from core.plugin_download import ensure_plugin_version
                ok = await ensure_plugin_version(plugin_id, target_version, plugin.get("target_sha256"))
                if ok:
                    database.upsert_plugin(plugin_id, installed_version=target_version)
                else:
                    log.warning("plugin %s: download of version %s failed, staying on current version",
                                plugin_id, target_version)
                # Report the result back to the platform — without this the
                # platform never knows whether a plugin download succeeded
                # (unlike core updates, which are reported via /update-result
                # + heartbeat).
                customer_integration_id = plugin.get("id")
                if customer_integration_id and self.sync:
                    await self.sync._send({
                        "channel": "plugin_update_result",
                        "customer_integration_id": customer_integration_id,
                        "plugin_id": plugin_id,
                        "version": target_version,
                        "success": ok,
                        "error": None if ok else "download/checksum failed",
                    })

            database.upsert_plugin(
                plugin_id,
                target_version=target_version,
                config_json=json.dumps(plugin_config),
                status="installed" if enabled else "paused",
                label=plugin.get("name"),
                slug=plugin.get("slug"),
                integration_name=plugin.get("integration_name"),
            )

            # Always stop first (no-op if it wasn't running yet) so that a
            # config change on an already-running plugin actually takes effect.
            was_running = self.supervisor.is_running(plugin_id)
            await self.supervisor.stop_plugin(plugin_id)
            if enabled:
                log.info("plugin %s %s via config push", plugin_id, "restarted" if was_running else "newly started")
                await self._start_plugin_from_row({"plugin_id": plugin_id, "config": json.dumps(plugin_config)})
            else:
                # Mark as paused instead of fully clearing the health status —
                # stays visible on the local status page (orange), making it
                # clear the plugin is deliberately idle and hasn't
                # accidentally disappeared.
                self.health.mark_paused(plugin_id)
                if was_running:
                    log.info("plugin %s stopped via config push (disabled)", plugin_id)

        # Plugins no longer in the list (instance removed on the platform)
        # must also stop — otherwise a removed sub-integration silently keeps
        # polling.
        for plugin_id in list(self.supervisor.running_plugin_ids()):
            if plugin_id not in seen_plugin_ids:
                await self.supervisor.stop_plugin(plugin_id)
                self.health.clear(plugin_id)
                # Without this the row stays at status='installed', and
                # bootstrap() simply loads it again on the next full agent
                # restart (WHERE status = 'installed') — before the next
                # config push stops it again. Result: a removed instance
                # briefly starts up again after every restart.
                database.upsert_plugin(plugin_id, status="removed")
                log.info("plugin %s stopped (no longer in config — instance removed)", plugin_id)

    async def _on_command(self, msg: dict) -> dict | str:
        plugin_id = msg.get("plugin_id", "")
        action = msg.get("type", "")
        command_id = msg.get("id") or msg.get("request_id") or ""
        database.log_command(command_id, plugin_id, action, json.dumps(msg.get("payload", {})), "received")

        if action == "probe":
            result = await _run_probe(msg.get("payload") or {})
            database.log_command(command_id, plugin_id, action, json.dumps(msg.get("payload", {})),
                                  "executed" if result.get("success") else "failed")
            return result

        if action == "ping":
            result = await _run_ping(msg.get("payload") or {})
            database.log_command(command_id, plugin_id, action, json.dumps(msg.get("payload", {})),
                                  "executed" if result.get("success") else "failed")
            return result

        if action == "get_logs":
            result = await _get_logs(msg.get("payload") or {})
            database.log_command(command_id, plugin_id, action, json.dumps(msg.get("payload", {})),
                                  "executed" if result.get("success") else "failed")
            return result

        return "not_supported"


async def run() -> None:
    logging.basicConfig(level=config.LOG_LEVEL)
    agent = Agent()
    await agent.bootstrap()
    await asyncio.Event().wait()
