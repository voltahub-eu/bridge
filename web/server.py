import ipaddress
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from core import database
from core.agent import _get_logs
from core.env_file import write_agent_key, write_env
from core.version import get_agent_version

STATIC_DIR = Path(__file__).parent / "static"
_START_TIME = time.monotonic()
_pi_serial_cache: str | None = None


def _pi_serial() -> str | None:
    """Unique hardware serial number of the Pi itself (from /proc/cpuinfo) —
    unlike agent.device_id (the platform ID), this always stays available,
    even before a device is linked to the platform."""
    global _pi_serial_cache
    if _pi_serial_cache is not None:
        return _pi_serial_cache
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    _pi_serial_cache = line.split(":", 1)[1].strip()
                    return _pi_serial_cache
    except OSError:
        pass
    return None


def _local_ip() -> str | None:
    """Local LAN IP without actually sending anything — the socket is never
    connected, this just lets the OS pick the outgoing interface/route for
    8.8.8.8 so we can read the corresponding local address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def _subnet_mask(ip: str | None) -> str | None:
    """Subnet mask of the interface that has `ip`, via `ip -o -4 addr show`
    (present by default on Raspberry Pi OS)."""
    if not ip:
        return None
    try:
        result = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.splitlines():
            match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", line)
            if match and match.group(1) == ip:
                return str(ipaddress.IPv4Network(f"0.0.0.0/{match.group(2)}").netmask)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _disk_usage() -> dict:
    """Disk space of the SD card — SD cards are the weak point of a Pi
    running 24/7, so early visibility into it filling up is worth more here
    than elsewhere."""
    try:
        usage = shutil.disk_usage("/")
        return {
            "disk_total_gb": round(usage.total / 1_000_000_000, 1),
            "disk_used_gb": round(usage.used / 1_000_000_000, 1),
            "disk_percent": round(usage.used / usage.total * 100, 1),
        }
    except OSError:
        return {"disk_total_gb": None, "disk_used_gb": None, "disk_percent": None}


def _cpu_temp_c() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


def _boot_time() -> str | None:
    try:
        with open("/proc/uptime") as f:
            uptime_s = float(f.read().split()[0])
        boot_ts = datetime.now(timezone.utc).timestamp() - uptime_s
        return datetime.fromtimestamp(boot_ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError, IndexError):
        return None


class TokenRequest(BaseModel):
    token: str


class PlatformUrlRequest(BaseModel):
    password: str
    platform_ws_url: str
    platform_api_url: str


class PasswordCheckRequest(BaseModel):
    password: str


def _local_edit_password(agent) -> str | None:
    """Barrier against accidentally changing the platform endpoint: the last
    6 characters of the device id, which is already visible on this page
    anyway. Not real security — it only prevents someone from blindly
    changing the URL via the API without ever having seen the page."""
    device_id = agent.device_id or _pi_serial()
    if not device_id or len(device_id) < 6:
        return None
    return device_id[-6:].lower()


def build_app(agent) -> FastAPI:
    app = FastAPI(title="Voltahub Bridge")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def api_health():
        meta = database.plugin_metadata()
        plugins = [
            {
                **p,
                "label": meta.get(p["id"], {}).get("label"),
                "slug": meta.get(p["id"], {}).get("slug"),
                "integration_name": meta.get(p["id"], {}).get("integration_name"),
                "installed_version": meta.get(p["id"], {}).get("installed_version"),
            }
            for p in agent.health.snapshot()
        ]
        sync = agent.sync
        if sync and sync.authenticated:
            agent_status = "online"
        elif sync and sync.auth_error:
            agent_status = "auth_error"
        else:
            agent_status = "offline"
        return {
            "agent_status": agent_status,
            "auth_error": sync.auth_error if sync else None,
            "unsynced_count": database.unsynced_count(),
            "plugins": plugins,
        }

    @app.get("/api/readings")
    def api_readings():
        # Only readings not yet synced: an acceptable MVP assumption because
        # the flush interval is short (30s); a "latest per plugin" table is a
        # logical next step if this turns out to be a blind spot.
        latest_by_plugin: dict[str, dict] = {}
        for row in database.unsynced_readings(limit=1000):
            source = row["source"]
            existing = latest_by_plugin.get(source)
            if not existing or row["timestamp"] > existing["timestamp"]:
                latest_by_plugin[source] = dict(row)
        return latest_by_plugin

    @app.get("/api/readings/{source}")
    def api_readings_latest(source: str):
        rows = database.latest_readings_batch(source)
        return {
            "timestamp": rows[0]["timestamp"] if rows else None,
            "readings": [
                {"metric": r["metric"], "value": r["value"], "unit": r["unit"], "direction": r["direction"]}
                for r in rows
            ],
        }

    @app.get("/api/device")
    def api_device():
        local_ip = _local_ip()
        return {
            "device_id": agent.device_id or _pi_serial(),
            "agent_version": get_agent_version(),
            "uptime_s": int(time.monotonic() - _START_TIME),
            "network_mode": database.get_device_config("network_mode", "lan"),
            "agent_key": config.AGENT_KEY,
            "platform_ws_url": config.PLATFORM_WS_URL,
            "platform_api_url": config.PLATFORM_API_URL,
            "local_ip": local_ip,
            "subnet_mask": _subnet_mask(local_ip),
            "boot_time": _boot_time(),
            "cpu_temp_c": _cpu_temp_c(),
            **_disk_usage(),
        }

    @app.post("/api/token")
    def api_token(body: TokenRequest):
        """Update the agent token (AGENT_KEY) — for when you generated the
        token on the platform the old-fashioned way and want to link it
        here, or after a token rotation. Restarts the agent service so the
        new value is used immediately."""
        token = body.token.strip()
        if not token.startswith("vh_br_"):
            raise HTTPException(status_code=422, detail="Token must start with 'vh_br_'")
        write_agent_key(token)
        subprocess.Popen(["bash", "-c", "sleep 1 && systemctl restart voltahub-bridge"])
        return {"ok": True, "message": "Token saved, restarting agent..."}

    @app.post("/api/platform-url/verify-password")
    def api_platform_url_verify_password(body: PasswordCheckRequest):
        """Separate verification step so the UI can validate the password
        immediately before showing the URL fields, instead of only at
        save time."""
        expected = _local_edit_password(agent)
        if not expected or body.password.strip().lower() != expected:
            raise HTTPException(status_code=403, detail="Incorrect password")
        return {"ok": True}

    @app.post("/api/platform-url")
    def api_platform_url(body: PlatformUrlRequest):
        """Change the platform endpoint (WS/API) this bridge connects to —
        e.g. to point a test bridge at a test environment. Only useful
        before the bridge is linked/online, so this goes locally via the
        env file instead of via the platform itself."""
        expected = _local_edit_password(agent)
        if not expected or body.password.strip().lower() != expected:
            raise HTTPException(status_code=403, detail="Incorrect password")
        write_env({
            "PLATFORM_WS_URL": body.platform_ws_url.strip(),
            "PLATFORM_API_URL": body.platform_api_url.strip(),
        })
        subprocess.Popen(["bash", "-c", "sleep 1 && systemctl restart voltahub-bridge"])
        return {"ok": True, "message": "Platform endpoint saved, restarting agent..."}

    @app.post("/api/restart")
    def api_restart():
        subprocess.Popen(["bash", "-c", "sleep 1 && systemctl restart voltahub-bridge"])
        return {"ok": True, "message": "Restarting agent..."}

    @app.get("/api/logs")
    async def api_logs(lines: int = 200):
        result = await _get_logs({"lines": lines})
        if not result.get("success"):
            raise HTTPException(status_code=500, detail=result.get("error") or "Failed to fetch logs")
        return result

    return app
