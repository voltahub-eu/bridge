"""GoodWe local UDP plugin — async `goodwe` library (port 8899), same
structure as `plugins/sma_webconnect/plugin.py`: the library is already
async itself, so the calls are awaited directly in `collect()`, no
`run_blocking()` needed.

`_last_known_good` is an instance attribute (not a SQLite cache) — same
convention as sma_webconnect/solaredge: the "last known good value" only
needs to survive the process lifetime, not a restart.

Note: the `goodwe` library discovers its own sensor set per inverter model
(ET/EH/DT/XS/…) via `inverter.sensors()`. SENSOR_TO_METRIC covers the most
common sensor IDs; missing sensors are simply skipped by `_normalize()`."""
import logging
from datetime import datetime, timezone

from core.plugin import Command, DevicePlugin, Reading

log = logging.getLogger("plugin.goodwe")

# sensor_id (goodwe-library) → (metric, unit, direction)
SENSOR_TO_METRIC = {
    "ppv": ("current_power_w", "W", "production"),
    "ppv1": ("pv1_power_w", "W", "production"),
    "vpv1": ("pv1_voltage_v", "V", "production"),
    "ipv1": ("pv1_current_a", "A", "production"),
    "ppv2": ("pv2_power_w", "W", "production"),
    "vpv2": ("pv2_voltage_v", "V", "production"),
    "ipv2": ("pv2_current_a", "A", "production"),
    "pgrid": ("grid_power_w", "W", "production"),
    "fgrid": ("grid_frequency_hz", "Hz", "production"),
    "vgrid": ("grid_voltage_v", "V", "production"),
    "igrid": ("grid_current_a", "A", "production"),
    "temperature": ("inverter_temp_c", "degC", "production"),
    "e_day": ("energy_today_kwh", "kWh", "production"),
    "e_total": ("energy_lifetime_kwh", "kWh", "production"),
    "h_total": ("running_hours_total", "h", "production"),
    "battery_soc": ("battery_soc_pct", "%", "production"),
    "battery_power": ("battery_power_w", "W", "production"),
    "battery_temperature": ("battery_temp_c", "degC", "production"),
    "meter_active_power_total": ("meter_power_w", "W", "import"),
    "house_consumption": ("load_power_w", "W", "consumption"),
}
# "work_mode"/"work_mode_label" have no numeric value and don't fit in
# Reading.value (float) — only logged, just like solaredge's inverter_mode.
STATUS_SENSORS = ("work_mode", "work_mode_label")


async def _read_sensors(host: str) -> dict:
    import goodwe

    inverter = await goodwe.connect(host)
    data = await inverter.read_runtime_data()
    return {"values": data}


class GoodwePlugin(DevicePlugin):
    def __init__(self, device_id: str, config: dict):
        super().__init__(device_id, config)
        self._last_known_good: dict | None = None

    @property
    def plugin_id(self) -> str:
        return "goodwe"

    async def collect(self) -> list[Reading]:
        host = self.config.get("host")
        if not host:
            raise RuntimeError("No host configured for goodwe")

        try:
            data = await _read_sensors(host)
            self._last_known_good = data
        except (TimeoutError, OSError, ConnectionError) as e:
            log.warning("GoodWe: connection to %s failed, using last known values: %s", host, e)
            data = self._last_known_good

        if not data:
            return []

        return self._normalize(data)

    def _normalize(self, data: dict) -> list[Reading]:
        timestamp = datetime.now(timezone.utc)
        values = data.get("values") or {}
        readings = []

        for sensor_id, (metric, unit, direction) in SENSOR_TO_METRIC.items():
            value = values.get(sensor_id)
            if value is None:
                continue
            readings.append(Reading(
                device_id=self.device_id, metric=metric, value=float(value),
                unit=unit, timestamp=timestamp, source="goodwe", direction=direction,
            ))

        for status_sensor in STATUS_SENSORS:
            status = values.get(status_sensor)
            if status:
                log.debug("GoodWe %s=%s", status_sensor, status)

        return readings

    async def execute(self, command: Command) -> dict:
        raise NotImplementedError("Actuation not supported for goodwe")

    @staticmethod
    async def test_connection(config: dict) -> dict:
        host = config.get("host")
        if not host:
            raise ValueError("host is required")
        data = await _read_sensors(host)
        values = data.get("values") or {}
        return {
            "Current power": f"{values.get('ppv', 0)} W",
            "Generated today": f"{values.get('e_day', 0)} kWh",
        }
