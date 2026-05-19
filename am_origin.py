"""Origin enforcement for the local backend.

The viewer is meant to be reached only from a browser tab pointed at this
same local backend. A cross-origin page (or another localhost service) can
still try to drive our endpoints — WebSockets are not subject to the
Same-Origin Policy at the connection level, and POSTs ride CSRF if not
checked. Reject any request whose Origin doesn't match the local host.
"""
from __future__ import annotations

from urllib.parse import urlparse

from aiohttp import web


def _is_localhost_origin(request: web.Request) -> bool:
    """True iff the Origin header is http://(127.0.0.1|localhost):<port> and
    matches the request's Host header. Caller decides what to do when Origin
    is absent (allowed for top-level navigation, rejected for state changes)."""
    origin = request.headers.get("Origin", "")
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme != "http":
        return False
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        return False
    if parsed.netloc != request.host:
        return False
    return True


def _require_localhost_origin(request: web.Request) -> None:
    """Reject the request if it carries a non-localhost Origin header.

    Missing Origin is *allowed* — per the Fetch spec, browsers omit the
    Origin header for same-origin GET/HEAD requests, so a strict "Origin
    required" check would reject legitimate same-origin viewer fetches
    (history, doc loads) in Chrome and Firefox. The threat we want to stop
    is cross-origin and DNS-rebinding requests, which *always* set Origin
    to a non-localhost value — and those we still reject."""
    if request.headers.get("Origin") and not _is_localhost_origin(request):
        raise web.HTTPForbidden(text="origin not allowed")


# Same behavior as _require_localhost_origin; kept as a separate name to
# document intent at the call sites (static resource serving vs state-
# mutating endpoints).
_check_static_origin = _require_localhost_origin
