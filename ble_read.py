"""Read the station once over Bluetooth and print every value as JSON.

    ~/venvs/ecoflow/bin/python ble_read.py
"""

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
# ha-ef-ble protocol library, pinned as a submodule; only its eflib package is used
sys.path.insert(0, str(HERE / "vendor/ha-ef-ble/custom_components/ef_ble"))

import eflib  # noqa: E402
from bleak import BleakScanner  # noqa: E402

ECOFLOW = 0xB5B5  # manufacturer id in the station's advertisement
# one value from each module (ems, pd, inverter, bms, mppt): once all five have
# reported, the read is complete
CORE = ("battery_level", "input_power", "ac_input_voltage", "cell_temperature", "ac_charging_speed")


def user_id() -> str:
    for line in (HERE / ".env").read_text().splitlines():
        key, _, value = line.partition("=")
        if key == "ECOFLOW_USER_ID" and value:
            return value
    raise SystemExit("No ECOFLOW_USER_ID in .env: run get_user_id.py first")


async def find_station():
    seen = {}

    def is_station(ble_dev, adv) -> bool:
        # ponytail: first EcoFlow advertisement wins; match on serial if a second device appears
        if ECOFLOW not in adv.manufacturer_data:
            return False
        seen["adv"] = adv
        return True

    ble_dev = await BleakScanner.find_device_by_filter(is_station, timeout=10)
    if ble_dev is None:
        raise RuntimeError("Station not in Bluetooth range")
    return eflib.NewDevice(ble_dev, seen["adv"])


async def read_once(settle: float = 15) -> dict:
    """Connect, let each module report, disconnect. Nothing is written to the station."""
    device = await find_station()
    await device.with_disabled_reconnect().connect(user_id=user_id())
    try:
        await device.wait_until_authenticated_or_error(raise_on_error=True)
        # A module that is switched off never reports; the library fills its values with
        # 0 ten seconds after login, so this loop ends either way.
        for _ in range(int(settle * 4)):
            if all(getattr(device, name) is not None for name in CORE):
                break
            await asyncio.sleep(0.25)
        return {f.public_name: getattr(device, f.public_name) for f in device._fields}
    finally:
        await device.disconnect()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(read_once()), indent=2, default=str))
