"""Log the station to SQLite once a minute.

Each read connects over Bluetooth, takes every value and disconnects, because the
station accepts one Bluetooth connection at a time and the phone app needs the gaps.

    ~/venvs/ecoflow/bin/python collector.py
"""

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

from ble_read import read_once

# the library warns on every disconnect, and every read ends with one
logging.disable(logging.WARNING)

DB = Path("/home/pi/data/ecoflow/ecoflow.db")
EVERY = 60  # seconds between reads


def save(db: sqlite3.Connection, values: dict, ts: int) -> None:
    """One row per read, one column per value; new values get their column on arrival."""
    values = {name: value for name, value in values.items() if value is not None}
    have = {row[1] for row in db.execute("PRAGMA table_info(readings)")}
    for name in values:
        if name not in have:
            db.execute(f'ALTER TABLE readings ADD COLUMN "{name}"')
    columns = ", ".join(f'"{name}"' for name in values)
    marks = ", ".join("?" * len(values))
    db.execute(f"INSERT INTO readings (ts, {columns}) VALUES (?, {marks})", [ts, *values.values()])
    db.commit()


def open_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS readings (ts INTEGER PRIMARY KEY)")
    return db


async def main() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = open_db(DB)
    while True:
        started = time.monotonic()
        try:
            save(db, await asyncio.wait_for(read_once(), 45), int(time.time()))
        except Exception as e:  # phone app holds the link, station out of range, Bluetooth hiccup
            print(f"read failed: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(max(0, EVERY - (time.monotonic() - started)))


def demo() -> None:
    db = open_db(":memory:")
    save(db, {"battery_level": 88.7, "ac_ports": True, "battery_1_sn": None}, 1)
    save(db, {"battery_level": 88.5, "input_power": 155}, 2)  # a value that shows up later
    assert db.execute("SELECT * FROM readings ORDER BY ts").fetchall() == [(1, 88.7, 1, None), (2, 88.5, None, 155)]
    assert [row[1] for row in db.execute("PRAGMA table_info(readings)")] == ["ts", "battery_level", "ac_ports", "input_power"]


if __name__ == "__main__":
    demo()
    asyncio.run(main())
