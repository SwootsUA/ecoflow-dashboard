"""Log the station to SQLite once a minute, and stream it while someone watches the page.

The station accepts one Bluetooth connection at a time. With nobody watching, each read
connects, takes every value and disconnects, so the phone app has the gaps. While the
page is open in a visible tab the link is held and the values are published every second.

    ~/venvs/ecoflow/bin/python collector.py
"""

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from ble_read import Station

# the library warns on every disconnect, and every read ends with one
logging.disable(logging.WARNING)

DB = Path(os.environ.get("ECOFLOW_DB", "/home/pi/data/ecoflow/ecoflow.db"))
# Both sit on /run: memory, not the SD card. The collector writes LIVE every second while
# watched; the page touches VIEWER on every request from a visible tab.
LIVE = Path(os.environ.get("ECOFLOW_LIVE", "/run/ecoflow-collector/live.json"))
VIEWER = Path(os.environ.get("ECOFLOW_VIEWER", "/run/ecoflow-page/viewer"))
EVERY = 60  # seconds between logged rows
TICK = 1  # seconds between published readings while the link is held
VIEWER_FRESH = 10  # seconds one page request keeps the link held
RETRY_WATCHED = 15  # seconds before reconnecting after a failure with someone watching
QUIET_LIMIT = 10  # seconds without a packet before a held link counts as dead
# systemd-timesyncd creates this once the clock is set after boot. The Pi has no clock
# battery and loses power together with the station, so after an outage it boots on a
# stale clock; rows stamped before the network answers would land in the past.
CLOCK_SET = Path("/run/systemd/timesync/synchronized")


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


def viewer_present() -> bool:
    try:
        return time.time() - VIEWER.stat().st_mtime < VIEWER_FRESH
    except OSError:
        return False


def publish(values: dict, ts: float) -> None:
    """The latest reading for the page, swapped in whole so it never reads half a file."""
    live = {name: value for name, value in values.items() if value is not None and name != "raw"}
    try:
        swap = LIVE.with_suffix(".tmp")
        swap.write_text(json.dumps(live | {"ts": ts}))
        swap.replace(LIVE)
    except OSError:
        pass  # run by hand without the runtime directory: the database still gets its rows


class Collector:
    def __init__(self, db: sqlite3.Connection, connect=Station.connect, clock=time.time) -> None:
        self.db = db
        self.connect = connect
        self.clock = clock
        self.row_due = 0.0  # time.monotonic() when the next row is due

    async def session(self) -> None:
        """One connection: log a row when one is due, and for as long as someone is
        watching stay connected and publish every TICK seconds."""
        started = time.monotonic()
        station = await asyncio.wait_for(self.connect(), 45)
        held = False
        try:
            while True:
                values = station.read()
                publish(values, self.clock())
                if time.monotonic() >= self.row_due:
                    save(self.db, values, int(self.clock()))
                    # an unwatched read is due a minute after it began, not after it ended
                    self.row_due = max(self.row_due, started) + EVERY
                if not viewer_present():
                    return
                if not held:
                    held = True
                    print("someone is watching: holding the Bluetooth link", flush=True)
                await asyncio.sleep(TICK)
                if station.quiet_for() > QUIET_LIMIT:
                    raise ConnectionError("station stopped sending")
        finally:
            if held:
                print("link released", flush=True)
            await asyncio.wait_for(station.close(), 15)

    async def run(self) -> None:
        retry_at = 0.0
        while True:
            now = time.monotonic()
            if now < retry_at or (now < self.row_due and not viewer_present()):
                await asyncio.sleep(1)
                continue
            try:
                if not CLOCK_SET.exists():
                    raise RuntimeError("clock not set since boot")
                await self.session()
            except Exception as e:  # phone app holds the link, station out of range, Bluetooth hiccup
                print(f"read failed: {type(e).__name__}: {e}", flush=True)
                retry_at = time.monotonic() + (RETRY_WATCHED if viewer_present() else EVERY)


def demo() -> None:
    global LIVE, VIEWER, EVERY, TICK

    db = open_db(":memory:")
    save(db, {"battery_level": 88.7, "ac_ports": True, "battery_1_sn": None}, 1)
    save(db, {"battery_level": 88.5, "input_power": 155}, 2)  # a value that shows up later
    assert db.execute("SELECT * FROM readings ORDER BY ts").fetchall() == [(1, 88.7, 1, None), (2, 88.5, None, 155)]
    assert [row[1] for row in db.execute("PRAGMA table_info(readings)")] == ["ts", "battery_level", "ac_ports", "input_power"]

    class FakeStation:
        closed = quiet = 0

        def read(self):
            return {"battery_level": 50, "raw": "{}", "absent": None}

        def quiet_for(self):
            return self.quiet

        async def close(self):
            self.closed += 1

    real = LIVE, VIEWER, EVERY, TICK
    try:
        with tempfile.TemporaryDirectory() as tmp:
            LIVE, VIEWER, EVERY, TICK = Path(tmp, "live.json"), Path(tmp, "viewer"), 0.2, 0.01
            _demo_sessions(FakeStation())
    finally:
        LIVE, VIEWER, EVERY, TICK = real


def _demo_sessions(station) -> None:
    ticks = iter(range(1000, 3000))

    async def connect():
        return station

    collector = Collector(open_db(":memory:"), connect, clock=lambda: next(ticks))
    rows = lambda: collector.db.execute("SELECT count(*) FROM readings").fetchone()[0]

    # nobody watching: one row, the reading published without the raw dump, link closed at once
    asyncio.run(collector.session())
    assert rows() == 1 and station.closed == 1 and not viewer_present()
    assert json.loads(LIVE.read_text()) == {"battery_level": 50, "ts": 1000}
    assert 0 < collector.row_due - time.monotonic() <= EVERY

    # someone watching: the link is held and rows keep their own pace, far fewer than ticks
    async def watched():
        VIEWER.touch()
        task = asyncio.create_task(collector.session())
        await asyncio.sleep(1)
        assert not task.done() and station.closed == 1
        VIEWER.unlink()
        await task

    asyncio.run(watched())
    assert station.closed == 2 and 3 <= rows() <= 9, rows()

    # a held link that goes quiet is dropped as a failure, and still closed
    VIEWER.touch()
    station.quiet = QUIET_LIMIT + 1
    try:
        asyncio.run(collector.session())
        raise AssertionError("a quiet station should fail the session")
    except ConnectionError:
        assert station.closed == 3


if __name__ == "__main__":
    DB.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(Collector(open_db(DB)).run())
