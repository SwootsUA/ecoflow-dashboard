"""Single sign-on for the *.swoots.dev apps.

home.swoots.dev sets a signed `swoots_sso` cookie on the whole domain once the owner password checks out.
Every app reads it: no session yet + a valid cookie = logged in. Logging out of any app deletes it, so
that logs you out everywhere. Only home ever sets it, so WT watch links and per-app logins never grant it.
The same file is copied into every app repo; keep the copies identical."""
import os

from flask import request, session
from itsdangerous import BadData, URLSafeTimedSerializer

COOKIE = "swoots_sso"
MAX_AGE = 30 * 24 * 3600
DOMAIN = "swoots.dev"  # the parent domain: the browser sends it to every subdomain
KEY_FILE = os.environ.get("SSO_KEY_FILE", "/home/pi/data/sso/key")  # rotate it to log out every browser


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


def init_sso(app, **fields):
    """Call right after app.secret_key. fields: extra session values this app's owner login sets (WT: role="full")."""
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
