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
import sys
import tempfile
import urllib.request
from contextlib import closing
from pathlib import Path

QUEUE = os.environ.get("OUTAGE_QUEUE", "2.1")
POSTS = "https://www.cherkasyoblenergo.com/api/v1/posts"
OUT = Path(os.environ.get("ECOFLOW_OUTAGES", "/home/pi/data/ecoflow/outages.json"))
DB = Path(os.environ.get("ECOFLOW_DB", "/home/pi/data/ecoflow/ecoflow.db"))
SEND = "http://127.0.0.1:8010/send"
CLOCK_SET = Path("/run/systemd/timesync/synchronized")  # no RTC: after an outage the Pi boots in the past
WARN_BEFORE = dt.timedelta(minutes=30)
# Battery % used per hour, measured in the 14 Oct outage. The page reads these from OUT too.
# ponytail: one outage's numbers; re-measure once a few more outages are logged
DRAIN = {"PC on": 22.5, "PC off": 2.8}
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
    """{day: {"windows", "title", "published"}} from the newest post for each day from today on."""
    days = {}
    for post in sorted(get(f"{POSTS}/main?lang=uk&limit=50"), key=lambda p: p["publishedAt"], reverse=True):
        day = post_day(post["title"], post["publishedAt"])
        if day and day >= today and day not in days:
            body = get(f"{POSTS}/{post['slug']}?lang=uk")["content"]
            days[day] = {"windows": windows(body, queue), "title": post["title"], "published": post["publishedAt"]}
    return days


def spans(days: dict) -> list[tuple[dt.datetime, dt.datetime]]:
    """Every window as (start, end), soonest first; "24:00" is the next midnight."""
    def at(day, hhmm):
        hours, minutes = map(int, hhmm.split(":"))
        return dt.datetime.fromisoformat(day) + dt.timedelta(hours=hours, minutes=minutes)

    return sorted((at(day, a), at(day, b)) for day, info in days.items() for a, b in info["windows"])


def lasts(hours: float) -> str:
    return f"{int(hours)} h {round(hours % 1 * 60):02d} min" if hours < 20 else f"{round(hours)} h"


def battery_line(battery: dict | None) -> str:
    if not battery:
        return ""
    above = max(0, battery["battery_level"] - battery["battery_charge_limit_min"])
    on, off = (above / DRAIN[name] for name in ("PC on", "PC off"))
    return (f"\nBattery {battery['battery_level']:.0f}%: lasts ~{lasts(on)} with the PC on, "
            f"~{lasts(off)} with it off (to the {battery['battery_charge_limit_min']:.0f}% cutoff).")


def day_label(day: str, now: dt.datetime) -> str:
    offset = (dt.date.fromisoformat(day) - now.date()).days
    return {0: "today", 1: "tomorrow"}.get(offset, dt.date.fromisoformat(day).strftime("%d %b"))


def messages(old: dict, days: dict, now: dt.datetime, battery: dict | None) -> tuple[list[str], list[str]]:
    """The Telegram messages this run owes, and the window starts warned about (old ones dropped)."""
    out = []
    for day, info in sorted(days.items()):
        if info["windows"] == old.get("days", {}).get(day, {}).get("windows"):
            continue
        what = "changed" if day in old.get("days", {}) else "posted"
        slots = ", ".join(f"{a}–{b}" for a, b in info["windows"]) or "none listed"
        out.append(f"🔌 Schedule {what} for {day_label(day, now)}, queue {QUEUE}: {slots}. Possible, not certain.")
    warned = [start for start in old.get("warned", []) if start >= (now - dt.timedelta(days=2)).isoformat()]
    for start, end in spans(days):
        if now < start <= now + WARN_BEFORE and start.isoformat() not in warned:
            out.append(f"⚡ Possible outage {start:%H:%M}–{end:%H:%M} (queue {QUEUE}), "
                       f"in {round((start - now).total_seconds() / 60)} min." + battery_line(battery))
            warned.append(start.isoformat())
    return out, warned


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
    texts, warned = messages(old, days, now, latest_battery())
    for text in texts:
        send(text)  # a failed send raises before the save, so the next run tries again
    save({"queue": QUEUE, "checked": now.isoformat(), "days": days, "warned": warned, "drain": DRAIN})


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
    assert spans(days)[1] == (dt.datetime(2026, 10, 14, 18), dt.datetime(2026, 10, 15))
    battery = {"battery_level": 90.0, "battery_charge_limit_min": 15}
    # first sight of a day: one "posted" message; nothing to warn about at 15:00
    texts, warned = messages({}, days, dt.datetime(2026, 10, 14, 15), battery)
    assert texts == ["🔌 Schedule posted for today, queue 2.1: 10:00–12:00, 18:00–24:00. Possible, not certain."] and warned == []
    old = {"days": days, "warned": warned}
    # 17:40: the 18:00 window is 20 minutes away, warned once, with the battery estimate
    texts, warned = messages(old, days, dt.datetime(2026, 10, 14, 17, 40), battery)
    assert len(texts) == 1 and "18:00–00:00" in texts[0] and "in 20 min" in texts[0] and "~3 h 20 min with the PC on" in texts[0], texts
    assert messages({"days": days, "warned": warned}, days, dt.datetime(2026, 10, 14, 17, 50), battery) == ([], warned)
    # a changed schedule is announced again
    texts, _ = messages(old, {"2026-10-14": {"windows": [["18:00", "20:00"]]}}, dt.datetime(2026, 10, 14, 15), None)
    assert texts == ["🔌 Schedule changed for today, queue 2.1: 18:00–20:00. Possible, not certain."], texts

    global OUT
    with tempfile.TemporaryDirectory() as tmp:
        OUT = Path(tmp, "outages.json")
        save({"days": days})
        assert json.loads(OUT.read_text())["days"] == days
    print("ok")


if __name__ == "__main__":
    demo() if sys.argv[1:] == ["demo"] else run()
