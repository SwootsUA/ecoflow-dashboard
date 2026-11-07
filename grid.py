"""Is the grid feeding the station? One rule for the page, the family page, /grid and the Telegram alerts.

Voltage on the input means the grid is there, and input watts with the voltage reading 0 means it is
charging. But the station refuses a sagging line (it took 175 V and up over October, and ran on battery
at 166-170 V on 7 Nov): voltage on the input, 0 W taken in, and the battery carrying the load. For the
battery that is an outage, so it counts as off.

    python3 grid.py    # self-check
"""

VOLTS = 50       # an unplugged input still reads a few volts
CARRYING = 20    # output watts that must come from the battery when nothing comes in


def refused(r: dict) -> bool:
    """Voltage on the input, yet the battery carries the load."""
    return ((r.get("ac_input_voltage") or 0) > VOLTS and not (r.get("ac_input_power") or 0)
            and r.get("input_power") == 0 and (r.get("output_power") or 0) > CARRYING)


def on_grid(r: dict) -> bool:
    return ((r.get("ac_input_voltage") or 0) > VOLTS or (r.get("ac_input_power") or 0) > 0) and not refused(r)


# the same rule for a WHERE clause over the readings table
ON_GRID_SQL = (f"(ac_input_voltage > {VOLTS} OR ac_input_power > 0) AND NOT (ac_input_voltage > {VOLTS} "
               f"AND COALESCE(ac_input_power, 0) = 0 AND COALESCE(input_power, -1) = 0 "
               f"AND COALESCE(output_power, 0) > {CARRYING})")


if __name__ == "__main__":
    import sqlite3
    on = {"ac_input_voltage": 228, "ac_input_power": 0, "input_power": 150, "output_power": 140}
    assert on_grid(on) and not refused(on)
    assert not on_grid(dict(on, ac_input_voltage=0))
    assert on_grid(dict(on, ac_input_voltage=0, ac_input_power=700))  # charging, voltage reading 0
    sag = dict(on, ac_input_voltage=166, input_power=0, output_power=95)
    assert refused(sag) and not on_grid(sag)
    assert on_grid(dict(sag, output_power=0))  # nothing drawn: no way to tell, trust the voltage
    assert on_grid({"ac_input_voltage": 228})  # old rows without watts
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE readings (ac_input_voltage, ac_input_power, input_power, output_power)")
    rows = [on, dict(on, ac_input_voltage=0), sag, dict(sag, output_power=0), {"ac_input_voltage": 228}]
    for r in rows:
        db.execute("INSERT INTO readings VALUES (?, ?, ?, ?)", [r.get(k) for k in ("ac_input_voltage", "ac_input_power", "input_power", "output_power")])
    got = [bool(x[0]) for x in db.execute(f"SELECT {ON_GRID_SQL} IS 1 FROM readings")]
    assert got == [on_grid(r) for r in rows], got
    print("ok")
