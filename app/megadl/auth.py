"""Sign-in for the web GUI: a login page plus a signed session cookie.

The password is sent once, when you sign in. After that the browser only
sends a cookie, so the password doesn't travel with every request (sending
it with every request, as HTTP Basic auth does, trips network scanners such
as UniFi's IPS, which then block the connection).

The cookie holds an expiry time and an HMAC signature made with a random key
kept in /config. Changing WEBUI_PASSWORD (or WEBUI_USER) signs everyone out.
"""

import hashlib
import hmac
import os
import secrets
import threading
import time

from . import config

COOKIE = "megadl_session"
REMEMBER_DAYS = 30
SESSION_HOURS = 24

MAX_FAILURES = 5
FAILURE_WINDOW = 300  # seconds


def enabled():
    return bool(config.WEBUI_PASSWORD)


def _load_key():
    path = os.path.join(config.CONFIG_DIR, "session.key")
    try:
        with open(path, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    except OSError:
        pass  # sessions then just won't survive a restart
    return key


_KEY = _load_key()


def _fingerprint():
    # Ties sessions to the current user name and password.
    data = f"{config.WEBUI_USER}\0{config.WEBUI_PASSWORD}".encode()
    return hmac.new(_KEY, data, hashlib.sha256).hexdigest()[:24]


def _sign(expires):
    msg = f"{expires}.{_fingerprint()}".encode()
    return hmac.new(_KEY, msg, hashlib.sha256).hexdigest()


def make_token(remember):
    seconds = REMEMBER_DAYS * 86400 if remember else SESSION_HOURS * 3600
    expires = int(time.time()) + seconds
    return f"{expires}.{_sign(expires)}", (seconds if remember else None)


def valid_token(token):
    if not token or "." not in token:
        return False
    expires, _, sig = token.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(sig, _sign(int(expires)))


def check_credentials(user, secret):
    ok_user = hmac.compare_digest((user or "").encode(), config.WEBUI_USER.encode())
    ok_pass = hmac.compare_digest((secret or "").encode(), config.WEBUI_PASSWORD.encode())
    return ok_user and ok_pass


class Throttle:
    """Slow down password guessing: 5 wrong tries per 5 minutes per address."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fails = {}

    def _recent(self, ip, now):
        times = [t for t in self._fails.get(ip, []) if now - t < FAILURE_WINDOW]
        self._fails[ip] = times
        return times

    def blocked(self, ip):
        with self._lock:
            return len(self._recent(ip, time.time())) >= MAX_FAILURES

    def failed(self, ip):
        with self._lock:
            now = time.time()
            self._recent(ip, now).append(now)

    def succeeded(self, ip):
        with self._lock:
            self._fails.pop(ip, None)


throttle = Throttle()
