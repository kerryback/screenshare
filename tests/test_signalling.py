"""The signalling paths, without a browser: who may join, who may negotiate.

Fake SDP stands in for the real thing here. What is being checked is the
routing and the rules around it -- a wrong code gets nowhere, and a student the
instructor has not put up cannot push an offer at the projector.
"""
import asyncio
import json

import websockets

from serve import CODE, KEY, Results, start

PORT = 8041
BASE = f"ws://127.0.0.1:{PORT}"
r = Results()


async def recv(ws, want, timeout=5):
    """The next message of a given type, skipping the others."""
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if message.get("type") == want:
            return message


async def main():
    async with websockets.connect(f"{BASE}/ws/student") as bad:
        await bad.send(json.dumps({"type": "join", "name": "Mallory", "code": "0000"}))
        r.check("wrong code refused", json.loads(await bad.recv())["type"] == "error")

    try:
        async with websockets.connect(f"{BASE}/ws/display?key=nope") as display:
            await display.recv()
        r.check("bad display key refused", False)
    except Exception:
        r.check("bad display key refused", True)

    async with websockets.connect(f"{BASE}/ws/display?key={KEY}") as display:
        config = await recv(display, "config")
        r.check("display gets ICE servers", bool(config.get("ice")))
        first = await recv(display, "state")
        r.check("display opens on an empty room", first["peers"] == [] and first["code"] == CODE)

        async with websockets.connect(f"{BASE}/ws/student") as student:
            await student.send(json.dumps({"type": "join", "name": "Ada", "code": CODE}))
            joined = await recv(student, "joined")
            peer_id = joined["id"]
            r.check("student joins with the right code", bool(peer_id))
            r.check("student gets ICE servers", bool(joined.get("ice")))

            state = await recv(display, "state")
            r.check("display sees the student", any(p["name"] == "Ada" for p in state["peers"]))
            r.check("student starts as joined", state["peers"][0]["state"] == "joined")

            await student.send(json.dumps({"type": "ping"}))
            r.check("student keepalive answered", (await recv(student, "pong"))["type"] == "pong")

            # A student with nothing captured must not reach the projector: it
            # would go blank with their name on it.
            await display.send(json.dumps({"type": "stage", "id": peer_id}))
            await student.send(json.dumps({"type": "ready"}))
            state = await recv(display, "state")
            r.check("ready reaches the display", state["peers"][0]["state"] == "ready")
            r.check("nobody was staged while un-captured", state["stage"] is None)

            # Nor may a student who is merely ready push an offer through.
            await student.send(json.dumps({"type": "signal", "data": {"sdp": "hijack"}}))

            await display.send(json.dumps({"type": "stage", "id": peer_id}))
            r.check("staged student is told to send", (await recv(student, "go"))["type"] == "go")
            state = await recv(display, "state")
            r.check("state says live",
                    state["peers"][0]["state"] == "live" and state["stage"] == peer_id)

            await student.send(json.dumps({"type": "signal", "data": {"sdp": "offer"}}))
            signal = await recv(display, "signal")
            r.check("offer reaches the display", signal["data"]["sdp"] == "offer")
            r.check("the earlier hijack never arrived", signal["data"]["sdp"] != "hijack")

            await display.send(json.dumps(
                {"type": "signal", "to": peer_id, "data": {"sdp": "answer"}}))
            r.check("answer reaches the student",
                    (await recv(student, "signal"))["data"]["sdp"] == "answer")

            await display.send(json.dumps({"type": "ping"}))
            r.check("display keepalive answered", (await recv(display, "pong"))["type"] == "pong")

            await display.send(json.dumps(
                {"type": "path", "path": {"local": "relay", "relayed": True}}))
            state = await recv(display, "state")
            r.check("path is recorded", state["path"].get("relayed") is True)

            await display.send(json.dumps({"type": "stage", "id": None}))
            r.check("take down pauses the student", (await recv(student, "stop"))["type"] == "stop")

        # The take-down broadcast arrives before the departure one.
        empty = False
        for _ in range(4):
            if (await recv(display, "state"))["peers"] == []:
                empty = True
                break
        r.check("leaving empties the room", empty)


server = start(PORT)
try:
    asyncio.run(main())
finally:
    server.terminate()
r.finish()
