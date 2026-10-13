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


def user_id() -> str:
    for line in (HERE / ".env").read_text().splitlines():
        key, _, value = line.partition("=")
        if key == "ECOFLOW_USER_ID" and value:
            return value
    raise SystemExit("No ECOFLOW_USER_ID in .env: run get_user_id.py first")


async def find_station():
    found = await BleakScanner.discover(timeout=10, return_adv=True)
    # ponytail: first EcoFlow advertisement wins; match on serial if a second device appears
    for ble_dev, adv in found.values():
        if ECOFLOW in adv.manufacturer_data:
            return eflib.NewDevice(ble_dev, adv)
    raise RuntimeError("Station not in Bluetooth range")


async def read_once(settle: float = 15) -> dict:
    """Connect, let each module report, disconnect. Nothing is written to the station."""
    device = await find_station()
    await device.with_disabled_reconnect().connect(user_id=user_id())
    try:
        await device.wait_until_authenticated_or_error(raise_on_error=True)
        await asyncio.sleep(settle)
        return {f.public_name: getattr(device, f.public_name) for f in device._fields}
    finally:
        await device.disconnect()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(read_once()), indent=2, default=str))
