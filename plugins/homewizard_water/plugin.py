"""HomeWizard Water Meter plugin — async HTTP polling of the local REST API."""
import logging
from datetime import datetime, timezone

import aiohttp

from core.plugin import Command, DevicePlugin, Reading

log = logging.getLogger("plugin.homewizard_water")


def _normalize_host(host: str) -> str:
    if host and "://" not in host:
        return f"http://{host}"
    return host


class HomewizardWaterPlugin(DevicePlugin):
    @property
    def plugin_id(self) -> str:
        return "homewizard_water"

    async def collect(self) -> list[Reading]:
        host = _normalize_host(self.config.get("host"))
        if not host:
            raise RuntimeError("No host configured for HomeWizard Water Meter")

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{host}/api/v1/data", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                resp.raise_for_status()
                data = await resp.json()

        timestamp = datetime.now(timezone.utc)
        return [
            Reading(device_id=self.device_id, metric=key, value=value, unit="",
                    timestamp=timestamp, source="homewizard_water", direction="consumption")
            for key, value in data.items()
            if isinstance(value, (int, float))
        ]

    async def execute(self, command: Command) -> dict:
        raise NotImplementedError("Actuation not supported for homewizard_water")

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
            if product_type != "HWE-WTR":
                raise ValueError(
                    f"This address is not a HomeWizard Water Meter, but '{info.get('product_name') or product_type}' "
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
            "total_liter_m3": data.get("total_liter_m3"),
        }
