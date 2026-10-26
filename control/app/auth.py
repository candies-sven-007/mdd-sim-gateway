"""Local administrator authentication for the management UI.

Credentials are stored outside the source tree in ``$MDD_DATA/auth.json``. Passwords use
stdlib scrypt with a per-install salt.

Sessions survive a restart. They used to be memory-only, which reads as deliberate until you
count the restarts: replacing an engine image, reloading and every self-update all restart the
control plane, so an appliance that is otherwise untouched logs its administrator out several
times a day. Only a hash of each token is written to ``$MDD_DATA/sessions.json``, so the file
cannot be replayed as a cookie by anyone who reads it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from . import config as cfg

AUTH_PATH = os.path.join(cfg.DATA_DIR, "auth.json")
SESSIONS_PATH = os.path.join(cfg.DATA_DIR, "sessions.json")
SESSION_COOKIE = "mdd_session"
SESSION_TTL = 12 * 60 * 60
# "Remember me" is for the browser someone administers the gateway from; the short default
# stays for everyone else.
SESSION_TTL_REMEMBER = 30 * 24 * 60 * 60
# Every request slides its session forward. Writing that to disk each time would rewrite the
# file continuously on an SD card for no benefit, so persist only once the expiry has really
# moved. A restart therefore costs at most this much of a session's remaining life.
SESSION_PERSIST_SKEW = 10 * 60
# Keyed by the token's hash, never the token itself.
_sessions: dict[str, dict] = {}
_failures: dict[str, list[float]] = {}
_lock = threading.RLock()


def _token_key(token: str) -> str:
    """Look a session up by digest so the stored file is not a list of usable cookies."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _save_sessions() -> None:
    """Persist the live sessions. Callers already hold the lock."""
    try:
        _write_json(SESSIONS_PATH, {"version": 1, "sessions": _sessions})
    except OSError:
        # A session that cannot be written is still valid in this process; losing it on the
        # next restart is not a reason to refuse the login that is happening now.
        pass


def _load_sessions() -> None:
    global _sessions
    try:
        with open(SESSIONS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return
    stored = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(stored, dict):
        return
    now = time.time()
    live = {}
    for key, item in stored.items():
        if not isinstance(item, dict):
            continue
        try:
            expires = float(item["expires"])
            ttl = float(item.get("ttl") or SESSION_TTL)
        except (KeyError, TypeError, ValueError):
            continue
        csrf = item.get("csrf")
        if not isinstance(csrf, str) or not csrf or expires <= now:
            continue
        live[str(key)] = {"csrf": csrf, "expires": expires, "ttl": ttl}
    with _lock:
        _sessions = live


def _read() -> dict:
    try:
        with open(AUTH_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def configured() -> bool:
    data = _read()
    return bool(data.get("salt") and data.get("password_hash"))


def username() -> str:
    """Return the configured single administrator name for the login screen."""
    return str(_read().get("username") or "admin")


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1,
                          dklen=32, maxmem=64 * 1024 * 1024)


def setup(password: str, username: str = "admin") -> None:
    if configured():
        raise ValueError("administrator account is already configured")
    if len(password) < 10 or len(password) > 256:
        raise ValueError("password must contain 10-256 characters")
    username = str(username or "admin").strip()
    if not username or len(username) > 64:
        raise ValueError("username must contain 1-64 characters")
    salt = secrets.token_bytes(16)
    payload = {
        "version": 1,
        "username": username,
        "salt": salt.hex(),
        "password_hash": _derive(password, salt).hex(),
        "created_at": int(time.time()),
    }
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    temporary = AUTH_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.chmod(temporary, 0o600)
    os.replace(temporary, AUTH_PATH)
    # A new administrator starts with nobody signed in: sessions left from the one reset-admin
    # removed must not carry over to the new account.
    with _lock:
        if _sessions:
            _sessions.clear()
            _save_sessions()


def throttled(peer: str) -> int:
    now = time.time()
    with _lock:
        attempts = [stamp for stamp in _failures.get(peer, []) if now - stamp < 900]
        _failures[peer] = attempts
    return max(0, 60 - int(now - attempts[-1])) if len(attempts) >= 5 else 0


def verify(username: str, password: str, peer: str) -> bool:
    """Check the administrator's credentials, counting a failure against the peer.

    The browser sign-in uses it to open a session.
    """
    data = _read()
    try:
        expected = bytes.fromhex(data["password_hash"])
        actual = _derive(password, bytes.fromhex(data["salt"]))
    except (KeyError, ValueError, TypeError):
        return False
    valid = hmac.compare_digest(str(username), str(data.get("username") or "admin"))
    valid = hmac.compare_digest(actual, expected) and valid
    with _lock:
        if valid:
            _failures.pop(peer, None)
        else:
            _failures.setdefault(peer, []).append(time.time())
    return valid


def login(username: str, password: str, peer: str,
          remember: bool = False) -> tuple[str, str] | None:
    if not verify(username, password, peer):
        return None
    with _lock:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        ttl = float(SESSION_TTL_REMEMBER if remember else SESSION_TTL)
        _sessions[_token_key(token)] = {"csrf": csrf, "expires": time.time() + ttl,
                                        "ttl": ttl}
        _save_sessions()
        return token, csrf


def session(token: str | None) -> dict | None:
    if not token:
        return None
    key = _token_key(token)
    with _lock:
        item = _sessions.get(key)
        now = time.time()
        if not item or item["expires"] < now:
            if _sessions.pop(key, None) is not None:
                _save_sessions()
            return None
        renewed = now + item.get("ttl", SESSION_TTL)
        # Only touch the disk once the window has genuinely moved on; see SESSION_PERSIST_SKEW.
        if renewed - item["expires"] >= SESSION_PERSIST_SKEW:
            item["expires"] = renewed
            _save_sessions()
        else:
            item["expires"] = renewed
        return dict(item)


def session_key(token: str | None) -> str:
    """A stable name for one browser session that is not the session's secret."""
    return _token_key(token) if token else ""


def logout(token: str | None) -> None:
    if token:
        with _lock:
            if _sessions.pop(_token_key(token), None) is not None:
                _save_sessions()


def change_password(current_password: str, new_password: str) -> None:
    if len(new_password) < 10 or len(new_password) > 256:
        raise ValueError("new password must contain 10-256 characters")
    data = _read()
    try:
        valid = hmac.compare_digest(
            _derive(current_password, bytes.fromhex(data["salt"])),
            bytes.fromhex(data["password_hash"]),
        )
    except (KeyError, ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("current password is incorrect")
    salt = secrets.token_bytes(16)
    data.update({"salt": salt.hex(), "password_hash": _derive(new_password, salt).hex(),
                 "changed_at": int(time.time())})
    temporary = AUTH_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.chmod(temporary, 0o600)
    os.replace(temporary, AUTH_PATH)
    with _lock:
        _sessions.clear()
        _save_sessions()


# A restart must not log the administrator out; see the module docstring.
_load_sessions()
