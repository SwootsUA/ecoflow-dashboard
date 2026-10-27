"""Possible outages for one queue, from Черкасиобленерго's daily schedule posts (ГПВ).

A post is a forecast, not a promise: on 13 Oct queue 2.1's 16:00-18:00 slot never came.
So this only ever says "possible"; what really happened is the grid voltage the collector logs.

Run by ecoflow-outages.timer every 10 minutes. Each run:
  - writes OUT with the queue's windows for every day from today that has a post (the page reads it)
  - sends Telegram, through tgbot's loopback send API, when a day's windows appear or change,
    and about half an hour before each window

    ~/venvs/ecoflow/bin/python outages.py         # one run
    ~/venvs/ecoflow/bin/python outages.py demo    # self-check, no network
"""

import datetime as dt
import html
import json
import os
import re
import sqlite3
import ssl
import sys
import tempfile
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path

QUEUE = os.environ.get("OUTAGE_QUEUE", "2.1")
POSTS = "https://www.cherkasyoblenergo.com/api/v1/posts"
OUT = Path(os.environ.get("ECOFLOW_OUTAGES", "/home/pi/data/ecoflow/outages.json"))
HISTORY = OUT.with_name("outages-history.jsonl")  # every version of every day's post, as first seen
# Outages heard of some other way (the landlord, a notice on the door), kept by hand:
# [{"kind": "Planned outage (landlord)", "start": "2026-10-22T08:00:00", "stop": "2026-10-22T17:00:00"}]
MANUAL = OUT.with_name("outages-manual.json")
DB = Path(os.environ.get("ECOFLOW_DB", "/home/pi/data/ecoflow/ecoflow.db"))
SEND = "http://127.0.0.1:8010/send"
CLOCK_SET = Path("/run/systemd/timesync/synchronized")  # no RTC: after an outage the Pi boots in the past
WARN_BEFORE = dt.timedelta(minutes=30)
# Battery % used per hour on battery, measured over the 12 outages of 13-19 Oct: 150-200 W (the PC in use)
# and below 18 W (PC off, router and Pi). The page reads these from OUT too.
DRAIN = {"PC on": 21, "PC off": 2.6}

# Emergency and planned-works outages, from the oblenergo's whole-city list (the same data as its
# "check by address" page). Only the district goes out in the request; the building is matched here.
# Off until OUTAGE_STREET and OUTAGE_HOUSE are set, written as the oblenergo writes them ("Ю. Іллєнка", "4").
CABINET = "https://cabinet.cherkasyoblenergo.com/api_new/disconn.php"
STREET = os.environ.get("OUTAGE_STREET", "")
HOUSE = os.environ.get("OUTAGE_HOUSE", "")
KINDS = {"0": "Planned works", "1": "Emergency outage"}
# Their server leaves Sectigo's intermediate out of its chain; the repo carries that public cert so
# the connection is still verified, not skipped.
TLS = ssl.create_default_context()
TLS.load_verify_locations(Path(__file__).with_name("certs") / "sectigo-dv-r36.pem")
MONTHS = ("січня", "лютого", "березня", "квітня", "травня", "червня",
          "липня", "серпня", "вересня", "жовтня", "листопада", "грудня")


def get(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (swoots ecoflow page)"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["data"]


def post_day(title: str, published: str) -> str | None:
    """The day a schedule post is for ("Оновлено графік ... (ГПВ) на 14 жовтня"), or None."""
    found = re.search(r"ГПВ\)? на (\d{1,2}) (" + "|".join(MONTHS) + ")", title)
    if not found:
        return None
    month = MONTHS.index(found.group(2)) + 1
    year = int(published[:4]) + (month < int(published[5:7]))  # posted 31 Dec for 1 Jan
    return dt.date(year, month, int(found.group(1))).isoformat()


def windows(content: str, queue: str) -> list[list[str]]:
    """[["10:00", "12:00"], ...] for one queue from a post's HTML; [] when the queue isn't listed.

    The HTML splits times across tags ("10<span>:00 - 12:00</span>"), so it goes to plain text first.
    """
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", content)).split())
    text = re.sub(r"(\d{1,2})\s*:\s*(\d{2})", r"\1:\2", text)
    span = r"\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}"
    for name, body in re.findall(rf"(\d\.\d)\s+((?:{span}[\s,;]*)+)", text):
        if name == queue:
            return [list(pair) for pair in re.findall(r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})", body)]
    return []


def fetch(today: str, queue: str = QUEUE) -> dict:
    """{day: {"windows", "title", "published", "versions"}} for each day from today on.

    The newest post is the one that counts ("Оновлено ..." replaces the morning's), so its windows lead;
    "versions" keeps every post for the day, newest first, because a slot an update drops can still come
    (15 Oct: 08-10 was taken out of the update, and the outage came at 08:04 anyway).
    """
    days = {}
    for post in sorted(get(f"{POSTS}/main?lang=uk&limit=50"), key=lambda p: p["publishedAt"], reverse=True):
        day = post_day(post["title"], post["publishedAt"])
        if day and day >= today:
            body = get(f"{POSTS}/{post['slug']}?lang=uk")["content"]
            version = {"windows": windows(body, queue), "title": post["title"], "published": post["publishedAt"]}
            days.setdefault(day, dict(version, versions=[]))["versions"].append(version)
    return days


def record(days: dict) -> int:
    """Append the versions not in HISTORY yet; returns how many were new."""
    try:
        seen = {(r["day"], r["published"]) for r in map(json.loads, HISTORY.read_text().splitlines())}
    except OSError:
        seen = set()
    new = [dict(v, day=day) for day, info in sorted(days.items()) for v in reversed(info["versions"])
           if (day, v["published"]) not in seen]
    with HISTORY.open("a") as fh:
        for row in new:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(new)


def at_building(address: str, street: str = STREET, house: str = HOUSE) -> bool:
    """'<strong>м. Черкаси</strong><br>вул. Ю. Іллєнка 4' against the street and house number."""
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", address)).split()).lower()
    i = text.find(street.lower())
    if not street or i < 0:
        return False
    # only the numbers right after the street: one row can go on to another street ("... 305 вул. Припортова 43")
    for token in re.split(r"[\s,;]+", text[i + len(street):].strip()):
        if not re.fullmatch(r"\d+[\w/-]*", token):
            return False
        if token == house.lower():
            return True
    return False


def stamp(text: str) -> str:
    """'17:00 16.10.2026 (план)' -> '2026-10-16T17:00:00'."""
    return dt.datetime.strptime(text.split("(")[0].strip(), "%H:%M %d.%m.%Y").isoformat()


def unplanned(now: dt.datetime) -> list[dict]:
    """Emergency and planned-works outages at the building, today and tomorrow."""
    if not (STREET and HOUSE):
        return []
    found = []
    for selector, kind in KINDS.items():
        query = {"op": "disconn_by_dept", "dept_id": "1", "disconn_selector": selector,  # 1 = Cherkasy city
                 "n_date": f"{now:%d.%m.%Y}", "k_date": f"{now + dt.timedelta(days=1):%d.%m.%Y}"}
        request = urllib.request.Request(f"{CABINET}?{urllib.parse.urlencode(query)}",
                                         headers={"User-Agent": "Mozilla/5.0 (swoots ecoflow page)"})
        with urllib.request.urlopen(request, timeout=60, context=TLS) as response:
            rows = json.loads(response.read().decode("utf-8", "ignore")).get("DISCONNECTIONS") or []
        for row in rows if isinstance(rows, list) else rows.values():
            if at_building(row.get("ADDRESS", "")):
                found.append({"kind": kind, "start": stamp(row["DATE_START"]), "stop": stamp(row["DATE_STOP"]),
                              "planned_stop": "план" in row["DATE_STOP"], "over": row.get("STATE_INT") == "-1"})
    unique = {(e["kind"], e["start"], e["stop"]): e for e in found}  # one outage can be listed once per line
    return sorted(unique.values(), key=lambda e: e["start"])


def spans(days: dict) -> list[tuple[dt.datetime, dt.datetime, bool]]:
    """Every window as (start, end, dropped), soonest first; "24:00" is the next midnight.

    A slot an update took out still counts, marked dropped: in the week to 27 Oct nearly every dropped
    slot came anyway, 7 of that week's 19 outages, none of them warned.
    """
    def at(day, hhmm):
        hours, minutes = map(int, hhmm.split(":"))
        return dt.datetime.fromisoformat(day) + dt.timedelta(hours=hours, minutes=minutes)

    found = {}
    for day, info in days.items():
        for a, b in info["windows"]:
            found[at(day, a), at(day, b)] = False
        for version in info.get("versions", []):
            for a, b in version["windows"]:
                found.setdefault((at(day, a), at(day, b)), True)
    return sorted((start, end, dropped) for (start, end), dropped in found.items())


def lasts(hours: float) -> str:
    return f"{int(hours)}h{round(hours % 1 * 60):02d}" if hours < 20 else f"{round(hours)}h"


def short(hhmm: str) -> str:
    """'18:00' -> '18', '18:30' stays."""
    return hhmm[:-3] if hhmm.endswith(":00") else hhmm


def battery_line(battery: dict | None) -> str:
    if not battery:
        return ""
    above = max(0, battery["battery_level"] - battery["battery_charge_limit_min"])
    on, off = (above / DRAIN[name] for name in ("PC on", "PC off"))
    return f" · {battery['battery_level']:.0f}% lasts {lasts(on)} PC on, {lasts(off)} off"


def day_label(day: str, now: dt.datetime) -> str:
    offset = (dt.date.fromisoformat(day) - now.date()).days
    return {0: "Today", 1: "Tomorrow"}.get(offset, dt.date.fromisoformat(day).strftime("%d %b"))


def messages(old: dict, days: dict, now: dt.datetime, battery: dict | None) -> tuple[list[str], list[str]]:
    """The Telegram messages this run owes, and the window starts warned about (old ones dropped).

    He asked for them short (14 Oct): no queue, no "possible", hours without ":00".
    """
    out = []
    for day, info in sorted(days.items()):
        if info["windows"] == old.get("days", {}).get(day, {}).get("windows"):
            continue
        changed = ", changed" if day in old.get("days", {}) else ""
        slots = ", ".join(f"{short(a)}–{short(b)}" for a, b in info["windows"]) or "no outages"
        out.append(f"🔌 {day_label(day, now)}{changed}: {slots}")
    warned = [start for start in old.get("warned", []) if start >= (now - dt.timedelta(days=2)).isoformat()]
    for start, end, dropped in spans(days):
        if now < start <= now + WARN_BEFORE and start.isoformat() not in warned:
            until = "24" if end.time() == dt.time(0) else short(f"{end:%H:%M}")  # as the post writes midnight
            hours = f"{short(f'{start:%H:%M}')}–{until}"
            note = " (dropped in an update)" if dropped else ""
            out.append(f"⚡ {hours} in {round((start - now).total_seconds() / 60)} min{note}" + battery_line(battery))
            warned.append(start.isoformat())
    return out, warned


def manual(now: dt.datetime) -> list[dict]:
    """MANUAL's outages that haven't ended, in the same shape as unplanned()'s."""
    try:
        rows = json.loads(MANUAL.read_text())
    except (OSError, ValueError):
        return []
    return [{"kind": r["kind"], "start": r["start"], "stop": r["stop"], "planned_stop": True, "over": False}
            for r in rows if dt.datetime.fromisoformat(r["stop"]) > now]


def unplanned_messages(old: dict, events: list[dict], now: dt.datetime | None = None) -> list[str]:
    """One message per emergency or planned-works outage at the building, when first seen and not over yet."""
    known = {(e["kind"], e["start"]) for e in old.get("unplanned", [])}
    out = []
    for e in events:
        if (e["kind"], e["start"]) in known or e["over"]:
            continue
        start, stop = dt.datetime.fromisoformat(e["start"]), dt.datetime.fromisoformat(e["stop"])
        label = day_label(start.date().isoformat(), now) if now and start.date() != now.date() else ""
        day = f" {label.lower() if label == 'Tomorrow' else 'on ' + label}" if label else ""
        out.append(f"⚠️ {e['kind']} at home{day}: {start:%H:%M}–{stop:%H:%M}" + (" (planned end)" if e["planned_stop"] else ""))
    return out


def send(text: str) -> None:
    token = os.environ.get("NOTIFY_TOKEN")
    if not token:  # run by hand: show it instead
        print(text)
        return
    body = json.dumps({"text": text}).encode()
    request = urllib.request.Request(SEND, data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=15).close()


def latest_battery() -> dict | None:
    try:
        with closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True)) as db:
            row = db.execute("SELECT battery_level, battery_charge_limit_min FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return {"battery_level": row[0], "battery_charge_limit_min": row[1]} if row and None not in row else None


def save(state: dict) -> None:
    """Swapped in whole, so the page never reads half a file."""
    swap = OUT.with_suffix(".tmp")
    swap.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    swap.replace(OUT)


def run() -> None:
    if not CLOCK_SET.exists():
        print("clock not synchronised yet; skipping")  # not a failure: the next run will do
        return
    now = dt.datetime.now().replace(microsecond=0)
    try:
        old = json.loads(OUT.read_text())
    except (OSError, ValueError):
        old = {}
    days = fetch(now.date().isoformat())
    record(days)
    try:
        events = unplanned(now)
    except (OSError, ValueError) as e:  # the cabinet API is a second source; its outage must not stop the schedule
        print(f"unplanned outages: {e}")
        events = [e for e in old.get("unplanned", []) if e.get("kind") in KINDS.values()]
    events = sorted({(e["kind"], e["start"], e["stop"]): e for e in events + manual(now)}.values(), key=lambda e: e["start"])
    battery = latest_battery()
    texts, warned = messages(old, days, now, battery)
    for e in events:  # the same half-hour warning as for the posted slots
        start = dt.datetime.fromisoformat(e["start"])
        if not e["over"] and now < start <= now + WARN_BEFORE and e["start"] not in warned:
            texts.append(f"⚡ {e['kind']} in {round((start - now).total_seconds() / 60)} min" + battery_line(battery))
            warned.append(e["start"])
    for text in texts + unplanned_messages(old, events, now):
        send(text)  # a failed send raises before the save, so the next run tries again
    save({"queue": QUEUE, "checked": now.isoformat(), "days": days, "unplanned": events, "warned": warned,
          "drain": DRAIN})


def demo() -> None:
    post = ('<p>Години відсутності електропостачання:</p><p>1.1 10<span>:00 - 12:00,&nbsp;</span>'
            '<span>16</span><span>:00 - 18:00</span></p><p>2.1</p><p>10</p><p>:00 - 12:00,</p><p>18</p>'
            '<p>:00 - 24:00</p><p>2.2 06<span>:00 - 08:00</span></p>')
    assert windows(post, "2.1") == [["10:00", "12:00"], ["18:00", "24:00"]], windows(post, "2.1")
    assert windows(post, "1.1") == [["10:00", "12:00"], ["16:00", "18:00"]] and windows(post, "6.2") == []
    assert post_day("Оновлено графік погодинних відключень (ГПВ) на 12 жовтня", "2026-10-12T09:55:00") == "2026-10-12"
    assert post_day("Графік погодинних відключень (ГПВ) на 1 січня", "2026-12-31T20:00:00") == "2027-01-01"
    assert post_day("Для промисловості та бізнесу 14 жовтня діятимуть ГОП", "2026-10-13T19:45:00") is None

    days = {"2026-10-14": {"windows": [["10:00", "12:00"], ["18:00", "24:00"]]}}
    assert spans(days)[1] == (dt.datetime(2026, 10, 14, 18), dt.datetime(2026, 10, 15), False)
    # a slot the update dropped is still warned, marked
    updated = {"2026-10-14": {"windows": [["18:00", "24:00"]], "versions": [
        {"windows": [["18:00", "24:00"]]}, {"windows": [["08:00", "10:00"], ["18:00", "24:00"]]}]}}
    assert [s[2] for s in spans(updated)] == [True, False], spans(updated)
    texts, _ = messages({"days": updated}, updated, dt.datetime(2026, 10, 14, 7, 40), None)
    assert texts == ["⚡ 08–10 in 20 min (dropped in an update)"], texts
    battery = {"battery_level": 90.0, "battery_charge_limit_min": 15}
    # first sight of a day: one "posted" message; nothing to warn about at 15:00
    texts, warned = messages({}, days, dt.datetime(2026, 10, 14, 15), battery)
    assert texts == ["🔌 Today: 10–12, 18–24"] and warned == [], texts
    old = {"days": days, "warned": warned}
    # 17:40: the 18:00 window is 20 minutes away, warned once, with the battery estimate
    texts, warned = messages(old, days, dt.datetime(2026, 10, 14, 17, 40), battery)
    assert texts == ["⚡ 18–24 in 20 min · 90% lasts 3h34 PC on, 29h off"], texts
    assert messages({"days": days, "warned": warned}, days, dt.datetime(2026, 10, 14, 17, 50), battery) == ([], warned)
    # a changed schedule is announced again
    texts, _ = messages(old, {"2026-10-14": {"windows": [["18:00", "20:00"]]}}, dt.datetime(2026, 10, 14, 15), None)
    assert texts == ["🔌 Today, changed: 18–20"], texts
    assert short("18:30") == "18:30" and lasts(26.8) == "27h"

    assert at_building("<strong>м. Черкаси</strong><br>вул. Ю. Іллєнка 4", "Ю. Іллєнка", "4")
    assert not at_building("<strong>м. Черкаси</strong><br>вул. Ю. Іллєнка 46", "Ю. Іллєнка", "4")
    assert at_building("м. Черкаси вул. Ю. Іллєнка 2, 4, 6", "ю. іллєнка", "4")
    assert not at_building("вул. Ю. Іллєнка 4", "", "")
    assert not at_building("м. Черкаси б-р Т. Шевченка 305 вул. Припортова 43", "Т. Шевченка", "43")
    assert at_building("м. Черкаси б-р Т. Шевченка 305 вул. Припортова 43", "Припортова", "43")
    assert stamp("17:00 16.10.2026 (план)") == "2026-10-16T17:00:00"
    event = {"kind": "Emergency outage", "start": "2026-10-16T14:01:00", "stop": "2026-10-16T17:00:00",
             "planned_stop": True, "over": False}
    assert unplanned_messages({}, [event]) == ["⚠️ Emergency outage at home: 14:01–17:00 (planned end)"]
    assert unplanned_messages({"unplanned": [event]}, [event]) == []
    assert unplanned_messages({}, [dict(event, over=True)]) == []
    assert unplanned_messages({}, [event], dt.datetime(2026, 10, 14, 9)) == ["⚠️ Emergency outage at home on 16 Oct: 14:01–17:00 (planned end)"]

    global OUT, HISTORY
    with tempfile.TemporaryDirectory() as tmp:
        OUT, HISTORY = Path(tmp, "outages.json"), Path(tmp, "history.jsonl")
        global MANUAL
        MANUAL = Path(tmp, "manual.json")
        assert manual(dt.datetime(2026, 10, 20)) == []  # no file
        MANUAL.write_text(json.dumps([{"kind": "Planned outage (landlord)", "start": "2026-10-22T08:00:00", "stop": "2026-10-22T17:00:00"},
                                      {"kind": "Old", "start": "2026-10-01T08:00:00", "stop": "2026-10-01T10:00:00"}]))
        assert [e["kind"] for e in manual(dt.datetime(2026, 10, 20))] == ["Planned outage (landlord)"]
        save({"days": days})
        assert json.loads(OUT.read_text())["days"] == days
        v1 = {"windows": [["08:00", "10:00"]], "title": "a", "published": "2026-10-14T21:00:00"}
        v2 = {"windows": [], "title": "b", "published": "2026-10-15T07:00:00"}
        assert record({"2026-10-15": {"versions": [v1]}}) == 1
        assert record({"2026-10-15": {"versions": [v2, v1]}}) == 1  # only the update is new
        assert [r["published"] for r in map(json.loads, HISTORY.read_text().splitlines())] == [v1["published"], v2["published"]]
    print("ok")


if __name__ == "__main__":
    demo() if sys.argv[1:] == ["demo"] else run()
