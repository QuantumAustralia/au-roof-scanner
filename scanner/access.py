"""Password gate for the operator pages.

The public marketplace stays open. The scanner dashboard and the review
page ask for the name and password in DASHBOARD_USER and DASHBOARD_PASSWORD.
When those are blank, the pages stay open so this PC can be used locally.
"""

from __future__ import annotations

import secrets

from flask import Response, request

from scanner.config import env

_PUBLIC_GET = {
    "/marketplace",
    "/marketplace/cart",
    "/marketplace/checkout",
    "/marketplace/success",
    "/privacy",
    "/terms",
    "/data-terms",
}


def password_configured() -> bool:
    return bool(env("DASHBOARD_USER") and env("DASHBOARD_PASSWORD"))


def marketplace_is_public() -> bool:
    """Buyer pages and roof photos. Publishing and pricing stay locked."""
    path = request.path.rstrip("/") or "/"
    if path.startswith("/roof/") or path.startswith("/thumb/"):
        return True
    if request.method == "POST" and path in {"/marketplace/checkout", "/marketplace/webhook"}:
        return True
    return request.method in {"GET", "HEAD"} and path in _PUBLIC_GET


def require_password() -> Response | None:
    """401 until the browser sends the configured name and password."""
    if not password_configured():
        return None
    supplied = request.authorization
    user = env("DASHBOARD_USER")
    password = env("DASHBOARD_PASSWORD")
    if (
        supplied is not None
        and supplied.username is not None
        and supplied.password is not None
        and secrets.compare_digest(supplied.username, user)
        and secrets.compare_digest(supplied.password, password)
    ):
        return None
    return Response(
        "Sign in required.",
        401,
        {"WWW-Authenticate": 'Basic realm="AU Roof Scanner"'},
    )
