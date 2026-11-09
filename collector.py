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
import re
import signal
import sqlite3
import tempfile
import time
from pathlib import Path

import outages
from collections import deque

from grid import ON_GRID_SQL, on_grid, refused
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
# The link is held all the time (the phone app reaches the station over Wi-Fi anyway). The page's
# "free Bluetooth" button touches RELEASE: for RELEASE_FOR seconds the collector goes back to one short
# connection a minute, so a phone can pair over Bluetooth in the gaps.
ALWAYS_HOLD = True
RELEASE = Path(os.environ.get("ECOFLOW_RELEASE", "/run/ecoflow-page/release"))
RELEASE_FOR = 600
RETRY_WATCHED = 15  # seconds before reconnecting after a failure with someone watching
QUIET_LIMIT = 10  # seconds without a packet before a held link counts as dead
# systemd-timesyncd creates this once the clock is set after boot. The Pi has no clock
# battery and loses power together with the station, so after an outage it boots on a
# stale clock; rows stamped before the network answers would land in the past.
CLOCK_SET = Path("/run/systemd/timesync/synchronized")
# Settings from the page: the page (another user, no Bluetooth) writes REQUEST, the collector applies it
# over its link and writes RESULT with the same id. A request whose id is already in RESULT is done.
REQUEST = Path(os.environ.get("ECOFLOW_REQUEST", "/run/ecoflow-page/settings.json"))
RESULT = LIVE.with_name("settings-result.json")
# A line hovering at the station's limit (it refuses below ~175 V) makes it flip between grid and battery
# every minute or two. After FLIPS flips within FLIP_WINDOW seconds with the line under SAG_V, AC charging
# is paused; it comes back after the line has held SAG_V or more for RESUME_AFTER seconds. The guard only
# resumes what it paused itself (GUARD holds that across restarts), so charging switched off by hand stays off.
SAG_V, FLIPS, FLIP_WINDOW, RESUME_AFTER = 180, 3, 600, 120
REQUEST_FRESH = 600  # seconds a settings request from the page stays valid
GUARD = DB.parent / "sag-guard.json"


class SagGuard:
    def __init__(self, path: Path = GUARD) -> None:
        self.path = path
        self.seen = deque()  # (ts, volts, taking power) of recent reads
        try:
            self.paused = bool(json.loads(path.read_text()).get("paused_at"))
        except (OSError, ValueError):
            self.paused = False

    def step(self, values: dict, ts: float) -> str | None:
        """Feed one reading; "pause" or "resume" when the charging switch should change, else None."""
        volts = values.get("ac_input_voltage") or 0
        self.seen.append((ts, volts, on_grid(values)))
        while self.seen and self.seen[0][0] < ts - FLIP_WINDOW:
            self.seen.popleft()
        if not self.paused:
            powered = [taking for _, v, taking in self.seen if v > 50]
            flips = sum(a != b for a, b in zip(powered, powered[1:]))
            if flips >= FLIPS and 50 < volts < SAG_V and values.get("ac_charging") is not False:
                return "pause"
        else:
            steady = [v for t_, v, _ in self.seen if t_ >= ts - RESUME_AFTER]
            covered = self.seen[0][0] <= ts - RESUME_AFTER
            if covered and steady and min(steady) >= SAG_V:
                return "resume"
        return None

    def done(self, action: str, ts: float) -> None:
        """The station took the change: remember it (a failed send is retried on the next reading)."""
        self.paused = action == "pause"
        swap = self.path.with_suffix(".tmp")
        swap.write_text(json.dumps({"paused_at": ts} if self.paused else {}))
        swap.replace(self.path)


SETTERS = {"battery_charge_limit_max": "set_battery_charge_limit_max",
           "battery_charge_limit_min": "set_battery_charge_limit_min",
           "ac_charging_speed": "set_ac_charging_speed"}


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


# Per-second detail: every read is compared with the last stored value of each number, and a change
# bigger than its deadband (sensor noise: voltages wobble by 0.01 V every second) goes to the changes
# table. They are buffered and written with the minute row, so the SD card sees one write a minute.
RAW_EVERY = 600  # seconds between minute rows that keep the raw packet dump (~1.5 KB each)


def deadband(name: str) -> float:
    if "voltage" in name:
        return 1.0
    if "power" in name:
        return 5.0
    if "current" in name:
        return 0.2
    if "temperature" in name:
        return 1.0
    return 0.0  # anything else: every change


class Changes:
    def __init__(self) -> None:
        self.last: dict[str, float] = {}
        self.pending: list[tuple[str, int, float]] = []
        self.ids: dict[str, int] = {}

    def observe(self, values: dict, ts: int) -> None:
        for name, value in values.items():
            if isinstance(value, bool):
                value = int(value)
            if not isinstance(value, (int, float)):
                continue
            old = self.last.get(name)
            if old is None or (value != old and abs(value - old) >= deadband(name)):
                self.last[name] = value
                self.pending.append((name, ts, value))

    def flush(self, db: sqlite3.Connection) -> None:
        """Write what's buffered; the caller commits (save() does, right after)."""
        if not self.pending:
            return
        db.execute("CREATE TABLE IF NOT EXISTS fields (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS changes (field INTEGER NOT NULL, ts INTEGER NOT NULL, value REAL,"
                   " PRIMARY KEY (field, ts)) WITHOUT ROWID")
        for name in {n for n, _, _ in self.pending} - self.ids.keys():
            db.execute("INSERT OR IGNORE INTO fields (name) VALUES (?)", (name,))
            self.ids[name] = db.execute("SELECT id FROM fields WHERE name = ?", (name,)).fetchone()[0]
        # two changes in one second: the later one wins
        db.executemany("INSERT OR REPLACE INTO changes (field, ts, value) VALUES (?, ?, ?)",
                       [(self.ids[n], ts, v) for n, ts, v in self.pending])
        self.pending.clear()


def grid_change(db: sqlite3.Connection, values: dict, ts: int) -> str | None:
    """The Telegram message when this reading flips the grid against the last stored row, else None.

    It compares with the database, not memory, so it is right after a restart too: when the station
    ran flat and took the Pi with it, the first row after boot reports the grid coming back.
    Every outage counts, posted or not, and an unplugged station looks the same as a dead grid.
    """
    try:
        cur = db.execute("SELECT * FROM readings ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
    except sqlite3.OperationalError:  # no readings table yet
        return None
    if row is None:
        return None
    last = dict(zip((c[0] for c in cur.description), row))
    now = on_grid(values)
    if on_grid(last) == now:
        return None
    clock = time.strftime("%H:%M", time.localtime(ts))
    battery = values.get("battery_level") or 0
    if not now:
        line = outages.battery_line({"battery_level": battery,
                                     "battery_charge_limit_min": values.get("battery_charge_limit_min") or 0})
        if refused(values):
            return (f"🟠 Grid at {values['ac_input_voltage']:.0f} V since {clock}, too low: "
                    f"EcoFlow refuses it, on battery{line}")
        return f"🔴 No grid since {clock}{line}"
    on_ts = db.execute(f"SELECT max(ts) FROM readings WHERE {ON_GRID_SQL}").fetchone()[0]
    since = db.execute("SELECT min(ts) FROM readings WHERE ts > ?", (on_ts or 0,)).fetchone()[0]
    gone = outages.lasts((ts - since) / 3600) if since else "?"
    return f"🟢 Grid back {clock} after {gone} · battery {battery:.0f}%"


def tell(text: str) -> None:
    try:
        outages.send(text)
    except OSError as e:  # the bot being down must not cost a row
        print(f"telegram: {e}", flush=True)


def open_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS readings (ts INTEGER PRIMARY KEY)")
    return db


def released() -> bool:
    try:
        return time.time() - RELEASE.stat().st_mtime < RELEASE_FOR
    except OSError:
        return False


def hold() -> bool:
    """Keep the link: always, unless it was freed for the phone app."""
    return not released() and (ALWAYS_HOLD or viewer_present())


def viewer_present() -> bool:
    try:
        return time.time() - VIEWER.stat().st_mtime < VIEWER_FRESH
    except OSError:
        return False


def pending() -> dict | None:
    """The page's settings request, if the collector hasn't answered it yet."""
    try:
        request = json.loads(REQUEST.read_text())
    except (OSError, ValueError):
        return None
    try:
        done = json.loads(RESULT.read_text()).get("id")
    except (OSError, ValueError):
        done = None
    # RESULT lives on /run and is gone after a restart: an old request must not be sent again then,
    # it could undo a change made since in the phone app
    if time.time() - request.get("at", time.time()) > REQUEST_FRESH:
        return None
    return request if request.get("id") and request["id"] != done else None


async def apply(device, request: dict) -> dict:
    """Send each requested setting; the station checks min <= max, so the order follows the request."""
    names = [n for n in SETTERS if n in request]
    # raising the cut-off above the current charge limit needs the limit first, lowering the limit
    # below the current cut-off needs the cut-off first
    if "battery_charge_limit_max" in request and request["battery_charge_limit_max"] < (device.battery_charge_limit_min or 0):
        names.sort(key=lambda n: n != "battery_charge_limit_min")
    result = {"id": request["id"], "ts": time.time(), "ok": True}
    for name in names:
        try:
            # checked again here: the battery may have dropped since the page accepted the request
            if name == "battery_charge_limit_min" and request[name] >= (device.battery_level or 0):
                raise ValueError(f"cut-off {request[name]}% is not below the battery ({device.battery_level}%)")
            done = await asyncio.wait_for(getattr(device, SETTERS[name])(request[name]), 15)
            if done is False:
                raise ValueError("refused by the library's min/max check")
        except Exception as e:
            result.update(ok=False, error=f"{name}: {type(e).__name__}: {e}")
            break
    swap = RESULT.with_suffix(".tmp")
    swap.write_text(json.dumps(result))
    swap.replace(RESULT)
    print(f"settings {request}: {'ok' if result['ok'] else result['error']}", flush=True)
    return result


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
        self.guard = SagGuard()
        self.changes = Changes()
        self.raw_due = 0  # unix time when a minute row next keeps the raw dump

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
                self.changes.observe(values, int(self.clock()))
                if time.monotonic() >= self.row_due:
                    ts = int(self.clock())
                    change = grid_change(self.db, values, ts)
                    self.changes.flush(self.db)
                    if ts >= self.raw_due:
                        self.raw_due = ts + RAW_EVERY
                    else:
                        values = {name: value for name, value in values.items() if name != "raw"}
                    save(self.db, values, ts)
                    if change:
                        await asyncio.to_thread(tell, change)
                    # an unwatched read is due a minute after it began, not after it ended
                    self.row_due = max(self.row_due, started) + EVERY
                request = pending()
                if request:
                    await apply(station.device, request)
                await self.guard_step(station, values)
                if not hold():
                    return
                if not held:
                    held = True
                    print("holding the Bluetooth link", flush=True)
                await asyncio.sleep(TICK)
                if station.quiet_for() > QUIET_LIMIT:
                    raise ConnectionError("station stopped sending")
        finally:
            if held:
                print("link released", flush=True)
            await asyncio.wait_for(station.close(), 15)

    async def guard_step(self, station, values: dict) -> None:
        action = self.guard.step(values, self.clock())
        if not action:
            return
        try:
            await asyncio.wait_for(station.device.enable_ac_charging(action == "resume"), 15)
        except Exception as e:
            print(f"sag guard: {action} failed: {type(e).__name__}: {e}", flush=True)
            return
        self.guard.done(action, self.clock())
        volts = round(values.get("ac_input_voltage") or 0)
        text = (f"⏸ Grid flapping at {volts} V: AC charging paused until it holds {SAG_V} V+ for {RESUME_AFTER // 60} min"
                if action == "pause" else f"▶ Grid steady at {volts} V: AC charging back on")
        print(f"sag guard: {text}", flush=True)
        await asyncio.to_thread(tell, text)

    async def run(self) -> None:
        retry_at = 0.0
        while True:
            now = time.monotonic()
            if now < retry_at or (now < self.row_due and not hold() and not pending()):
                await asyncio.sleep(1)
                continue
            try:
                if not CLOCK_SET.exists():
                    raise RuntimeError("clock not set since boot")
                await self.session()
            except Exception as e:  # phone app holds the link, station out of range, Bluetooth hiccup
                print(f"read failed: {type(e).__name__}: {e}", flush=True)
                retry_at = time.monotonic() + (RETRY_WATCHED if hold() else EVERY)


def demo() -> None:
    global LIVE, VIEWER, EVERY, TICK, ALWAYS_HOLD, RELEASE, REQUEST, RESULT
    grid = open_db(Path(":memory:"))
    on = {"battery_level": 99.0, "battery_charge_limit_min": 15, "ac_input_voltage": 228, "ac_input_power": 0,
          "input_power": 120, "output_power": 110}
    off = dict(on, ac_input_voltage=0)
    t0 = 1792400000  # stamps in local time below
    assert grid_change(grid, on, t0) is None  # nothing stored yet
    save(grid, on, t0)
    assert grid_change(grid, on, t0 + 60) is None
    # the hours come from the live drain rates in outages.json, so only the shape is fixed here
    alert = grid_change(grid, off, t0 + 60)
    assert re.match(rf"🔴 No grid since {time.strftime('%H:%M', time.localtime(t0 + 60))} · 99% lasts \d+h\d\d PC on", alert), alert
    save(grid, off, t0 + 60)
    save(grid, dict(off, battery_level=70.0), t0 + 7260)
    back = grid_change(grid, dict(on, battery_level=70.0, ac_input_voltage=0, ac_input_power=700), t0 + 7320)
    assert back == f"🟢 Grid back {time.strftime('%H:%M', time.localtime(t0 + 7320))} after 2h01 · battery 70%", back
    save(grid, dict(on, battery_level=70.0), t0 + 7320)
    sag = dict(on, battery_level=70.0, ac_input_voltage=166.2, input_power=0)
    assert grid_change(grid, sag, t0 + 7380).startswith("🟠 Grid at 166 V since"), grid_change(grid, sag, t0 + 7380)

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

    real = LIVE, VIEWER, EVERY, TICK, REQUEST, RESULT, ALWAYS_HOLD, RELEASE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            LIVE, VIEWER, EVERY, TICK = Path(tmp, "live.json"), Path(tmp, "viewer"), 0.2, 0.01
            REQUEST, RESULT = Path(tmp, "settings.json"), Path(tmp, "settings-result.json")
            ALWAYS_HOLD, RELEASE = False, Path(tmp, "release")
            _demo_sessions(FakeStation())
            ALWAYS_HOLD = True
            assert hold() and not released()
            RELEASE.touch()
            assert released() and not hold()
            VIEWER.touch()
            assert not hold()  # freed for the phone: not even a watching page takes the link
            os.utime(RELEASE, (time.time() - RELEASE_FOR - 1,) * 2)
            assert hold()
            _demo_settings()
            _demo_sag_guard(Path(tmp, "guard.json"))
            _demo_changes()
    finally:
        LIVE, VIEWER, EVERY, TICK, REQUEST, RESULT, ALWAYS_HOLD, RELEASE = real


def _demo_changes() -> None:
    db = open_db(":memory:")
    c = Changes()
    c.observe({"ac_input_voltage": 220.0, "output_power": 250, "ac_charging": True, "raw": "{}", "battery_level": 50.0}, 1)
    c.observe({"ac_input_voltage": 220.4, "output_power": 253, "ac_charging": True, "battery_level": 50.0}, 2)  # noise
    c.observe({"ac_input_voltage": 218.9, "output_power": 262, "ac_charging": False, "battery_level": 49.9}, 3)
    c.observe({"ac_input_voltage": 0.0, "output_power": 262}, 3)  # same second: the later value wins
    c.flush(db)
    got = db.execute("SELECT f.name, c.ts, c.value FROM changes c JOIN fields f ON f.id = c.field ORDER BY f.name, c.ts").fetchall()
    assert got == [("ac_charging", 1, 1.0), ("ac_charging", 3, 0.0), ("ac_input_voltage", 1, 220.0), ("ac_input_voltage", 3, 0.0),
                   ("battery_level", 1, 50.0), ("battery_level", 3, 49.9), ("output_power", 1, 250.0), ("output_power", 3, 262.0)], got
    assert not c.pending
    c.flush(db)  # nothing buffered: no-op


def _demo_sag_guard(path: Path) -> None:
    on = lambda v: {"ac_input_voltage": v, "ac_input_power": 300, "input_power": 300, "output_power": 250}
    off = lambda v: {"ac_input_voltage": v, "ac_input_power": 0, "input_power": 0, "output_power": 250}
    g = SagGuard(path)
    # steady sagging line, no flipping: nothing
    assert all(g.step(on(176), t) is None for t in range(0, 600, 60))
    # on -> off -> on -> off at 172 V within ten minutes: pause on the third flip
    assert g.step(off(172), 660) is None and g.step(on(176), 720) is None
    assert g.step(off(171), 780) == "pause"
    g.done("pause", 780)
    assert SagGuard(path).paused  # survives a restart
    # back over 180 V, but not for two minutes yet, then a dip: hold
    assert g.step(on(185), 840) is None and g.step(on(178), 900) is None and g.step(on(190), 960) is None
    assert g.step(on(190), 1020) is None  # 120 s back still includes the 178 V reading
    assert g.step(on(195), 1080) == "resume"
    g.done("resume", 1080)
    assert not SagGuard(path).paused
    # charging already off by hand: never paused, so never resumed either
    h = SagGuard(Path(str(path) + "2"))
    for t, r in enumerate([on(176), off(172), on(176), off(171)]):
        assert h.step(dict(r, ac_charging=False), 2000 + t * 60) is None
    assert all(h.step(on(220), 2300 + t * 60) is None for t in range(5))
    # an outage (0 V) is not a flip and doesn't pause anything
    k = SagGuard(Path(str(path) + "3"))
    assert all(k.step(r, 3000 + i * 60) is None for i, r in enumerate([on(220), off(0), on(220), off(0), on(220)]))


def _demo_settings() -> None:
    class Device:
        battery_charge_limit_min, battery_charge_limit_max, battery_level = 5, 100, 100
        sent = []

        async def set_battery_charge_limit_min(self, v):
            if v > self.battery_charge_limit_max:
                return False
            self.sent.append(("min", v)); self.battery_charge_limit_min = v

        async def set_battery_charge_limit_max(self, v):
            if v < self.battery_charge_limit_min:
                return False
            self.sent.append(("max", v)); self.battery_charge_limit_max = v

        async def set_ac_charging_speed(self, v):
            self.sent.append(("ac", v)); return True

    assert pending() is None
    REQUEST.write_text(json.dumps({"id": "a", "battery_charge_limit_min": 0, "ac_charging_speed": 600}))
    assert pending()["id"] == "a"
    d = Device()
    assert asyncio.run(apply(d, pending()))["ok"] and d.sent == [("min", 0), ("ac", 600)]
    assert pending() is None  # answered
    REQUEST.write_text(json.dumps({"id": "old", "at": time.time() - 3600, "ac_charging_speed": 600}))
    assert pending() is None  # an hour old: never sent
    # new limit 15 is below the current cut-off 20: the cut-off has to go down first
    d.sent.clear(); d.battery_charge_limit_min = 20
    REQUEST.write_text(json.dumps({"id": "b", "battery_charge_limit_min": 10, "battery_charge_limit_max": 15}))
    assert asyncio.run(apply(d, pending()))["ok"] and d.sent == [("min", 10), ("max", 15)], d.sent
    d.sent.clear()
    REQUEST.write_text(json.dumps({"id": "c", "battery_charge_limit_min": 50, "battery_charge_limit_max": 100}))
    assert asyncio.run(apply(d, pending()))["ok"] and d.sent == [("max", 100), ("min", 50)], d.sent
    REQUEST.write_text(json.dumps({"id": "d", "battery_charge_limit_min": 99, "battery_charge_limit_max": 60}))
    out = asyncio.run(apply(d, pending()))
    assert not out["ok"] and "refused" in out["error"], out
    d.battery_level = 9.5  # the battery dropped below the requested cut-off after the page accepted it
    REQUEST.write_text(json.dumps({"id": "e", "battery_charge_limit_min": 10}))
    out = asyncio.run(apply(d, pending()))
    assert not out["ok"] and "not below the battery" in out["error"] and ("min", 10) not in d.sent[-1:], out


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


async def main() -> None:
    # systemd stops the service with SIGTERM. Cancelling instead of dying lets the session
    # close its link; a link left open would keep the station from being found again.
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    DB.parent.mkdir(parents=True, exist_ok=True)
    await Collector(open_db(DB)).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except asyncio.CancelledError:
        pass
