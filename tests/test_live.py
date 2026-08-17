"""The same end-to-end check, run against the deployed service.

The local browser test proves the code. This proves the deployment: signalling
over wss through Koyeb's edge, the ICE servers the live instance hands out, and
video actually arriving on a display page loaded over https.

    SCREENSHARE_URL=https://your-app.koyeb.app \
    SCREENSHARE_DISPLAY_KEY=... SCREENSHARE_CODE=... \
      python test_live.py

What it cannot tell you is whether the classroom will work: both browsers here
sit on one network, so they will find a direct path even when a campus would
not allow one. Only the phone check in the README answers that.
"""
import os
import sys

from playwright.sync_api import sync_playwright

from serve import Results

BASE = os.environ.get("SCREENSHARE_URL", "").rstrip("/")
KEY = os.environ.get("SCREENSHARE_DISPLAY_KEY", "")
CODE = os.environ.get("SCREENSHARE_CODE", "")
if not (BASE and KEY and CODE):
    sys.exit("set SCREENSHARE_URL, SCREENSHARE_DISPLAY_KEY and SCREENSHARE_CODE")

r = Results()

with sync_playwright() as p:
    browser = p.chromium.launch(args=[
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
    ])

    display = browser.new_context().new_page()
    display.goto(f"{BASE}/display?key={KEY}", wait_until="load")
    display.wait_for_selector("#peers li.empty", timeout=30000)
    r.check("display page loads over https", display.evaluate("() => window.isSecureContext"))
    r.check("the code on the projector matches", CODE in display.inner_text(".join-code"))
    r.check("the join address is the deployed one",
            BASE.replace("https://", "") in display.inner_text("#join-url"))

    ice = display.evaluate("() => JSON.stringify(state.ice)")
    r.check(f"live instance hands out ICE servers: {ice[:90]}", "stun:" in ice or "turn" in ice)
    r.check("TURN is configured", "turn:" in ice or "turns:" in ice)

    student = browser.new_context(permissions=["camera"]).new_page()
    errors: list[str] = []
    student.on("pageerror", lambda e: errors.append(str(e)))
    student.goto(f"{BASE}/?test=1", wait_until="load")
    student.fill("#name", "Test student")
    student.fill("#code", CODE)
    student.click("#join-form button[type=submit]")
    student.wait_for_selector("#stage:not([hidden])", timeout=30000)
    r.check("student joins through the edge", True)

    student.click("#share")
    display.wait_for_selector("#peers li.ready", timeout=30000)
    display.click("#peers li.ready button")
    display.wait_for_function(
        "() => { const v = document.querySelector('#video');"
        " return v && v.videoWidth > 0 && !v.paused; }", timeout=45000)
    size = display.evaluate("() => [video.videoWidth, video.videoHeight]")
    r.check(f"video arrives through the deployment ({size[0]}x{size[1]})", size[0] > 0)

    display.wait_for_function(
        "() => document.querySelector('#path').textContent.length > 0", timeout=20000)
    r.check(f"path: {display.inner_text('#path')} (both ends are on this network here)", True)

    student.close()
    display.wait_for_selector("#peers li.empty", timeout=20000)
    r.check("the room empties when they leave", True)
    r.check(f"no javascript errors {errors}", not errors)

    browser.close()

r.finish()
