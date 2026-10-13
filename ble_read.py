"""The station over Bluetooth: connect, read every value, disconnect.

    ~/venvs/ecoflow/bin/python ble_read.py     # one read, printed as JSON
"""

import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
# ha-ef-ble protocol library, pinned as a submodule; only its eflib package is used
sys.path.insert(0, str(HERE / "vendor/ha-ef-ble/custom_components/ef_ble"))

import eflib  # noqa: E402
from bleak import BleakScanner  # noqa: E402
from eflib import model  # noqa: E402
from bleak_retry_connector import close_stale_connections_by_address  # noqa: E402

ECOFLOW = 0xB5B5  # manufacturer id in the station's advertisement
# one value from each module (ems, pd, inverter, bms, mppt): once all five have
# reported, the read is complete
CORE = ("battery_level", "input_power", "ac_input_voltage", "cell_temperature", "ac_charging_speed")
# the station's Bluetooth address, remembered from the last time it was found
ADDRESS = Path("/home/pi/data/ecoflow/station-address")


def user_id() -> str:
    for line in (HERE / ".env").read_text().splitlines():
        key, _, value = line.partition("=")
        if key == "ECOFLOW_USER_ID" and value:
            return value
    raise SystemExit("No ECOFLOW_USER_ID in .env: run get_user_id.py first")


async def scan():
    seen = {}

    def is_station(ble_dev, adv) -> bool:
        # ponytail: first EcoFlow advertisement wins; match on serial if a second device appears
        if ECOFLOW not in adv.manufacturer_data:
            return False
        seen["adv"] = adv
        return True

    ble_dev = await BleakScanner.find_device_by_filter(is_station, timeout=10)
    return eflib.NewDevice(ble_dev, seen["adv"]) if ble_dev else None


async def find_station():
    device = await scan()
    if device is None and ADDRESS.exists():
        # A link left open by a process that was killed stays up in BlueZ, and a connected
        # station stops advertising, so the scan cannot see it. Drop any such link held by
        # this Pi and look once more. (A phone holding the link is not affected.)
        await close_stale_connections_by_address(ADDRESS.read_text().strip())
        device = await scan()
    if device is None:
        raise RuntimeError("Station not in Bluetooth range")
    try:
        if not ADDRESS.exists() or ADDRESS.read_text().strip() != device.address:
            ADDRESS.write_text(device.address)
    except OSError:
        pass  # no data directory (run somewhere else): only the recovery above is lost
    return device


def unnamed(packets: dict[str, str]) -> dict:
    """Values the station sends but the library gives no name to, read from the packets."""
    values = {}
    if "04-20-02" in packets:  # the inverter's heartbeat
        inverter = model.DirectInvDeltaHeartbeatPack.from_bytes(bytes.fromhex(packets["04-20-02"]))
        values["inverter_temperature"] = inverter.out_temp
    return values


class Station:
    """A connected, logged-in station. Nothing is written to it beyond the login."""

    def __init__(self, device) -> None:
        self.device = device
        # Latest decrypted payload of each packet type, as hex, keyed "source-set-id".
        # ponytail: extra batteries report on the same type as each other; add the slot byte to the key if one is ever attached
        self.packets: dict[str, str] = {}
        self.heard = time.monotonic()
        device.on_packet_parsed(self._keep)

    def _keep(self, packet) -> None:
        self.packets[f"{packet.src:02x}-{packet.cmd_set:02x}-{packet.cmd_id:02x}"] = packet.payload.hex()
        self.heard = time.monotonic()

    @classmethod
    async def connect(cls, settle: float = 15) -> "Station":
        station = cls(await find_station())
        await station.device.with_disabled_reconnect().connect(user_id=user_id())
        try:
            await station.device.wait_until_authenticated_or_error(raise_on_error=True)
            # A module that is switched off never reports; the library fills its values with
            # 0 ten seconds after login, so this loop ends either way.
            for _ in range(int(settle * 4)):
                if all(getattr(station.device, name) is not None for name in CORE):
                    break
                await asyncio.sleep(0.25)
        except BaseException:
            await station.close()
            raise
        return station

    def read(self) -> dict:
        """Every value as of now; the library keeps them current while the link is up."""
        values = {f.public_name: getattr(self.device, f.public_name) for f in self.device._fields}
        values.update(unnamed(self.packets))
        # everything the station said, so values the library has no name for can be decoded later
        values["raw"] = json.dumps(self.packets, sort_keys=True)
        return values

    def quiet_for(self) -> float:
        """Seconds since the station last sent anything."""
        return time.monotonic() - self.heard

    async def close(self) -> None:
        await self.device.disconnect()


async def read_once() -> dict:
    station = await Station.connect()
    try:
        return station.read()
    finally:
        await station.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(read_once()), indent=2, default=str))
