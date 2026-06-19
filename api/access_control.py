"""Admin UI password, session, and model-visibility state."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from config.paths import access_state_path

ADMIN_SESSION_COOKIE = "fcc_admin_session"
ADMIN_SESSION_MAX_AGE_SECONDS = 9 * 60 * 60
PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000


def now_utc() -> datetime:
    """Return the current timezone-aware UTC timestamp."""

    return datetime.now(UTC)


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _default_password_hash() -> str:
    return hash_password("1")


def hash_password(password: str) -> str:
    """Return a password hash string suitable for storage."""

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Return whether a cleartext password matches the stored hash."""

    try:
        scheme, iterations_raw, salt, expected = stored_hash.split("$", 3)
    except ValueError:
        return False
    if scheme != PASSWORD_SCHEME:
        return False
    try:
        iterations = int(iterations_raw)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(actual, expected)


def _default_state() -> dict[str, Any]:
    return {
        "password_hash": _default_password_hash(),
        "sessions": {},
        "visible_model_ids": None,
    }


def _state_path() -> Path:
    return access_state_path()


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return _default_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return _default_state()
    state = _default_state()
    if isinstance(raw, dict):
        state.update(
            {
                "password_hash": str(
                    raw.get("password_hash") or state["password_hash"]
                ),
                "sessions": raw.get("sessions") or {},
            }
        )
        if "visible_model_ids" in raw:
            state["visible_model_ids"] = raw.get("visible_model_ids")
    return state


def _write_state(state: Mapping[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def _purge_expired_sessions(state: dict[str, Any], *, current_time: datetime) -> bool:
    sessions = state.get("sessions", {})
    changed = False
    for session_id, session in list(sessions.items()):
        expires_at = session.get("expires_at")
        if not isinstance(expires_at, str):
            sessions.pop(session_id, None)
            changed = True
            continue
        if _parse_datetime(expires_at) <= current_time:
            sessions.pop(session_id, None)
            changed = True
    return changed


def admin_session_status(session_id: str | None) -> dict[str, Any]:
    """Return whether an admin cookie session is active."""

    state = _load_state()
    current_time = now_utc()
    changed = _purge_expired_sessions(state, current_time=current_time)
    session = state.get("sessions", {}).get(session_id or "")
    authenticated = False
    expires_at: str | None = None
    if isinstance(session, dict):
        expires_at_raw = session.get("expires_at")
        if (
            isinstance(expires_at_raw, str)
            and _parse_datetime(expires_at_raw) > current_time
        ):
            authenticated = True
            expires_at = expires_at_raw
    if changed:
        _write_state(state)
    return {"authenticated": authenticated, "expires_at": expires_at}


def create_admin_session(password: str) -> dict[str, str] | None:
    """Create an admin cookie session when the password is correct."""

    state = _load_state()
    if not verify_password(password, str(state.get("password_hash", ""))):
        return None
    current_time = now_utc()
    _purge_expired_sessions(state, current_time=current_time)
    session_id = secrets.token_urlsafe(32)
    expires_at = _isoformat(
        current_time + timedelta(seconds=ADMIN_SESSION_MAX_AGE_SECONDS)
    )
    state.setdefault("sessions", {})[session_id] = {
        "created_at": _isoformat(current_time),
        "expires_at": expires_at,
    }
    _write_state(state)
    return {"session_id": session_id, "expires_at": expires_at}


def logout_admin_session(session_id: str | None) -> None:
    """Delete one admin cookie session if present."""

    if not session_id:
        return
    state = _load_state()
    sessions = state.get("sessions", {})
    if session_id in sessions:
        sessions.pop(session_id, None)
        _write_state(state)


def change_admin_password(current_password: str, new_password: str) -> bool:
    """Change the admin UI password and clear existing sessions."""

    state = _load_state()
    if not verify_password(current_password, str(state.get("password_hash", ""))):
        return False
    state["password_hash"] = hash_password(new_password)
    state["sessions"] = {}
    _write_state(state)
    return True


def get_visible_model_ids() -> list[str] | None:
    """Return explicit client-facing model ids, or None when unconfigured."""

    state = _load_state()
    model_ids = state.get("visible_model_ids")
    if model_ids is None:
        return None
    return [str(model_id) for model_id in model_ids if str(model_id).strip()]


def set_visible_model_ids(model_ids: list[str]) -> list[str]:
    """Persist the explicit client-facing model ids to expose."""

    state = _load_state()
    normalized = sorted(
        {model_id.strip() for model_id in model_ids if model_id.strip()}
    )
    state["visible_model_ids"] = normalized
    _write_state(state)
    return normalized
