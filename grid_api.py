"""Grid state for machines on the home network: GET /grid.

The PC asks this every few seconds and switches to its outage profile while the grid is off.
Read-only and LAN-only (not tunneled): it serves the collector's latest reading and nothing else.

    {"grid": false, "battery": 87.4, "output_w": 172, "age_s": 23, "ts": 1792...}

"grid" is null when the newest reading is older than STALE_AFTER: the collector missed reads, so
nobody should act on it. A reading comes once a minute, or once a second while power.swoots.dev is open.

    python3 grid_api.py         # serve on 0.0.0.0:8017
    python3 grid_api.py demo    # self-check
"""

import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB = Path(os.environ.get("ECOFLOW_DB", "/home/pi/data/ecoflow/ecoflow.db"))
LIVE = Path(os.environ.get("ECOFLOW_LIVE", "/run/ecoflow-collector/live.json"))
PORT = int(os.environ.get("GRID_PORT", 8017))
STALE_AFTER = 180  # seconds; the same line the page uses for "No reading since"


def latest() -> dict:
    """The newest reading: the live one while the collector holds the link, else the last stored row."""
    try:
        live = json.loads(LIVE.read_text())
        if time.time() - live.get("ts", 0) < 5:
            return live
    except (OSError, ValueError):
        pass
    with closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT ts, battery_level, output_power, ac_input_voltage, ac_input_power "
                         "FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
    return dict(row) if row else {}


def state(reading: dict, now: float) -> dict:
    if not reading.get("ts"):
        return {"grid": None, "error": "no readings"}
    age = round(now - reading["ts"])
    grid = (reading.get("ac_input_voltage") or 0) > 50 or (reading.get("ac_input_power") or 0) > 0
    return {"grid": grid if age <= STALE_AFTER else None, "battery": reading.get("battery_level"),
            "output_w": reading.get("output_power"), "age_s": age, "ts": reading["ts"]}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/grid":
            self.send_error(404)
            return
        try:
            body, code = state(latest(), time.time()), 200
        except sqlite3.Error as e:
            body, code = {"grid": None, "error": str(e)}, 503
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass  # a PC asking every few seconds would bury the journal


def demo() -> None:
    on = {"ts": 1000, "battery_level": 90, "output_power": 150, "ac_input_voltage": 228, "ac_input_power": 0}
    assert state(on, 1010) == {"grid": True, "battery": 90, "output_w": 150, "age_s": 10, "ts": 1000}
    off = dict(on, ac_input_voltage=0)
    assert state(off, 1010)["grid"] is False
    # charging with the inverter's voltage reading at 0 still counts as grid on
    assert state(dict(off, ac_input_power=700), 1010)["grid"] is True
    assert state(off, 1000 + STALE_AFTER + 1)["grid"] is None  # stale: don't act on it
    assert state({}, 0) == {"grid": None, "error": "no readings"}
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["demo"]:
        demo()
    else:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
