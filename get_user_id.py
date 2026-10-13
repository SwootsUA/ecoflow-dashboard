"""One-time: fetch the EcoFlow account's user ID and store it in .env.

The station's Bluetooth login is md5(user ID + serial number), and the serial is
broadcast, so the ID is the secret: it goes to .env and is never printed. The password
is used for these requests and is not stored. Run it yourself:

    python3 get_user_id.py

One email can be a separate account on each EcoFlow host, each with its own ID, and
only the one the station is bound to works. So every host is asked and each ID is kept
as ECOFLOW_USER_ID_<HOST>; ble_read.py uses ECOFLOW_USER_ID.
"""

import base64
import getpass
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ENV = Path(__file__).with_name(".env")
HOSTS = ["api-e", "api-a", "api"]  # Europe, America, default


def login(host: str, email: str, password: str) -> str:
    body = {
        "scene": "IOT_APP",
        "appVersion": "1.0.0",
        "userType": "ECOFLOW",
        "oauth": {"bundleId": "com.ef.EcoFlow"},
        "email": email,
        "password": base64.b64encode(password.encode()).decode(),
    }
    request = urllib.request.Request(
        f"https://{host}.ecoflow.com/auth/login",
        json.dumps(body).encode(),
        {"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError) as e:
        raise LookupError(str(e))
    if result.get("code") != "0":
        raise LookupError(result.get("message"))
    return result["data"]["user"]["userId"]


email = input("EcoFlow email: ").strip()
password = getpass.getpass("EcoFlow password (not shown, not stored): ")

ids = {}
for host in HOSTS:
    try:
        ids[host] = login(host, email, password)
    except LookupError as e:
        print(f"{host}: no login ({e})")
        continue
    twin = next((h for h in ids if h != host and ids[h] == ids[host]), None)
    print(f"{host}: account found" + (f", same ID as {twin}" if twin else ", its own ID"))
if not ids:
    raise SystemExit("No host accepted the login")

kept = ENV.read_text().splitlines() if ENV.exists() else []
kept = [line for line in kept if not line.startswith("ECOFLOW_USER_ID")]
found = [f"ECOFLOW_USER_ID_{host.upper().replace('-', '_')}={uid}" for host, uid in ids.items()]
ENV.write_text("\n".join([*kept, *found, f"ECOFLOW_USER_ID={next(iter(ids.values()))}"]) + "\n")
os.chmod(ENV, 0o600)
print("Saved to .env")
