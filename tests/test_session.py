"""Sessions: starting one, ending one, and what a dead code is worth.

The point of a session is that ending it is real. A student who was sharing is
disconnected, the code they typed stops working, and a link mailed to someone
outside the room gets them no further than the join form. This runs with
SCREENSHARE_CODE unset, the way production should, so each session mints its
own number.
"""
import asyncio
import json
import urllib.error
import urllib.request

import websockets

from serve import KEY, Results, start

PORT = 8044
BASE = f"ws://127.0.0.1:{PORT}"
HTTP = f"http://127.0.0.1:{PORT}"
r = Results()


async def recv(ws, want, timeout=5):
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if message.get("type") == want:
            return message


async def state(ws, is_open=None, timeout=5):
    """The next state that says what we are waiting for.

    A departing student and a change of session both broadcast, and they race.
    Waiting for the state that matters, rather than the next one to arrive, is
    what keeps this test from depending on that order.
    """
    while True:
        message = await recv(ws, "state", timeout)
        if is_open is None or message["open"] is is_open:
            return message


def get(path, redirect=True):
    """Returns (status, final path). Without `redirect`, the 303 itself."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None

    opener = urllib.request.build_opener() if redirect else urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(HTTP + path, timeout=5) as response:
            return response.status, response.url
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "")


async def join(code, name="Ada"):
    ws = await websockets.connect(f"{BASE}/ws/student")
    await ws.send(json.dumps({"type": "join", "name": name, "code": code}))
    return ws


async def main():
    async with websockets.connect(f"{BASE}/ws/display?key={KEY}") as display:
        await recv(display, "config")
        first = await state(display, is_open=True)
        code = first["code"]
        r.check("a session is running at boot", first["open"] is True)
        r.check("its code is four digits", len(code) == 4 and code.isdigit())

        student = await join(code)
        joined = await recv(student, "joined")
        r.check("a student can join it", bool(joined.get("id")))

        # --- Quit ---------------------------------------------------------
        await display.send(json.dumps({"type": "quit"}))
        r.check("quit tells the student", (await recv(student, "closed"))["type"] == "closed")
        r.check("quit tells the display", (await recv(display, "closed"))["type"] == "closed")
        await student.close()

        ended = await state(display, is_open=False)
        r.check("the room is empty after quit", ended["peers"] == [])
        r.check("the session reads as ended", ended["open"] is False)

        # The code that worked a moment ago is the whole point.
        dead = await join(code)
        refused = json.loads(await asyncio.wait_for(dead.recv(), 5))
        r.check("the old code is refused", refused["type"] == "error")
        r.check("and says why", "no session" in refused["message"].lower())
        await dead.close()

        # --- starting the next one ----------------------------------------
        await display.send(json.dumps({"type": "start"}))
        fresh = await state(display, is_open=True)
        r.check("start opens a session", fresh["open"] is True)
        r.check("on four fresh digits", len(fresh["code"]) == 4 and fresh["code"].isdigit())

        second = await join(fresh["code"], "Grace")
        r.check("students can join the new one", bool((await recv(second, "joined"))["id"]))
        await second.close()

        # --- the bookmark --------------------------------------------------
        status, _ = get(f"/start?key=wrong-key")
        r.check("/start needs the key", status == 404)

        # Clicked twice mid-class, it must not empty the room.
        running = fresh["code"]
        status, url = get(f"/start?key={KEY}")
        r.check("/start lands on the display page", status == 200 and "/display" in url)
        again = await state(display, is_open=True)
        r.check("/start left the running session alone", again["code"] == running)

        await display.send(json.dumps({"type": "quit"}))
        await recv(display, "closed")
        await state(display, is_open=False)
        status, location = get(f"/start?key={KEY}", redirect=False)
        r.check("/start redirects", status == 303 and "/display" in location)
        revived = await state(display, is_open=True)
        r.check("/start starts one when none is running", revived["open"] is True)


server = start(PORT, code=None)
try:
    asyncio.run(main())
finally:
    server.terminate()
r.finish()
