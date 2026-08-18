"""A screen-sharing relay for a classroom, hosted rather than run locally.

Students open the app's https address on their own laptops, pick a screen,
window or browser tab, and wait. The instructor -- at the classroom computer,
on the display page -- chooses whose screen goes up on the projector.

The server only carries the WebRTC handshake and a short list of who is in the
room. The video never passes through it: it goes browser to browser, directly
when the network permits and through a TURN relay when it does not. That is why
a 0.1-vCPU instance is enough for a full class.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config
from .hub import Hub

BASE_DIR = Path(__file__).resolve().parent

CODE = config.room_code()
DISPLAY_KEY = config.display_key()

hub = Hub(CODE)


async def _turn_loop() -> None:
    """Keep Cloudflare credentials fresh in the background.

    Refreshing here rather than on demand is what keeps `ice_servers` free of
    network I/O: a student joining mid-class never waits on Cloudflare, and the
    event loop never sits inside a blocking urllib call while it happens.
    """
    while True:
        await asyncio.sleep(config.refresh_interval())
        try:
            await asyncio.to_thread(config.refresh_turn)
        except Exception as exc:  # a failed refresh must not kill the task
            print(f"[screenshare] TURN refresh failed: {exc}", flush=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(config.refresh_turn, True)
    status = config.turn_status()
    print(f"[screenshare] room code: {CODE}", flush=True)
    if not os.environ.get("SCREENSHARE_DISPLAY_KEY"):
        # Worth shouting about: without the variable the instructor's bookmarked
        # display URL stops working every time the instance restarts.
        print(
            f"[screenshare] SCREENSHARE_DISPLAY_KEY is not set — using {DISPLAY_KEY} "
            "for this run only. Set it in the service's variables.",
            flush=True,
        )
    print(f"[screenshare] TURN: {status['source']}", flush=True)
    if status["error"]:
        print(f"[screenshare] TURN problem: {status['error']}", flush=True)

    task = asyncio.create_task(_turn_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Screen Share", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.middleware("http")
async def force_https(request: Request, call_next):
    """Send http:// callers to https:// before they reach a page that needs it.

    A student typing the bare hostname can land on http, and browsers hand out
    no screen capture outside a secure context -- so the share button would
    simply not work, on a page that otherwise looks fine. The edge does not
    redirect for us, so this does.

    Only requests that actually arrived over http at the edge are redirected.
    Koyeb's health check reaches the container directly with no forwarded
    scheme, and redirecting that would fail the check and take the service
    down.
    """
    if request.headers.get("x-forwarded-proto", "") == "http":
        return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
    return await call_next(request)


def _require_key(key: str) -> None:
    """The display page and the admin endpoints sit behind an unguessable key.

    Every route here is on the public internet, so where a request comes from
    proves nothing at all. The key does.
    """
    if not secrets.compare_digest(key or "", DISPLAY_KEY):
        raise HTTPException(status_code=404, detail="Not found")


LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "0.0.0.0")


def _public_url(request: Request) -> str:
    """The address students should type, as they would have to type it.

    This ends up on the projector, so getting the scheme wrong is not cosmetic:
    browsers refuse to capture a screen on a page that isn't secure, and a
    student who follows an http:// link gets a dead button and no explanation.
    Anything that isn't localhost is therefore https, whatever the edge
    happened to forward.
    """
    configured = (os.environ.get("SCREENSHARE_PUBLIC_URL") or "").strip()
    if configured:
        return configured.rstrip("/")

    forwarded = request.headers.get("x-forwarded-host", "")
    host = forwarded.split(",")[0].strip() or request.headers.get("host", "")
    if not host:
        return str(request.base_url).rstrip("/")

    local = host.split(":")[0] in LOCAL_HOSTS
    return f"{'http' if local else 'https'}://{host}"


# --- pages -----------------------------------------------------------------


@app.get("/")
def share_page(request: Request):
    """What the app's address gives a student."""
    return templates.TemplateResponse(request, "share.html", {})


@app.get("/display")
def display_page(request: Request, key: str = ""):
    """The classroom computer's own page. Bookmark it; the key is in the URL."""
    _require_key(key)
    status = config.turn_status()
    return templates.TemplateResponse(
        request,
        "display.html",
        {
            "key": DISPLAY_KEY,
            "code": hub.code,
            "join_url": _public_url(request),
            "has_turn": status["configured"],
            "turn_error": status["error"],
            "policy": config.ice_policy(),
        },
    )


@app.get("/healthz")
def healthz():
    """Koyeb's health check, and the request that wakes a sleeping instance."""
    return {"ok": True}


# --- for the instructor and for Claude --------------------------------------


@app.get("/api/state")
def state(key: str = ""):
    """Everything worth knowing about the room, for diagnosing a bad session."""
    _require_key(key)
    status = config.turn_status()
    snapshot = hub.snapshot()
    snapshot.update(
        {
            "displays": len(hub.displays),
            "turn_configured": status["configured"],
            "turn_source": status["source"],
            "turn_error": status["error"],
            "ice_policy": config.ice_policy(),
        }
    )
    return JSONResponse(snapshot)


# --- signalling ------------------------------------------------------------


@app.websocket("/ws/student")
async def ws_student(ws: WebSocket):
    await ws.accept()
    peer_id: str | None = None
    try:
        first = await ws.receive_json()
        given = str(first.get("code") or "").strip()
        if first.get("type") != "join" or not secrets.compare_digest(given, hub.code):
            await ws.send_json(
                {
                    "type": "error",
                    "message": "That code doesn't match the one on the classroom screen.",
                }
            )
            await ws.close()
            return

        name = (str(first.get("name") or "").strip() or "Anonymous")[:60]
        peer_id = await hub.add_peer(name, ws)
        await ws.send_json(
            {
                "type": "joined",
                "id": peer_id,
                "ice": config.ice_servers(),
                "policy": config.ice_policy(),
            }
        )

        while True:
            message = await ws.receive_json()
            kind = message.get("type")
            if kind == "ready":
                await hub.set_ready(peer_id)
            elif kind == "unready":
                await hub.set_unready(peer_id)
            elif kind == "signal":
                # Only the student the instructor put up may negotiate. Without
                # this, anyone with the code could send an offer whenever they
                # liked and take the projector from whoever is on it.
                if hub.stage == peer_id:
                    await hub.to_display(
                        {"type": "signal", "from": peer_id, "data": message.get("data")}
                    )
            elif kind == "ping":
                await ws.send_json({"type": "pong"})
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        if peer_id:
            await hub.remove_peer(peer_id)


@app.websocket("/ws/display")
async def ws_display(ws: WebSocket, key: str = ""):
    if not secrets.compare_digest(key or "", DISPLAY_KEY):
        await ws.close(code=4403)
        return
    await ws.accept()
    # The classroom end needs the same ICE servers the students get: when the
    # campus blocks the direct path, both ends have to be able to reach TURN.
    await ws.send_json(
        {"type": "config", "ice": config.ice_servers(), "policy": config.ice_policy()}
    )
    await hub.add_display(ws)
    try:
        while True:
            message = await ws.receive_json()
            kind = message.get("type")
            if kind == "stage":
                await hub.set_stage(message.get("id"))
            elif kind == "auto":
                await hub.set_auto(bool(message.get("on")))
            elif kind == "signal":
                await hub.to_peer(message.get("to"), {"type": "signal", "data": message.get("data")})
            elif kind == "path":
                await hub.record_path(message.get("path") or {})
            elif kind == "ping":
                await ws.send_json({"type": "pong"})
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        await hub.remove_display(ws)
