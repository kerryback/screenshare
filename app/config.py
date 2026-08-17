"""Settings, all from the environment.

This copy of the app is hosted -- on Koyeb -- rather than launched on the
classroom computer, so there is no config file to read and no home directory to
keep one in. Everything comes from environment variables set on the service.

    SCREENSHARE_CODE            the code students type. Set it, or it is random
                                each time the instance restarts.
    SCREENSHARE_DISPLAY_KEY     the secret in the instructor's /display URL.
                                Set it, or a restart invalidates the bookmark.
    SCREENSHARE_PUBLIC_URL      the address to put on the projector. Only worth
                                setting when the service answers on more than
                                one name and you want the short one shown.

    SCREENSHARE_CF_TURN_KEY_ID  Cloudflare Realtime TURN key ID
    SCREENSHARE_CF_TURN_TOKEN   its API token

    SCREENSHARE_TURN_URLS       or a static TURN server: comma-separated urls
    SCREENSHARE_TURN_USERNAME
    SCREENSHARE_TURN_CREDENTIAL

    SCREENSHARE_FORCE_RELAY     refuse direct paths, to prove TURN works

Nothing here is required to boot. With no TURN at all the app runs on public
STUN, which is enough wherever the two laptops can reach each other directly.
TURN is for the case where campus segmentation says they can't.

The distinction that matters for a hosted deployment: `refresh_turn` does
network I/O and is only ever called from a worker thread, while `ice_servers`
reads the cache and never blocks. A student joining must not wait on
Cloudflare, and the event loop must never sit in a urllib call.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_STUN = [
    "stun:stun.l.google.com:19302",
    "stun:stun.cloudflare.com:3478",
]


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def room_code() -> str:
    """The code students type to prove they are in the room."""
    return _env("SCREENSHARE_CODE") or f"{secrets.randbelow(9000) + 1000}"


def display_key() -> str:
    """The secret that separates the instructor's page from the students'."""
    return _env("SCREENSHARE_DISPLAY_KEY") or secrets.token_urlsafe(12)


def _static_turn() -> dict[str, Any] | None:
    urls = [u.strip() for u in _env("SCREENSHARE_TURN_URLS").split(",") if u.strip()]
    if not urls:
        return None
    server: dict[str, Any] = {"urls": urls}
    if _env("SCREENSHARE_TURN_USERNAME"):
        server["username"] = _env("SCREENSHARE_TURN_USERNAME")
        server["credential"] = _env("SCREENSHARE_TURN_CREDENTIAL")
    return server


# --- Cloudflare -------------------------------------------------------------

CLOUDFLARE_ENDPOINT = (
    "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"
)

# Cloudflare does not issue fixed passwords; it mints short-lived ones from a
# long-lived key. So credentials are fetched in the background and cached.
#
#   soft   when to start trying for a fresh set
#   hard   when the current set genuinely stops working
#
# The gap between them is what keeps a transient failure -- Cloudflare briefly
# unreachable at the wrong moment -- from taking TURN away mid-class. `error` is
# kept for /api/state, because a quietly failed fetch and a hostile campus
# network look identical from the projector and want opposite fixes.
_cache: dict[str, Any] = {"servers": None, "soft": 0.0, "hard": 0.0, "error": ""}


def _cloudflare() -> dict[str, Any] | None:
    key_id = _env("SCREENSHARE_CF_TURN_KEY_ID")
    token = _env("SCREENSHARE_CF_TURN_TOKEN")
    if not key_id or not token:
        return None
    try:
        ttl = int(_env("SCREENSHARE_CF_TURN_TTL", "86400"))
    except ValueError:
        ttl = 86400
    return {"key_id": key_id, "token": token, "ttl": max(600, ttl)}


def _fetch_cloudflare(cloud: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Ask Cloudflare for a fresh ICE list. Returns None on any failure.

    Cloudflare hands back the whole list -- its own STUN plus TURN over UDP, TCP
    and TLS on 443 -- so it replaces the defaults rather than adding to them.
    The 443/TLS entry is the one that survives a network that blocks UDP.
    """
    request = urllib.request.Request(
        CLOUDFLARE_ENDPOINT.format(key_id=cloud["key_id"]),
        data=json.dumps({"ttl": cloud["ttl"]}).encode(),
        headers={
            "Authorization": f"Bearer {cloud['token']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = "check the key ID and API token" if exc.code in (401, 403) else exc.reason
        _cache["error"] = f"Cloudflare returned {exc.code} ({detail})"
        return None
    except Exception as exc:
        _cache["error"] = f"Could not reach Cloudflare: {exc}"
        return None

    servers = payload.get("iceServers")
    if isinstance(servers, dict):  # older shape: a single server object
        servers = [servers]
    if not isinstance(servers, list) or not servers:
        _cache["error"] = "Cloudflare's reply had no iceServers in it"
        return None

    _cache["error"] = ""
    return servers


def refresh_turn(force: bool = False) -> None:
    """Fetch credentials if the cached ones are getting old. Blocks: use a thread.

    Called at startup and then on a timer, so `ice_servers` only ever reads what
    this left behind.
    """
    cloud = _cloudflare()
    if cloud is None:
        return
    if not force and _cache["servers"] and time.time() < _cache["soft"]:
        return

    servers = _fetch_cloudflare(cloud)
    if servers is None:
        return  # keep whatever we have until `hard`

    now = time.time()
    _cache["servers"] = servers
    _cache["soft"] = now + cloud["ttl"] * 0.5
    _cache["hard"] = now + cloud["ttl"] * 0.95


def refresh_interval() -> float:
    """How often the background task should call `refresh_turn`."""
    cloud = _cloudflare()
    if cloud is None:
        return 3600.0
    return max(300.0, min(1800.0, cloud["ttl"] * 0.25))


def _live_cloudflare() -> list[dict[str, Any]] | None:
    if _cache["servers"] and time.time() < _cache["hard"]:
        return _cache["servers"]
    return None


def turn_status() -> dict[str, Any]:
    if _cloudflare():
        working = _live_cloudflare() is not None
        return {
            "source": "cloudflare" if working else "cloudflare (failing)",
            "configured": working,
            "error": _cache["error"],
        }
    if _static_turn():
        return {"source": "static", "configured": True, "error": ""}
    return {"source": "none", "configured": False, "error": ""}


# --- what the browsers get --------------------------------------------------


def ice_servers() -> list[dict[str, Any]]:
    """Never blocks. Whatever the last refresh left, or plain STUN."""
    cloudflare = _live_cloudflare()
    if cloudflare:
        return cloudflare

    # Fall through to STUN rather than handing back nothing: direct connections
    # still work, and only the relay is missing.
    servers: list[dict[str, Any]] = [{"urls": list(DEFAULT_STUN)}]
    static = _static_turn()
    if static:
        servers.append(static)
    return servers


def has_turn() -> bool:
    return turn_status()["configured"]


def ice_policy() -> str:
    """"relay" makes both ends refuse direct paths -- a way to prove TURN works."""
    return "relay" if _env("SCREENSHARE_FORCE_RELAY") and has_turn() else "all"
