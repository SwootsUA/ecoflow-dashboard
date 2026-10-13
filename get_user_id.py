"""One-time: fetch the EcoFlow account's user ID and store it in .env.

The station's Bluetooth login is md5(user ID + serial number), and the serial is
broadcast, so the ID is the secret: it goes to .env and is never printed. The password
is used for this one request and is not stored. Run it yourself:

    python3 get_user_id.py
"""

import base64
import getpass
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ENV = Path(__file__).with_name(".env")

email = input("EcoFlow email: ").strip()
password = getpass.getpass("EcoFlow password (not shown, not stored): ")
body = {
    "scene": "IOT_APP",
    "appVersion": "1.0.0",
    "userType": "ECOFLOW",
    "oauth": {"bundleId": "com.ef.EcoFlow"},
    "email": email,
    "password": base64.b64encode(password.encode()).decode(),
}
request = urllib.request.Request(
    "https://api.ecoflow.com/auth/login",
    json.dumps(body).encode(),
    {"Content-Type": "application/json", "Accept": "application/json"},
)
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
except urllib.error.HTTPError as e:
    raise SystemExit(f"Login failed: HTTP {e.code}")
if result.get("code") != "0":
    raise SystemExit(f"Login failed: {result.get('message')}")

user_id = result["data"]["user"]["userId"]
kept = ENV.read_text().splitlines() if ENV.exists() else []
kept = [line for line in kept if not line.startswith("ECOFLOW_USER_ID=")]
ENV.write_text("\n".join([*kept, f"ECOFLOW_USER_ID={user_id}"]) + "\n")
os.chmod(ENV, 0o600)
print("Saved ECOFLOW_USER_ID to .env")
