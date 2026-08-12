"""
P1 Serial plugin — reads the smart meter's P1 port directly via a USB
(serial) cable. Same parse/CRC/lock logic as the previous (sync)
implementation in agent/integrations/p1_serial.py, now behind the
DevicePlugin interface with all blocking serial I/O explicitly routed
through run_blocking() so the event loop never blocks on a hanging/noisy
port.

Deliberately has no knowledge of which OBIS fields are "important" — this
plugin passes on every recognized OBIS line 1-to-1. Interpretation happens
on the platform side (see app/integrations/p1_serial.py::normalize_p1_serial).
"""
import fcntl
import logging
import re
import time
from datetime import datetime, timezone

from core.plugin import Command, DevicePlugin, Reading

log = logging.getLogger("plugin.p1_serial")

DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUDRATE = 115200
TELEGRAM_TIMEOUT = 15  # seconds — DSMR meters send a telegram roughly every 1s

OBIS_LINE = re.compile(r"^(\d+-\d+:\d+\.\d+\.\d+(?:\.\d+)?)((?:\([^)]*\))+)", re.MULTILINE)
OBIS_VALUES = re.compile(r"\(([^)]*)\)")


def _crc16(data: bytes) -> int:
    """CRC16/ARC (poly 0xA001, init 0x0000) as used in the DSMR telegram."""
    crc = 0x0000
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 0x0001 else crc >> 1
    return crc


def parse_telegram(raw: bytes) -> dict:
    text = raw.decode("ascii", errors="replace")
    bang_idx = text.rfind("!")
    if bang_idx == -1:
        raise ValueError("Telegram received without CRC terminator ('!')")

    crc_match = re.match(r"!([0-9A-Fa-f]{4})", text[bang_idx:])
    if crc_match:
        expected = int(crc_match.group(1), 16)
        actual = _crc16(raw[:bang_idx + 1])
        if expected != actual:
            raise ValueError(f"CRC error: expected {expected:04X}, calculated {actual:04X}")

    obis_data: dict[str, str | list[str]] = {}
    for m in OBIS_LINE.finditer(text):
        code = m.group(1)
        values = OBIS_VALUES.findall(m.group(2))
        obis_data[code] = values[0] if len(values) == 1 else values

    if not obis_data:
        raise ValueError("No OBIS fields found in telegram")
    return obis_data


def _power_port(ser) -> None:
    """RTS high after opening (some P1 splitters need this). Deliberately no
    DTR toggle: that triggers a reset on FTDI-like adapters."""
    try:
        ser.rts = True
    except Exception as e:
        log.warning("could not set RTS on %s: %s", ser.port, e)


def _lock_port(ser, port: str) -> None:
    """Exclusive non-blocking flock so a test_connection call and the running
    poll cycle don't get in each other's way."""
    try:
        fcntl.flock(ser.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        ser.close()
        raise RuntimeError(f"Port {port} is already in use, try again in a few seconds")


def _read_telegram(ser) -> bytes:
    """Total deadline instead of just a per-line timeout, otherwise a noisy
    connection could let the blocking read run forever."""
    deadline = time.monotonic() + TELEGRAM_TIMEOUT
    buf = bytearray()
    collecting = False
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("No complete telegram received within the timeout")
        line = ser.readline()
        if not line:
            raise TimeoutError("No data received from P1 port (timeout)")
        if not collecting:
            if line.startswith(b"/"):
                collecting = True
                buf = bytearray(line)
            continue
        buf.extend(line)
        if line.startswith(b"!"):
            return bytes(buf)


class P1SerialPlugin(DevicePlugin):
    def __init__(self, device_id: str, config: dict):
        super().__init__(device_id, config)
        self._serial = None

    @property
    def plugin_id(self) -> str:
        return "p1_serial"

    def _port_config(self) -> tuple[str, int]:
        return self.config.get("port") or DEFAULT_PORT, int(self.config.get("baudrate") or DEFAULT_BAUDRATE)

    def _open_serial_blocking(self):
        import serial

        port, baudrate = self._port_config()
        ser = serial.Serial(
            port=port, baudrate=baudrate,
            bytesize=serial.EIGHTBITS if baudrate == 115200 else serial.SEVENBITS,
            parity=serial.PARITY_NONE if baudrate == 115200 else serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE, timeout=TELEGRAM_TIMEOUT,
        )
        _power_port(ser)
        _lock_port(ser, port)
        return ser

    def _collect_blocking(self) -> dict:
        """All blocking serial I/O in one go, invoked via run_blocking()."""
        if self._serial is None or not self._serial.is_open:
            self._serial = self._open_serial_blocking()
        raw = _read_telegram(self._serial)
        return parse_telegram(raw)

    async def collect(self) -> list[Reading]:
        try:
            data = await self.run_blocking(self._collect_blocking)
        except Exception:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
            raise
        timestamp = datetime.now(timezone.utc)
        return [
            Reading(device_id=self.device_id, metric=obis_code, value=value, unit="",
                    timestamp=timestamp, source="p1", direction="import")
            for obis_code, value in data.items()
        ]

    async def execute(self, command: Command) -> dict:
        raise NotImplementedError("Actuation not yet supported for p1_serial")

    async def on_stop(self):
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    @staticmethod
    def test_connection(config: dict) -> dict:
        import serial

        port = config.get("port") or DEFAULT_PORT
        baudrate = int(config.get("baudrate") or DEFAULT_BAUDRATE)
        try:
            ser = serial.Serial(
                port=port, baudrate=baudrate,
                bytesize=serial.EIGHTBITS if baudrate == 115200 else serial.SEVENBITS,
                parity=serial.PARITY_NONE if baudrate == 115200 else serial.PARITY_EVEN,
                stopbits=serial.STOPBITS_ONE, timeout=TELEGRAM_TIMEOUT,
            )
        except serial.SerialException as e:
            raise RuntimeError(f"Cannot open port {port}: {e}")

        _power_port(ser)
        _lock_port(ser, port)
        try:
            data = parse_telegram(_read_telegram(ser))
        except (TimeoutError, ValueError) as e:
            raise RuntimeError(str(e))
        finally:
            ser.close()
        return {"port": port, "baudrate": baudrate,
                **{k: (", ".join(v) if isinstance(v, list) else v) for k, v in data.items()}}
