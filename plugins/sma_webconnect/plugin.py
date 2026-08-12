"""SMA WebConnect plugin — async `pysma` library, same structure as
`plugins/solaredge/plugin.py` but without `run_blocking()`: `pysma` is
already async itself, so the calls are awaited directly in `collect()`.

`_last_known_good` is an instance attribute (not a SQLite cache) — same
convention as solaredge: "last known good value" only needs to survive the
process's lifetime, not a restart."""
import logging
from datetime import datetime, timezone

from core.plugin import Command, DevicePlugin, Reading

log = logging.getLogger("plugin.sma_webconnect")

# pysma returns its own set of discovered sensors per inverter (not every
# model has battery/meter sensors). Names follow pysma's sensor names (same
# as the Home Assistant SMA integration uses); missing sensors are simply
# skipped by `_normalize`'s `add()` helper.
SENSOR_TO_METRIC = {
    "pv_power": ("current_power_w", "W", "production"),
    "pv_power_a": ("pv_power_a_w", "W", "production"),
    "pv_power_b": ("pv_power_b_w", "W", "production"),
    "pv_voltage_a": ("pv_voltage_a_v", "V", "production"),
    "pv_voltage_b": ("pv_voltage_b_v", "V", "production"),
    "pv_current_a": ("pv_current_a_a", "A", "production"),
    "pv_current_b": ("pv_current_b_a", "A", "production"),
    "grid_power": ("grid_power_w", "W", "production"),
    "frequency": ("frequency_hz", "Hz", "production"),
    "l1_power": ("power_l1_w", "W", "production"),
    "l2_power": ("power_l2_w", "W", "production"),
    "l3_power": ("power_l3_w", "W", "production"),
    "l1_current": ("current_l1_a", "A", "production"),
    "l2_current": ("current_l2_a", "A", "production"),
    "l3_current": ("current_l3_a", "A", "production"),
    "l1_voltage": ("voltage_l1_v", "V", "production"),
    "l2_voltage": ("voltage_l2_v", "V", "production"),
    "l3_voltage": ("voltage_l3_v", "V", "production"),
    "daily_yield": ("energy_today_wh", "Wh", "production"),
    "total_yield": ("energy_lifetime_kwh", "kWh", "production"),
    "metering_power_supplied": ("metering_power_supplied_w", "W", "export"),
    "metering_power_absorbed": ("metering_power_absorbed_w", "W", "import"),
    "metering_total_yield": ("metering_total_yield_kwh", "kWh", "export"),
    "metering_total_absorbed": ("metering_total_absorbed_kwh", "kWh", "import"),
    "battery_soc_total": ("battery_soc_pct", "%", "production"),
    "battery_power_charge_total": ("battery_power_charge_w", "W", "production"),
    "battery_power_discharge_total": ("battery_power_discharge_w", "W", "production"),
    "battery_charge_total": ("battery_charge_total_kwh", "kWh", "production"),
    "battery_discharge_total": ("battery_discharge_total_kwh", "kWh", "production"),
}
# "status" has no numeric value and doesn't fit in Reading.value (float) —
# only logged, just like solaredge's inverter_mode.
STATUS_SENSOR = "status"


def _normalize_host(host: str) -> str:
    if host and "://" not in host:
        return f"https://{host}"
    return host


async def _read_sensors(host: str, password: str, group: str, ssl: bool, verify_ssl: bool) -> dict:
    import aiohttp
    import pysma

    url = _normalize_host(host)
    async with aiohttp.ClientSession() as session:
        sma = pysma.SMA(session, url, password=password, group=group or "user")
        try:
            if not await sma.new_session():
                raise RuntimeError("SMA WebConnect: login failed (check host/password/group)")
            sensors = await sma.get_sensors()
            await sma.read(sensors)
            values = {s.name: s.value for s in sensors}
            status = next((s.value for s in sensors if s.name == STATUS_SENSOR), None)
        finally:
            await sma.close_session()
    return {"values": values, "status": status}


class SmaWebconnectPlugin(DevicePlugin):
    def __init__(self, device_id: str, config: dict):
        super().__init__(device_id, config)
        self._last_known_good: dict | None = None

    @property
    def plugin_id(self) -> str:
        return "sma_webconnect"

    async def collect(self) -> list[Reading]:
        host = self.config.get("host")
        password = self.config.get("password")
        if not host or not password:
            raise RuntimeError("No host/password configured for sma_webconnect")
        group = self.config.get("group", "user")
        ssl = bool(self.config.get("ssl", False))
        verify_ssl = bool(self.config.get("verify_ssl", False))

        import aiohttp

        try:
            data = await _read_sensors(host, password, group, ssl, verify_ssl)
            self._last_known_good = data
        except aiohttp.ClientConnectorCertificateError as e:
            log.warning(
                "SMA WebConnect: SSL error at %s — check whether 'ssl'/'verify_ssl' are "
                "configured correctly for this device: %s", host, e,
            )
            data = self._last_known_good
        except (aiohttp.ClientConnectionError, aiohttp.ClientError, TimeoutError, OSError) as e:
            log.warning("SMA WebConnect: connection to %s failed, using last known values: %s", host, e)
            data = self._last_known_good

        if not data:
            return []

        return self._normalize(data)

    def _normalize(self, data: dict) -> list[Reading]:
        timestamp = datetime.now(timezone.utc)
        values = data.get("values") or {}
        readings = []

        for sensor_name, (metric, unit, direction) in SENSOR_TO_METRIC.items():
            value = values.get(sensor_name)
            if value is None:
                continue
            readings.append(Reading(
                device_id=self.device_id, metric=metric, value=float(value),
                unit=unit, timestamp=timestamp, source="sma_webconnect", direction=direction,
            ))

        status = data.get("status")
        if status:
            log.debug("SMA WebConnect status=%s", status)

        return readings

    async def execute(self, command: Command) -> dict:
        raise NotImplementedError("Actuation not supported for sma_webconnect")

    @staticmethod
    async def test_connection(config: dict) -> dict:
        host = config.get("host")
        password = config.get("password")
        if not host or not password:
            raise ValueError("Host and password are required")
        data = await _read_sensors(
            host, password, config.get("group", "user"),
            bool(config.get("ssl", False)), bool(config.get("verify_ssl", False)),
        )
        values = data.get("values") or {}
        return {
            "Current power": f"{values.get('pv_power', 0)} W",
            "Generated today": f"{(values.get('daily_yield') or 0) / 1000:.1f} kWh",
        }
