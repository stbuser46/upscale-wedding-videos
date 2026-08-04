from __future__ import annotations

from collections import defaultdict, deque
from functools import wraps
import hmac
import secrets
import time
from typing import Callable, TypeVar

from flask import abort, current_app, redirect, request, session, url_for


F = TypeVar("F", bound=Callable)
_attempts: dict[str, deque[float]] = defaultdict(deque)


def login_rate_limited(address: str, *, limit: int = 8, window: int = 300) -> bool:
    now = time.monotonic()
    attempts = _attempts[address]
    while attempts and attempts[0] < now - window:
        attempts.popleft()
    if len(attempts) >= limit:
        return True
    attempts.append(now)
    return False


def password_matches(candidate: str) -> bool:
    configured = current_app.config["WEBAPP_SETTINGS"].password or ""
    return hmac.compare_digest(candidate.encode("utf-8"), configured.encode("utf-8"))


def ensure_csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def csrf_protect() -> None:
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.path.startswith("/api/"):
        supplied = request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            abort(403, description="Missing or invalid CSRF token")


def login_required(view: F) -> F:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                abort(401, description="Authentication required")
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped  # type: ignore[return-value]
