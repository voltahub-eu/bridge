"""
Enphase plugin — cloud JWT login (Enlighten) + local Envoy polling.

Deliberately stays on the synchronous `requests` library (instead of aiohttp)
because the original logic has several sequential calls with the same
tolerance for partial failures (see agent/integrations/enphase.py) — porting
to aiohttp would give more risk here than benefit. All calls therefore run
via run_blocking() so the event loop doesn't block.

Note: this plugin sends flat `inverter.{serial}.*` metrics instead of the
nested `inverters` list the old REST `/agent/readings` call used. The
platform normalization in app/routers/agent.py accepts both forms.
"""
import base64
import json
import logging
import time
from datetime import datetime, timezone

from core.plugin import Command, DevicePlugin, Reading

log = logging.getLogger("plugin.enphase")

ENLIGHTEN_LOGIN_URL = "https://enlighten.enphaseenergy.com/login/login.json"
ENLIGHTEN_TOKEN_URL = "https://entrez.enphaseenergy.com/tokens"
TOKEN_REFRESH_MARGIN = 3600


def _normalize_host(host: str) -> str:
    if host and "://" not in host:
        return f"https://{host}"
    return host


def _decode_token_exp(token: str) -> float:
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return float(payload.get("exp", 0))
    except Exception:
        return 0


def _get_token(cfg: dict) -> str:
    import requests

    # JSON, not form-urlencoded: Enphase's login endpoint now only accepts
    # JSON bodies (verified via the Edge Probe tool — a form-urlencoded POST
    # with exactly the same credentials always gave the generic "Please
    # provide a username and password" error, regardless of content).
    login_resp = requests.post(
        ENLIGHTEN_LOGIN_URL,
        json={"user": {"email": cfg.get("username"), "password": cfg.get("password")}},
        headers={"Accept": "application/json"},
        timeout=10,
    )
    login_resp.raise_for_status()
    session_id = login_resp.json().get("session_id")
    if not session_id:
        raise RuntimeError("Enlighten login failed: no session_id received")

    token_resp = requests.post(
        ENLIGHTEN_TOKEN_URL,
        json={"session_id": session_id, "serial_num": cfg.get("serial"), "username": cfg.get("username")},
        timeout=10,
    )
    token_resp.raise_for_status()
    token = token_resp.text.strip()
    if not token:
        raise RuntimeError("Fetching Enlighten token failed: empty response")
    return token


def _get_json(url: str, token: str):
    import requests
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10, verify=False)
    resp.raise_for_status()
    return resp.json()


class EnphasePlugin(DevicePlugin):
    def __init__(self, device_id: str, config: dict):
        super().__init__(device_id, config)
        # Load a previously cached token from local SQLite (via
        # _on_config_push, which preserves internal "_" keys) — the token is
        # valid for ~1 year, so after a restart/reconfigure there's no need
        # to immediately log in to Enlighten again.
        self._token = config.get("_token")
        self._token_exp = config.get("_token_exp", 0)

    @property
    def plugin_id(self) -> str:
        return "enphase"

    def _collect_blocking(self) -> dict:
        import requests

        host = _normalize_host(self.config.get("host"))
        if not host:
            raise RuntimeError("No host configured for Enphase")

        if not self._token or self._token_exp - time.time() < TOKEN_REFRESH_MARGIN:
            self._token = _get_token(self.config)
            self._token_exp = _decode_token_exp(self._token)
            from core import database
            database.merge_plugin_config(self.plugin_id, {"_token": self._token, "_token_exp": self._token_exp})
            log.info("Enphase JWT refreshed, valid until %s",
                      datetime.fromtimestamp(self._token_exp, tz=timezone.utc).isoformat())

        data = _get_json(f"{host}/api/v1/production", self._token)
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected response from Envoy: {data}")

        try:
            production = _get_json(f"{host}/production.json", self._token)
            inv = next((p for p in production.get("production", []) if p.get("type") == "inverters"), None)
            data["inverters_active"] = inv.get("activeCount") if inv else None
        except (requests.RequestException, RuntimeError) as e:
            log.warning("fetching Enphase active-inverter count failed: %s", e)

        energy_by_serial = {}
        try:
            raw = _get_json(f"{host}/ivp/pdm/device_data", self._token)
            for key, device in raw.items():
                if key in ("deviceCount", "deviceDataLimit") or not isinstance(device, dict):
                    continue
                if device.get("devName") != "pcu" or not device.get("active", True):
                    continue
                serial = device.get("sn")
                channels = device.get("channels")
                if not serial or not channels:
                    continue
                channel = channels[0]
                joules = (channel.get("lifetime") or {}).get("joulesProduced")
                today_wh = (channel.get("wattHours") or {}).get("today")
                energy_by_serial[serial] = {
                    "lifetime_kwh": round(joules / 3600 / 1000, 3) if joules is not None else None,
                    "today_kwh": round(today_wh / 1000, 3) if today_wh is not None else None,
                }
        except (requests.RequestException, RuntimeError) as e:
            log.warning("fetching Enphase inverter lifetime/today energy failed: %s", e)

        try:
            inverters = _get_json(f"{host}/api/v1/production/inverters", self._token)
            data["inverters"] = [
                {"serial": inv.get("serialNumber"), "watts": inv.get("lastReportWatts"),
                 **energy_by_serial.get(inv.get("serialNumber"), {})}
                for inv in inverters
                if inv.get("serialNumber") and inv.get("lastReportWatts") is not None
            ]
        except (requests.RequestException, RuntimeError) as e:
            log.warning("fetching Enphase inverter list failed: %s", e)

        return data

    async def collect(self) -> list[Reading]:
        import requests
        try:
            data = await self.run_blocking(self._collect_blocking)
        except (requests.RequestException, RuntimeError):
            # The Envoy may have rejected the token while it still looked
            # valid locally; force a fresh token on the next poll, and also
            # clear the local cache so a restart doesn't reload a dead token.
            self._token = None
            self._token_exp = 0
            from core import database
            database.merge_plugin_config(self.plugin_id, {"_token": None, "_token_exp": 0})
            raise

        timestamp = datetime.now(timezone.utc)
        readings = [
            Reading(device_id=self.device_id, metric=key, value=value, unit="",
                    timestamp=timestamp, source="enphase", direction="production")
            for key, value in data.items()
            if isinstance(value, (int, float))
        ]
        for inv in data.get("inverters", []):
            serial = inv.get("serial")
            if not serial:
                continue
            for suffix in ("watts", "lifetime_kwh", "today_kwh"):
                value = inv.get(suffix)
                if value is not None:
                    readings.append(Reading(
                        device_id=self.device_id, metric=f"inverter.{serial}.{suffix}", value=value,
                        unit="", timestamp=timestamp, source="enphase", direction="production",
                    ))
        return readings

    async def execute(self, command: Command) -> dict:
        raise NotImplementedError("Actuation not supported for enphase")

    @staticmethod
    def test_connection(config: dict) -> dict:
        host = _normalize_host(config.get("host"))
        if not all([host, config.get("username"), config.get("password"), config.get("serial")]):
            raise ValueError("Host, username, password and serial number are required")
        token = _get_token(config)
        data = _get_json(f"{host}/api/v1/production", token)
        return {
            "Total production": f"{data.get('wattHoursLifetime', 0) / 1000:.1f} kWh",
            "Current power": f"{data.get('wattsNow', 0)} W",
        }
