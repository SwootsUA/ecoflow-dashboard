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
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, jsonify, make_response, redirect, render_template, request, session, url_for

DB = Path(os.environ.get("ECOFLOW_DB", "/home/pi/data/ecoflow/ecoflow.db"))
# Both sit on /run. The collector writes LIVE every second while it holds the Bluetooth
# link; touching VIEWER is how this process asks it to hold the link.
LIVE = Path(os.environ.get("ECOFLOW_LIVE", "/run/ecoflow-collector/live.json"))
VIEWER = Path(os.environ.get("ECOFLOW_VIEWER", "/run/ecoflow-page/viewer"))
LIVE_FRESH = 5  # seconds a published reading counts as live
OUTAGES = Path(os.environ.get("ECOFLOW_OUTAGES", "/home/pi/data/ecoflow/outages.json"))  # written by outages.py
# Station settings: this app can't use Bluetooth, so it leaves a request the collector applies over its link
# and answers in RESULT with the same id (see collector.py)
RELEASE = VIEWER.with_name("release")  # the collector frees Bluetooth for the phone app while this is fresh
RELEASE_FOR = 600
REQUEST = VIEWER.with_name("settings.json")
RESULT = LIVE.with_name("settings-result.json")
SETTING_RANGES = {"battery_charge_limit_min": (0, 30), "battery_charge_limit_max": (50, 100),  # DELTA 2's own ranges
                  "ac_charging_speed": (200, 1200)}
PORT = int(os.environ.get("PORT", 8016))
CHARTED = ("battery_level", "input_power", "output_power", "ac_input_voltage", "inverter_temperature",
           "battery_charge_limit_min")
POINTS = 720  # most points a chart gets; a longer range is averaged into buckets
MAX_HOURS = 24 * 366

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
from grid import on_grid, refused
from sso import init_sso  # one login for all *.swoots.dev apps, see sso.py
init_sso(app)
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


PC_WATTS = (200, 300)  # the load with the PC on since the i7 (22 Oct): ~150 W light use, 237 W avg on 9 Nov, peaks ~320 W


def drain_at(watts: float) -> float:
    """Battery %/h at this load: the fit on 49 outages, the same one static/app.js uses."""
    return 2.09 + 0.1043 * watts


def span_text(short: float, long: float) -> str:
    """A range of hours: minutes under an hour ("24–30 хв"), decimal hours from one hour ("1,2–1,7 год")."""
    if long < 1:
        return f"{round(short * 60)}–{round(long * 60)} хв"
    if short < 1:
        return f"{round(short * 60)} хв – {long:.1f} год".replace(".", ",")
    return f"{short:.1f}–{long:.1f} год".replace(".", ",")


def hours_text(hours: float) -> str:
    h, m = divmod(round(hours * 60), 60)
    if not h:
        return f"{m} хв"
    return f"{h} год {m} хв" if h < 10 and m else f"{h} год"


def possible_outages(outages: dict, now: datetime) -> list[str]:
    """Posted windows and outages at home that haven't ended, soonest first, as text."""
    found = set()
    for day, d in outages.get("days", {}).items():
        midnight = datetime.fromisoformat(day)
        for a, b in d.get("windows", []):
            found.add(tuple(midnight + timedelta(hours=int(t[:2]), minutes=int(t[3:])) for t in (a, b)))
    for e in outages.get("unplanned", []):
        found.add((datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["stop"])))
    days = {now.date(): "сьогодні", (now + timedelta(days=1)).date(): "завтра"}
    out = []
    for start, end in sorted(s for s in found if s[1] > now)[:4]:
        stop = "24:00" if end.time() == datetime.min.time() and end.date() > start.date() else f"{end:%H:%M}"
        out.append(f"{days.get(start.date(), f'{start:%d.%m}')} {start:%H:%M}–{stop}" + (" (зараз)" if start <= now else ""))
    return out


def newest() -> dict:
    """The collector's live reading while it is fresh (someone has the main page open), else the last stored row."""
    try:
        if time.time() - LIVE.stat().st_mtime < LIVE_FRESH:
            return json.loads(LIVE.read_text())
    except (OSError, ValueError):
        pass
    return read(latest)


def time_left(d: dict, outages: dict) -> tuple[float, float] | None:
    """Hours to the cut-off at the last 10 minutes' load, and that load: the one figure both pages show.
    The station reports 0 W below 18 W while the router and Pi still draw, so never faster than "PC off"."""
    level = d.get("battery_level")
    since = int(d.get("ts") or 0) - 600
    def average(db):
        try:
            return db.execute("SELECT AVG(output_power) FROM readings WHERE ts >= ?", (since,)).fetchone()[0]
        except sqlite3.OperationalError:  # no load column yet: a fresh database
            return None
    watts = read(average)
    if level is None or watts is None:
        return None
    above = max(0, level - (d.get("battery_charge_limit_min") or 0))
    return above / max(drain_at(watts), outages.get("drain", {}).get("PC off") or 0), watts


def load_outages() -> dict:
    try:
        return json.loads(OUTAGES.read_text())
    except (OSError, ValueError):
        return {}


@app.route("/live")
def family():
    """The public page for the family: grid, battery, how long it lasts, possible outages.
    No watts and no history: the load would show when someone is home."""
    d = newest()
    outages = load_outages()
    left = time_left(d, outages)
    level, floor = d.get("battery_level"), d.get("battery_charge_limit_min") or 0
    lasts = []
    if level is not None:
        above, (low, high) = max(0, level - floor), PC_WATTS
        span = span_text(above / drain_at(high), above / drain_at(low))
        off = outages.get("drain", {}).get("PC off")
        if left:
            lasts.append(("Зараз", hours_text(left[0]), "при поточному навантаженні"))
        lasts.append(("З комп'ютером", span, f"при {low}–{high} Вт"))
        if off:
            lasts.append(("Без комп'ютера", hours_text(above / off), "лише роутер"))
    ts = d.get("ts")
    response = make_response(render_template(
        "live.html",
        grid_on=on_grid(d),
        refused_v=round(d["ac_input_voltage"]) if refused(d) else None,
        battery=None if level is None else round(level),
        floor=round(floor),
        lasts=lasts,
        slots=possible_outages(outages, datetime.now()),
        updated=f"{datetime.fromtimestamp(ts):%H:%M}" if ts else None,
        stale=not ts or time.time() - ts > 180,
    ))
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


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
    fine = int(hours * 3600 / POINTS)
    if fine < 60:  # 1 h and 6 h views: per-second detail from the change log, when it reaches back far enough
        got = fine_history(db, columns, now - int(hours * 3600), now, max(1, fine))
        if got:
            return got
    # grid voltage takes the lowest reading in a bucket, so a short outage survives the averaging;
    # the cut-off too, so a change lands as one step instead of an averaged in-between value
    picks = ", ".join(
        f"MIN({name})" if name in MIN_COLUMNS else f"ROUND(AVG({name}), 2)"
        for name in columns
    )
    rows = db.execute(
        f"SELECT (ts / ?) * ? AS t, {picks} FROM readings WHERE ts >= ? GROUP BY t ORDER BY t",
        (bucket, bucket, now - int(hours * 3600)),
    )
    return {"bucket": bucket, "columns": ["ts", *columns], "rows": [list(row) for row in rows]}


MIN_COLUMNS = ("ac_input_voltage", "battery_charge_limit_min")  # a short dip survives, a change lands as one step


def step_buckets(start, events, since: int, until: int, bucket: int, low: bool) -> dict:
    """A value that holds between changes, averaged over time (or its lowest) per bucket."""
    out, current, t = {}, start, since
    for ts, value in [*events, (until, None)]:
        while t < ts:
            b = t // bucket * bucket
            end = min(ts, b + bucket)
            if current is not None:
                acc = out.setdefault(b, [0.0, 0, current])
                acc[0] += current * (end - t)
                acc[1] += end - t
                acc[2] = min(acc[2], current)
            t = end
        if value is not None:
            current = value
    return {b: (a[2] if low else round(a[0] / a[1], 2)) for b, a in out.items() if a[1]}


def fine_history(db: sqlite3.Connection, columns: list, since: int, until: int, bucket: int) -> dict | None:
    """Buckets under a minute, rebuilt from the per-second change log; None while the log doesn't reach back."""
    try:
        first = db.execute("SELECT MIN(ts) FROM changes").fetchone()[0]
        ids = dict(db.execute(f"SELECT name, id FROM fields WHERE name IN ({','.join('?' * len(columns))})", columns))
    except sqlite3.OperationalError:  # no change log yet
        return None
    if first is None or first > since:
        return None
    series = {}
    for name in columns:
        if name not in ids:
            continue
        start = db.execute("SELECT value FROM changes WHERE field = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                           (ids[name], since)).fetchone()
        events = db.execute("SELECT ts, value FROM changes WHERE field = ? AND ts > ? AND ts <= ? ORDER BY ts",
                            (ids[name], since, until)).fetchall()
        series[name] = step_buckets(start[0] if start else None, events, since, until, bucket, name in MIN_COLUMNS)
    # the minute rows are the heartbeat: a bucket with no row within 90 s was a stretch without the collector
    beats = [row[0] for row in db.execute("SELECT ts FROM readings WHERE ts > ? AND ts <= ? ORDER BY ts", (since - 90, until + 90))]
    rows, i = [], 0
    for b in range(since // bucket * bucket, until + 1, bucket):
        while i < len(beats) and beats[i] < b - 90:
            i += 1
        if i < len(beats) and beats[i] <= b + bucket + 90:
            rows.append([b, *[series.get(name, {}).get(b) for name in columns]])
    return {"bucket": bucket, "columns": ["ts", *columns], "rows": rows}


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
    except OSError:
        pass
    try:
        live = time.time() - LIVE.stat().st_mtime < LIVE_FRESH
    except OSError:
        live = False  # no collector running, or nothing published yet: the last stored reading will do
    d = newest()
    left = time_left(d, load_outages())
    extra = {"hours_left": left[0], "load_10min": round(left[1])} if left else {}
    return jsonify(d | {"live": live} | extra)


@app.route("/api/history")
@require_login
def api_history():
    hours = hours_asked()
    return jsonify(read(lambda db: history(db, hours, int(time.time()))))


@app.route("/api/outages")
@require_login
def api_outages():
    """The queue's possible outage windows and the battery drain rates, as outages.py last wrote them."""
    try:
        return jsonify(json.loads(OUTAGES.read_text()))
    except (OSError, ValueError):
        return jsonify({})


def battery_now() -> float | None:
    """The battery level from the collector's newest reading, else the last stored row."""
    try:
        return float(json.loads(LIVE.read_text())["battery_level"])
    except (OSError, ValueError, KeyError, TypeError):
        level = read(latest).get("battery_level")
        return None if level is None else float(level)


@app.route("/api/settings", methods=["GET", "POST"])
@require_login
def api_settings():
    """POST: queue new station settings for the collector. GET: the last request and the collector's answer."""
    if request.method == "GET":
        def load(path: Path):
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                return None
        return jsonify({"request": load(REQUEST), "result": load(RESULT)})
    # JSON only: another site can't send that without a CORS preflight, which this app never answers
    body = request.get_json(silent=True) if request.is_json else None
    if not isinstance(body, dict):
        return jsonify({"error": "JSON body expected"}), 400
    wanted = {}
    for name, (low, high) in SETTING_RANGES.items():
        if name in body:
            value = body[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                return jsonify({"error": f"{name} must be {low}–{high}"}), 400
            wanted[name] = int(value)
    if not wanted:
        return jsonify({"error": "nothing to change"}), 400
    if wanted.get("battery_charge_limit_min", 0) > wanted.get("battery_charge_limit_max", 100):
        return jsonify({"error": "cut-off above the charge limit"}), 400
    if "battery_charge_limit_min" in wanted:
        # a cut-off at or above the charge left switches the outputs off at once: PC, router, this Pi
        level = battery_now()
        if level is None or wanted["battery_charge_limit_min"] >= level:
            return jsonify({"error": f"cut-off must stay below the battery ({'unknown' if level is None else f'{level:.0f}%'})"}), 400
    queued = {"id": os.urandom(8).hex(), "at": time.time()} | wanted
    swap = REQUEST.with_suffix(".tmp")
    swap.write_text(json.dumps(queued))
    swap.replace(REQUEST)
    VIEWER.touch()  # the collector holds its link while this is fresh, so it applies within a second or two
    return jsonify(queued)


@app.route("/api/release", methods=["GET", "POST"])
@require_login
def api_release():
    """POST {"on": true}: free the station's Bluetooth for the phone app for 10 minutes; {"on": false}: take it back."""
    if request.method == "POST":
        body = request.get_json(silent=True) if request.is_json else None
        if not isinstance(body, dict) or not isinstance(body.get("on"), bool):
            return jsonify({"error": "JSON {\"on\": true|false} expected"}), 400
        if body["on"]:
            RELEASE.touch()
            os.utime(RELEASE)  # touch() leaves an existing file's time alone on some systems
        else:
            RELEASE.unlink(missing_ok=True)
    try:
        until = RELEASE.stat().st_mtime + RELEASE_FOR
    except OSError:
        until = None
    return jsonify({"until": until if until and until > time.time() else None})


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
    # per-second history: 1 h view (5 s buckets) from the change log, gaps where the collector was off
    assert step_buckets(10.0, [(105, 20.0)], 100, 115, 5, False) == {100: 10.0, 105: 20.0, 110: 20.0}
    assert step_buckets(10.0, [(102, 20.0)], 100, 105, 5, False) == {100: 16.0}  # 2 s at 10, 3 s at 20
    assert step_buckets(220.0, [(102, 0.0), (103, 221.0)], 100, 105, 5, True) == {100: 0.0}  # the dip survives
    fine = sqlite3.connect(":memory:")
    fine.execute("CREATE TABLE readings (ts INTEGER PRIMARY KEY, battery_level)")
    fine.executemany("INSERT INTO readings VALUES (?, 50)", [(t,) for t in range(0, 3601, 60) if not 1800 <= t <= 2400])
    assert fine_history(fine, ["battery_level"], 0, 3600, 5) is None  # no change log yet
    fine.execute("CREATE TABLE fields (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL)")
    fine.execute("CREATE TABLE changes (field INTEGER NOT NULL, ts INTEGER NOT NULL, value REAL, PRIMARY KEY (field, ts)) WITHOUT ROWID")
    fine.execute("INSERT INTO fields VALUES (1, 'battery_level')")
    fine.executemany("INSERT INTO changes VALUES (1, ?, ?)", [(0, 50.0), (1000, 49.0)])
    got = fine_history(fine, ["battery_level"], 0, 3600, 5)
    by = {row[0]: row[1] for row in got["rows"]}
    assert got["bucket"] == 5 and by[995] == 50.0 and by[1000] == 49.0 and by[3595] == 49.0, got["rows"][:3]
    assert 2100 not in by and 1700 in by  # the collector was off 1800-2400: no buckets there
    assert fine_history(fine, ["battery_level"], -100, 3600, 5) is None  # asks for more than the log holds
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
    global DB, LIVE, VIEWER, OUTAGES, REQUEST, RESULT, RELEASE
    real = DB, LIVE, VIEWER, OUTAGES, REQUEST, RESULT, RELEASE
    with tempfile.TemporaryDirectory() as tmp:
        DB, LIVE, VIEWER, OUTAGES = Path(tmp, "db"), Path(tmp, "live.json"), Path(tmp, "viewer"), Path(tmp, "outages.json")
        REQUEST, RESULT, RELEASE = Path(tmp, "settings.json"), Path(tmp, "settings-result.json"), Path(tmp, "release")
        assert client.get("/api/release").json == {"until": None}
        assert client.post("/api/release", json={"on": "yes"}).status_code == 400
        assert client.post("/api/release", json={"on": True}).json["until"] > time.time() + RELEASE_FOR - 5
        assert client.post("/api/release", json={"on": False}).json == {"until": None} and not RELEASE.exists()
        assert client.__class__(app).post("/api/release", json={"on": True}).status_code == 401
        LIVE.write_text(json.dumps({"ts": 1, "battery_level": 12.5}))
        assert client.get("/api/settings").json == {"request": None, "result": None}
        assert "below the battery (12%)" in client.post("/api/settings", json={"battery_charge_limit_min": 13}).json["error"]
        assert client.post("/api/settings", json={"battery_charge_limit_min": 12}).status_code == 200
        assert client.post("/api/settings", data="battery_charge_limit_min=0").status_code == 400  # a plain form: no
        for bad in ({"battery_charge_limit_min": 31}, {"ac_charging_speed": 100}, {"battery_charge_limit_min": True}, {},
                    {"battery_charge_limit_min": 30, "battery_charge_limit_max": 20}):
            assert client.post("/api/settings", json=bad).status_code == 400, bad
        queued = client.post("/api/settings", json={"battery_charge_limit_min": 0, "ac_charging_speed": 600, "x": 1}).json
        assert set(queued) == {"id", "at", "battery_charge_limit_min", "ac_charging_speed"} and VIEWER.exists()
        assert client.get("/api/settings").json["request"] == queued
        assert client.__class__(app).post("/api/settings", json={"ac_charging_speed": 600}).status_code == 401
        LIVE.unlink()
        assert client.get("/api/outages").json == {}  # outages.py hasn't run yet
        OUTAGES.write_text(json.dumps({"queue": "2.1", "days": {}}))
        assert client.get("/api/outages").json == {"queue": "2.1", "days": {}}
        with closing(sqlite3.connect(DB)) as stored:
            stored.execute("CREATE TABLE readings (ts INTEGER PRIMARY KEY, battery_level, raw)")
            stored.execute("INSERT INTO readings VALUES (100, 40, \"{}\")")
            stored.commit()
        assert client.get("/api/live").json == {"ts": 100, "battery_level": 40, "live": False} and VIEWER.exists()
        LIVE.write_text(json.dumps({"ts": 200.5, "battery_level": 41}))
        assert client.get("/api/live").json == {"ts": 200.5, "battery_level": 41, "live": True}
        os.utime(LIVE, (time.time() - 60, time.time() - 60))
        assert client.get("/api/live").json["live"] is False

        # the family page needs no login and shows no watts
        with closing(sqlite3.connect(DB)) as stored:
            stored.execute("DROP TABLE readings")
            stored.execute("CREATE TABLE readings (ts, battery_level, battery_charge_limit_min, ac_input_voltage, ac_input_power, output_power, raw)")
            stored.execute("INSERT INTO readings VALUES (?, 55, 15, 0, 0, 171, '{}')", (int(time.time()),))
            stored.commit()
        OUTAGES.write_text(json.dumps({"days": {}, "drain": {"PC on": 20, "PC off": 2.8}}))
        page = client.__class__(app).get("/live")
        text = page.data.decode()
        assert page.status_code == 200 and page.headers["X-Robots-Tag"] == "noindex, nofollow", page.status_code
        assert "Немає" in text and "55%" in text and "14 год 17 хв" not in text and "14 год" in text, text
        assert "171" not in text and "не заплановані" in text and "1,2–1,7 год" in text and "200–300 Вт" in text, text
        assert "Зараз</h2><div class=\"stat-value\">2 год" in text, text  # 171 W -> 19.9 %/h, 40 % above the floor
        # the main page shows the same figure, from /api/live
        live = client.get("/api/live").json
        assert live["load_10min"] == 171 and abs(live["hours_left"] - 40 / drain_at(171)) < 1e-9, live
        assert hours_text(live["hours_left"]) in text
    DB, LIVE, VIEWER, OUTAGES, REQUEST, RESULT, RELEASE = real
    now = datetime(2026, 11, 4, 13, 0)
    got = possible_outages({"days": {"2026-11-04": {"windows": [["06:00", "10:00"], ["12:00", "16:00"], ["18:00", "24:00"]]},
                                     "2026-11-05": {"windows": [["00:00", "02:00"]]}}}, now)
    assert got == ["сьогодні 12:00–16:00 (зараз)", "сьогодні 18:00–24:00", "завтра 00:00–02:00"], got
    assert span_text(0.4, 0.5) == "24–30 хв" and span_text(0.75, 1.2) == "45 хв – 1,2 год" and span_text(1.2, 1.74) == "1,2–1,7 год"
    assert hours_text(2.0) == "2 год" and hours_text(2.25) == "2 год 15 хв" and hours_text(14.3) == "14 год" and hours_text(0.5) == "30 хв"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, threaded=True)
