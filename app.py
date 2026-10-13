"""The station page: login, latest values, history for the charts, CSV export.

Reads the collector's SQLite file and nothing else. SECRET_KEY and PASSWORD_HASH come
from .env.web; this process never sees .env, where the Bluetooth secret lives.

    ~/venvs/ecoflow/bin/python app.py
"""

import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for

DB = Path(os.environ.get("ECOFLOW_DB", "/home/pi/data/ecoflow/ecoflow.db"))
# Both sit on /run. The collector writes LIVE every second while it holds the Bluetooth
# link; touching VIEWER is how this process asks it to hold the link.
LIVE = Path(os.environ.get("ECOFLOW_LIVE", "/run/ecoflow-collector/live.json"))
VIEWER = Path(os.environ.get("ECOFLOW_VIEWER", "/run/ecoflow-page/viewer"))
LIVE_FRESH = 5  # seconds a published reading counts as live
PORT = int(os.environ.get("PORT", 8016))
CHARTED = ("battery_level", "input_power", "output_power", "ac_input_voltage", "inverter_temperature")
POINTS = 720  # most points a chart gets; a longer range is averaged into buckets
MAX_HOURS = 24 * 366

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.permanent_session_lifetime = timedelta(days=3650)
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax")
# one request a second from every open tab would bury the journal
logging.getLogger("werkzeug").addFilter(lambda record: "/api/live" not in record.getMessage())

# ponytail: one counter for every client; count per CF-Connecting-IP if lockouts ever bite
wrong_passwords: list[float] = []


@app.after_request
def no_store(response):
    # Cloudflare caches by file extension, .csv included: say plainly that nothing behind the login is cacheable
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.context_processor
def asset_urls():
    # Cloudflare and browsers keep static files for hours; the file time in the URL makes an edit show at once
    def asset(name: str) -> str:
        return url_for("static", filename=name, v=int((Path(app.static_folder) / name).stat().st_mtime))

    return {"asset": asset}


def check_password(password: str) -> bool:
    """Verify against the pbkdf2_sha256$iterations$salt$hash string shared by the other apps."""
    parts = os.environ.get("PASSWORD_HASH", "").split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), parts[2].encode(), int(parts[1]))
    return hmac.compare_digest(base64.b64encode(derived).decode(), parts[3])


def require_login(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if session.get("logged_in"):
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("login"))

    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        now = time.monotonic()
        wrong_passwords[:] = [at for at in wrong_passwords if now - at < 60]
        if len(wrong_passwords) >= 5:
            return render_template("login.html", error="Too many attempts, wait a minute"), 429
        if check_password(request.form.get("password", "")):
            session.permanent = True
            session["logged_in"] = True
            return redirect(url_for("index"))
        wrong_passwords.append(now)
        error = "Wrong password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@require_login
def index():
    return render_template("index.html")


def latest(db: sqlite3.Connection) -> dict:
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
    values = dict(row) if row else {}
    values.pop("raw", None)  # the packet dump belongs in the CSV, not on the page
    return values


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


def hours_asked() -> float:
    try:
        hours = float(request.args.get("hours", 24))
    except ValueError:
        hours = 0
    if not 0 < hours <= MAX_HOURS:  # also rejects nan and inf
        abort(400, "hours must be between 0 and 8784")
    return hours


def read(query):
    """Run query(db) on a read-only connection; a missing or locked file is a 503, not a crash."""
    try:
        with closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True)) as db:
            return query(db)
    except sqlite3.Error as e:
        abort(503, f"Database not readable: {e}")


@app.route("/api/live")
@require_login
def api_live():
    """What a visible tab asks for every second. Asking is also the signal: the collector
    holds its Bluetooth link to the station for as long as these requests keep coming."""
    try:
        VIEWER.touch()
        if time.time() - LIVE.stat().st_mtime < LIVE_FRESH:
            return jsonify(json.loads(LIVE.read_text()) | {"live": True})
    except (OSError, ValueError):
        pass  # no collector running, or nothing published yet: the last stored reading will do
    return jsonify(read(latest) | {"live": False})


@app.route("/api/history")
@require_login
def api_history():
    hours = hours_asked()
    return jsonify(read(lambda db: history(db, hours, int(time.time()))))


@app.route("/api/readings.csv")
@require_login
def api_csv():
    hours = hours_asked()
    body = read(lambda db: as_csv(db, hours, int(time.time())))
    return Response(body, mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="ecoflow-readings.csv"'})


def demo() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE readings (ts INTEGER PRIMARY KEY, battery_level, ac_input_voltage, note)")
    # three readings in one 2-hour bucket of a 60-day range, the middle one during an outage
    db.executemany("INSERT INTO readings VALUES (?, ?, ?, ?)", [(7200, 80, 220, "a"), (7260, 70, 0, "b"), (7320, 60, 221, "c")])
    got = history(db, 24 * 60, now=7400)
    assert got == {"bucket": 7200, "columns": ["ts", "battery_level", "ac_input_voltage"], "rows": [[7200, 70.0, 0]]}, got
    assert history(db, 1, now=7400)["rows"] == [[7200, 80.0, 220], [7260, 70.0, 0], [7320, 60.0, 221]]
    assert as_csv(db, 1, now=7400).splitlines()[:2] == ["ts,battery_level,ac_input_voltage,note", "7200,80,220,a"]
    db.execute("ALTER TABLE readings ADD COLUMN raw DEFAULT '{}'")
    assert latest(db)["note"] == "c" and "raw" not in latest(db)

    os.environ["PASSWORD_HASH"] = "pbkdf2_sha256$1000$salt$" + base64.b64encode(
        hashlib.pbkdf2_hmac("sha256", b"right", b"salt", 1000)
    ).decode()
    assert check_password("right") and not check_password("wrong")
    client = app.test_client()
    assert client.get("/").status_code == 302 and client.get("/api/live").status_code == 401
    assert b"Wrong password" in client.post("/login", data={"password": "wrong"}).data
    assert client.post("/login", data={"password": "right"}).status_code == 302
    page = client.get("/")
    assert page.status_code == 200 and page.headers["Cache-Control"] == "no-store"
    assert b"/static/app.js?v=" in page.data
    wrong_passwords.extend([time.monotonic()] * 5)
    assert client.post("/login", data={"password": "right"}).status_code == 429
    wrong_passwords.clear()

    # live: a fresh published reading is served as live and the request leaves the viewer mark;
    # a stale one falls back to the last stored row
    global DB, LIVE, VIEWER
    real = DB, LIVE, VIEWER
    with tempfile.TemporaryDirectory() as tmp:
        DB, LIVE, VIEWER = Path(tmp, "db"), Path(tmp, "live.json"), Path(tmp, "viewer")
        with closing(sqlite3.connect(DB)) as stored:
            stored.execute("CREATE TABLE readings (ts INTEGER PRIMARY KEY, battery_level, raw)")
            stored.execute("INSERT INTO readings VALUES (100, 40, \"{}\")")
            stored.commit()
        assert client.get("/api/live").json == {"ts": 100, "battery_level": 40, "live": False} and VIEWER.exists()
        LIVE.write_text(json.dumps({"ts": 200.5, "battery_level": 41}))
        assert client.get("/api/live").json == {"ts": 200.5, "battery_level": 41, "live": True}
        os.utime(LIVE, (time.time() - 60, time.time() - 60))
        assert client.get("/api/live").json["live"] is False
    DB, LIVE, VIEWER = real


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, threaded=True)
