"""HomeWizard P1 (via HomeWizard energy meter) plugin — async HTTP polling."""
import logging
from datetime import datetime, timezone

import aiohttp

from core.plugin import Command, DevicePlugin, Reading

log = logging.getLogger("plugin.homewizard_p1")


def _normalize_host(host: str) -> str:
    if host and "://" not in host:
        return f"http://{host}"
    return host


class HomewizardP1Plugin(DevicePlugin):
    @property
    def plugin_id(self) -> str:
        return "homewizard_p1"

    async def collect(self) -> list[Reading]:
        host = _normalize_host(self.config.get("host"))
        if not host:
            raise RuntimeError("No host configured for HomeWizard")

        async with aiohttp.ClientSession() as session:
            # 10s instead of 5s: a HomeWizard P1 under heavy local network load
            # sometimes responds slower than 5s, which caused a collect cycle to be
            # counted as failed unnecessarily (see plugin_id in the agent logs on
            # repeated timeouts).
            async with session.get(f"{host}/api/v1/data", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                data = await resp.json()

        timestamp = datetime.now(timezone.utc)
        return [
            Reading(device_id=self.device_id, metric=key, value=value, unit="",
                    timestamp=timestamp, source="homewizard_p1", direction="import")
            for key, value in data.items()
            if isinstance(value, (int, float))
        ]

    async def execute(self, command: Command) -> dict:
        raise NotImplementedError("Actuation not supported for homewizard_p1")

    @staticmethod
    async def test_connection(config: dict) -> dict:
        host = _normalize_host(config.get("host"))
        if not host:
            raise ValueError("Host is required")
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{host}/api", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                resp.raise_for_status()
                info = await resp.json()
            product_type = info.get("product_type")
            if product_type != "HWE-P1":
                raise ValueError(
                    f"This address is not a HomeWizard P1 Meter, but '{info.get('product_name') or product_type}' "
                    f"({product_type}). Check that you entered the correct device."
                )
            async with session.get(f"{host}/api/v1/data", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return {
            "product_name": info.get("product_name"),
            "product_type": info.get("product_type"),
            "serial": info.get("serial"),
            "firmware_version": info.get("firmware_version"),
            "active_power_w": data.get("active_power_w"),
        }
