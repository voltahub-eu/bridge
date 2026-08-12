"""SolarEdge local API plugin — sync `solaredge_local` library, wrapped via
run_blocking() so the event loop doesn't block (same pattern as `enphase`).
`get_status()` and `get_maintenance()` are done within the same executor
call so both read against the same internal scale-factor snapshot of the
inverter (prevents a race condition between two separate, sequentially
awaited calls)."""
import logging
from datetime import datetime, timezone

from core.plugin import Command, DevicePlugin, Reading

log = logging.getLogger("plugin.solaredge")


def _strip_protocol(host: str) -> str:
    """The SolarEdge inverter only serves its local API over plain HTTP — the
    `host` config does follow the "address with protocol" convention (for
    consistency with the other plugins/the onboarding UI), so any http(s)://
    given is ignored here before calling the solaredge_local library, which
    itself expects a bare host(:port)."""
    if "://" in host:
        host = host.split("://", 1)[1]
    return host.rstrip("/")


def _status_message_to_dict(status) -> dict:
    """Converts the parsed protobuf Status message to a plain dict, so the
    rest of this file stays protobuf-independent (same approach as
    app/integrations/solaredge.py::_status_message_to_dict in the main app)."""
    return {
        "powerWatt": status.powerWatt,
        "energy": {
            "today": status.energy.today,
            "thisMonth": status.energy.thisMonth,
            "thisYear": status.energy.thisYear,
            "total": status.energy.total,
        },
        "voltage": status.voltage,
        "frequencyHz": status.frequencyHz,
        "status": status.status,
        "inverters": {
            "primary": {
                "voltage": status.inverters.primary.voltage,
                "temperature": {"value": status.inverters.primary.temperature.value},
            },
        },
        "metersList": [
            {"currentPower": m.currentPower, "totalEnergy": m.totalEnergy}
            for m in status.metersList
        ],
    }


def _get_local_api(host: str):
    # ProtocolBuffersConverter must be explicitly registered as a converter —
    # the solaredge_local.SolarEdge class does not do this itself (contrary
    # to what the package documentation suggests), so without this line
    # get_status()/get_maintenance() return a raw uplink Response object
    # instead of a parsed protobuf message.
    from solaredge_local import SolarEdge
    from uplink_protobuf import ProtocolBuffersConverter

    return SolarEdge(f"http://{_strip_protocol(host)}", converters=(ProtocolBuffersConverter(),))


class SolaredgePlugin(DevicePlugin):
    def __init__(self, device_id: str, config: dict):
        super().__init__(device_id, config)
        self._last_known_good: dict | None = None

    @property
    def plugin_id(self) -> str:
        return "solaredge"

    def _read_blocking(self, host: str) -> dict:
        from google.protobuf.json_format import MessageToDict

        api = _get_local_api(host)
        status = api.get_status()
        try:
            # MessageToDict instead of manual field mapping (as with status)
            # because _normalize() only reads a small, optional part of this
            # message (diagnostics.optimizer.*) — not worth writing out every
            # protobuf field explicitly here.
            maintenance = MessageToDict(api.get_maintenance())
        except Exception as e:
            log.warning("SolarEdge local: get_maintenance() failed, continuing without optimizer data: %s", e)
            maintenance = None
        return {"status": _status_message_to_dict(status), "maintenance": maintenance}

    async def collect(self) -> list[Reading]:
        import requests

        host = self.config.get("host")
        if not host:
            raise RuntimeError("No host configured for solaredge")

        try:
            data = await self.run_blocking(self._read_blocking, host)
            self._last_known_good = data
        except requests.exceptions.ConnectTimeout as e:
            log.warning("SolarEdge local: timeout at %s, using last known values: %s", host, e)
            data = self._last_known_good
        except requests.exceptions.HTTPError as e:
            log.warning("SolarEdge local: HTTP error at %s, using last known values: %s", host, e)
            data = self._last_known_good

        if not data:
            return []

        return self._normalize(data)

    def _normalize(self, data: dict) -> list[Reading]:
        timestamp = datetime.now(timezone.utc)
        status = data.get("status") or {}
        maintenance = data.get("maintenance") or {}
        readings = []

        def add(metric, value, unit, direction="production"):
            if value is not None:
                readings.append(Reading(
                    device_id=self.device_id, metric=metric, value=float(value),
                    unit=unit, timestamp=timestamp, source=self.plugin_id, direction=direction,
                ))

        energy = status.get("energy", {})
        add("current_power_w", status.get("powerWatt"), "W")
        add("energy_today_wh", energy.get("today"), "Wh")
        add("energy_month_wh", energy.get("thisMonth"), "Wh")
        add("energy_year_wh", energy.get("thisYear"), "Wh")
        add("energy_lifetime_wh", energy.get("total"), "Wh")
        add("grid_voltage_v", status.get("voltage"), "V")
        add("grid_frequency_hz", status.get("frequencyHz"), "Hz")

        primary = (status.get("inverters") or {}).get("primary", {})
        add("dc_voltage_v", primary.get("voltage"), "V")
        temperature = (primary.get("temperature") or {}).get("value")
        add("inverter_temp_c", temperature, "degC")

        # inverter_mode (e.g. "MPPT"/"SLEEPING"/"FAULT") is a string and doesn't fit
        # in Reading.value (float) — only log it, don't emit it as a metric.
        inverter_mode = primary.get("mode") or status.get("status")
        if inverter_mode:
            log.debug("SolarEdge lokaal inverter_mode=%s", inverter_mode)

        meters = status.get("metersList") or []
        if len(meters) > 0:
            feed_in = meters[0]
            add("feed_in_power_w", feed_in.get("currentPower"), "W", direction="export")
            add("feed_in_total_wh", feed_in.get("totalEnergy"), "Wh", direction="export")
        if len(meters) > 1:
            grid_meter = meters[1]
            grid_power = grid_meter.get("currentPower")
            add("grid_power_w", grid_power, "W", direction="import" if (grid_power or 0) >= 0 else "export")
            add("purchased_total_wh", grid_meter.get("totalEnergy"), "Wh", direction="import")

        optimizers = maintenance.get("diagnostics", {}).get("optimizer", {}) if maintenance else {}
        online = optimizers.get("online")
        total = optimizers.get("total")
        if online:
            add("optimizer_count_online", online, "", direction="production")
            add("optimizer_count_total", total, "", direction="production")
            add("optimizer_avg_voltage_v", optimizers.get("avgVoltage"), "V")
            add("optimizer_avg_current_a", optimizers.get("avgCurrent"), "A")
            add("optimizer_avg_power_w", optimizers.get("avgPower"), "W")
            add("optimizer_avg_temp_c", optimizers.get("avgTemperature"), "degC")

        return readings

    async def execute(self, command: Command) -> dict:
        raise NotImplementedError("Actuation not supported for solaredge")

    @staticmethod
    async def test_connection(config: dict) -> dict:
        host = config.get("host")
        if not host:
            raise ValueError("host is required")

        import asyncio
        import functools

        def _check():
            api = _get_local_api(host)
            return _status_message_to_dict(api.get_status())

        loop = asyncio.get_running_loop()
        status = await loop.run_in_executor(None, functools.partial(_check))
        return {
            "Current power": f"{status.get('powerWatt', 0)} W",
            "Generated today": f"{status.get('energy', {}).get('today', 0) / 1000:.1f} kWh",
        }
