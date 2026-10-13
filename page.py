"""Serve the station page: latest values, history for the charts, CSV export.

Reads the collector's SQLite file and nothing else; it never opens .env.

    python3 page.py [port] [host]
"""

import csv
import io
import json
import sqlite3
import sys
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
DB = Path("/home/pi/data/ecoflow/ecoflow.db")
STATIC = {
    "/": ("page/index.html", "text/html; charset=utf-8"),
    "/chart.js": ("page/chart.umd.min.js", "text/javascript"),
}
CHARTED = ("battery_level", "input_power", "output_power", "ac_input_voltage", "battery_voltage", "cell_temperature")
POINTS = 720  # most points a chart gets; a longer range is averaged into buckets
MAX_HOURS = 24 * 366


def latest(db: sqlite3.Connection) -> dict:
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
    return dict(row) if row else {}


def history(db: sqlite3.Connection, hours: float, now: int) -> dict:
    bucket = max(60, int(hours * 3600 / POINTS))
    have = {row[1] for row in db.execute("PRAGMA table_info(readings)")}
    columns = [name for name in CHARTED if name in have]
    # grid voltage takes the lowest reading in a bucket, so a short outage survives the averaging
    picks = ", ".join(
        f"MIN({name})" if name == "ac_input_voltage" else f"ROUND(AVG({name}), 2)" for name in columns
    )
    rows = db.execute(
        f"SELECT (ts / ?) * ? AS t, {picks} FROM readings WHERE ts >= ? GROUP BY t ORDER BY t",
        (bucket, bucket, now - int(hours * 3600)),
    )
    return {"bucket": bucket, "columns": ["ts", *columns], "rows": [list(row) for row in rows]}


def as_csv(db: sqlite3.Connection, hours: float, now: int) -> str:
    rows = db.execute("SELECT * FROM readings WHERE ts >= ? ORDER BY ts", (now - int(hours * 3600),))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(column[0] for column in rows.description)
    writer.writerows(rows)
    return out.getvalue()


class Page(BaseHTTPRequestHandler):
    def reply(self, body: bytes, kind: str, filename: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path in STATIC:
            name, kind = STATIC[url.path]
            return self.reply((HERE / name).read_bytes(), kind)
        try:
            hours = float(parse_qs(url.query).get("hours", ["24"])[0])
        except ValueError:
            hours = 0
        if not 0 < hours <= MAX_HOURS:  # also rejects nan and inf
            return self.send_error(400, "hours must be between 0 and 8784")
        now = int(time.time())
        try:
            with closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True)) as db:
                if url.path == "/api/latest":
                    return self.reply(json.dumps(latest(db)).encode(), "application/json")
                if url.path == "/api/history":
                    return self.reply(json.dumps(history(db, hours, now)).encode(), "application/json")
                if url.path == "/api/readings.csv":
                    return self.reply(as_csv(db, hours, now).encode(), "text/csv", "ecoflow-readings.csv")
        except sqlite3.Error as e:
            return self.send_error(503, f"Database not readable: {e}")
        self.send_error(404)


def demo() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE readings (ts INTEGER PRIMARY KEY, battery_level, ac_input_voltage, note)")
    # three readings in one 2-hour bucket of a 60-day range, the middle one during an outage
    db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", [(7200, 80, 220, "a"), (7260, 70, 0, "b"), (7320, 60, 221, "c")])
    got = history(db, 24 * 60, now=7400)
    assert got == {"bucket": 7200, "columns": ["ts", "battery_level", "ac_input_voltage"], "rows": [[7200, 70.0, 0]]}, got
    assert history(db, 1, now=7400)["rows"] == [[7200, 80.0, 220], [7260, 70.0, 0], [7320, 60.0, 221]]
    assert as_csv(db, 1, now=7400).splitlines()[:2] == ["ts,battery_level,ac_input_voltage,note", "7200,80,220,a"]
    assert latest(db)["note"] == "c"


if __name__ == "__main__":
    demo()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8016
    host = sys.argv[2] if len(sys.argv) > 2 else "0.0.0.0"
    ThreadingHTTPServer((host, port), Page).serve_forever()
