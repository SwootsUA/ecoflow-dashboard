"""Single sign-on for the *.swoots.dev apps.

home.swoots.dev sets a signed `swoots_sso` cookie on the whole domain once the owner password checks out.
Every app reads it: no session yet + a valid cookie = logged in. Logging out of any app deletes it, so
that logs you out everywhere. Only home ever sets it, so WT watch links never grant it.
Home is the only door: every other app's sign-in page sends the browser there (one password form to guard,
with its lockout and alerts, instead of eleven). The same file is copied into every app repo; keep them identical."""
import os
from urllib.parse import quote

from flask import redirect, request, session
from itsdangerous import BadData, URLSafeTimedSerializer

COOKIE = "swoots_sso"
MAX_AGE = 30 * 24 * 3600
DOMAIN = "swoots.dev"  # the parent domain: the browser sends it to every subdomain
KEY_FILE = os.environ.get("SSO_KEY_FILE", "/home/pi/data/sso/key")  # rotate it to log out every browser
HOME = "https://home.swoots.dev"


def _signer():
    try:
        with open(KEY_FILE) as f:
            return URLSafeTimedSerializer(f.read().strip(), salt="swoots-sso")
    except OSError:
        return None


def _valid(signer) -> bool:
    try:
        return signer.loads(request.cookies.get(COOKIE, ""), max_age=MAX_AGE) == "owner"
    except BadData:
        return False


def init_sso(app, front_door=False, **fields):
    """Call right after app.secret_key. front_door: home only. fields: extra session values this app's owner
    login sets (WT: role="full")."""
    # every app's own session cookie too: https only, hidden from page scripts, not sent cross-site
    app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    if not front_door:
        @app.before_request
        def home_is_the_door():
            # /login everywhere; lost-souls signs in under /auth (its passkey pages)
            if request.path.rstrip("/") in ("/login", "/auth") or request.path.startswith("/auth/"):
                # https by hand: behind the tunnel Flask sees http, and home only returns to https pages
                return redirect(f"{HOME}/login?next={quote(f'https://{request.host}/', safe='')}")

    signer = _signer()
    if not signer:
        app.logger.warning("sso: no key at %s, single sign-on is off", KEY_FILE)
        return

    @app.before_request
    def sso_login():
        if session.get("logged_in") or "logout" in (request.endpoint or ""):
            return
        if _valid(signer):
            session.update(logged_in=True, **fields)
            session.permanent = True

    @app.after_request
    def sso_logout(response):
        if "logout" in (request.endpoint or ""):
            response.delete_cookie(COOKIE, domain=DOMAIN, secure=True, httponly=True, samesite="Lax")
        return response


def set_cookie(response):
    """home only, after the owner password checks out."""
    response.set_cookie(COOKIE, _signer().dumps("owner"), max_age=MAX_AGE, domain=DOMAIN,
                        secure=True, httponly=True, samesite="Lax")
    return response


if __name__ == "__main__":  # self-check: python3 sso.py
    from flask import Flask
    app = Flask(__name__)
    app.secret_key = "t"
    init_sso(app)
    client = app.test_client()
    for path in ("/login", "/login/", "/auth", "/auth/login/begin"):
        r = client.post(path, base_url="http://chat.swoots.dev")  # what the tunnel hands Flask
        assert r.status_code == 302 and r.location == f"{HOME}/login?next=https%3A%2F%2Fchat.swoots.dev%2F", (path, r.location)
    assert client.get("/authors", base_url="https://chat.swoots.dev").status_code == 404  # not a sign-in page
    assert app.config["SESSION_COOKIE_SECURE"] and app.config["SESSION_COOKIE_HTTPONLY"]
    print("sso ok")
