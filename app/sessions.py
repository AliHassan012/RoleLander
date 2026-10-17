from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from app.db import connect, hash_cookie, new_opaque_cookie

COOKIE_NAME = "rolelander_session"
SESSION_DAYS = 14


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def create_session(db_path, user_id: str) -> tuple[str, str]:
    cookie_value = new_opaque_cookie()
    csrf_token = secrets.token_urlsafe(32)
    now = utc_now()
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions(cookie_hash, session_key, user_id, csrf_token, created_at, expires_at) VALUES(?,?,?,?,?,?)",
            (
                hash_cookie(cookie_value),
                secrets.token_urlsafe(24),
                user_id,
                csrf_token,
                now.isoformat(),
                (now + timedelta(days=SESSION_DAYS)).isoformat(),
            ),
        )
    return cookie_value, csrf_token


def current_session(request: Request):
    cookie_value = request.cookies.get(COOKIE_NAME)
    if not cookie_value:
        raise HTTPException(status_code=401, detail="Authentication required")
    with connect(request.app.state.db_path) as conn:
        row = conn.execute(
            """SELECT s.user_id, s.csrf_token, s.expires_at, u.verified_email
               FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.cookie_hash=?""",
            (hash_cookie(cookie_value),),
        ).fetchone()
    if row is None or datetime.fromisoformat(row["expires_at"]) <= utc_now():
        raise HTTPException(status_code=401, detail="Session expired")
    return row


def require_csrf(request: Request, session_row) -> None:
    origin = request.headers.get("origin", "").rstrip("/")
    token = request.headers.get("x-csrf-token", "")
    if origin != request.app.state.settings.app_origin:
        raise HTTPException(status_code=403, detail="Origin check failed")
    if not hmac.compare_digest(token, session_row["csrf_token"]):
        raise HTTPException(status_code=403, detail="CSRF check failed")


