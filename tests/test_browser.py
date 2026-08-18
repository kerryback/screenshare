"""One student, real browsers, real video arriving on the display page.

Uses ?test=1, which sends the camera rather than the screen -- Chromium's fake
device stands in for one. Everything past the capture call is the same code a
student's screen goes through: the same signalling, ICE, peer connection and
video element.
"""
from playwright.sync_api import sync_playwright

from serve import CODE, KEY, Results, start

PORT = 8042
BASE = f"http://127.0.0.1:{PORT}"
r = Results()

server = start(PORT)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
        ])

        display = browser.new_context().new_page()
        display.goto(f"{BASE}/display?key={KEY}")
        display.wait_for_selector("#peers li.empty")
        r.check("display shows the code", CODE in display.inner_text(".join-code"))
        r.check("display says laptops only",
                "laptop" in display.inner_text(".join-note").lower())

        student = browser.new_context(permissions=["camera"]).new_page()
        errors: list[str] = []
        student.on("pageerror", lambda e: errors.append(str(e)))
        student.goto(f"{BASE}/?test=1")
        student.fill("#name", "Ada")
        student.fill("#code", CODE)
        student.click("#join-form button[type=submit]")
        student.wait_for_selector("#stage:not([hidden])")
        r.check("student joined", "Ada" in student.inner_text("#who-name"))

        display.wait_for_selector("#peers li .name")
        r.check("display lists the student", "Ada" in display.inner_text("#peers"))

        student.click("#share")
        display.wait_for_selector("#peers li.ready", timeout=10000)
        r.check("capture marks the student ready", True)

        display.click("#peers li.ready button")
        display.wait_for_function(
            "() => !document.querySelector('#overlay').hidden", timeout=15000)
        r.check("display names who is up", "Ada" in display.inner_text("#live-name"))

        # The frames are the point. Everything above is plumbing.
        display.wait_for_function(
            "() => { const v = document.querySelector('#video');"
            " return v && v.videoWidth > 0 && !v.paused; }", timeout=20000)
        size = display.evaluate("() => [video.videoWidth, video.videoHeight]")
        r.check(f"video is arriving ({size[0]}x{size[1]})", size[0] > 0)

        student.wait_for_function(
            "() => document.querySelector('#status').className.includes('live')", timeout=15000)
        r.check("student is told they are connected", True)

        display.wait_for_function(
            "() => document.querySelector('#path').textContent.length > 0", timeout=15000)
        r.check(f"path is reported: {display.inner_text('#path')}", True)

        display.click("#clear")
        display.wait_for_function(
            "() => document.querySelector('#overlay').hidden", timeout=10000)
        r.check("take down clears the projector", True)

        display.wait_for_selector("#peers li.ready", timeout=10000)
        display.click("#peers li.ready button")
        display.wait_for_function("() => video.videoWidth > 0", timeout=20000)
        r.check("putting them back up needs no new capture", True)

        student.click("#stop")
        display.wait_for_function(
            "() => document.querySelector('#overlay').hidden", timeout=10000)
        r.check("stop sharing takes them off", True)

        student.close()
        display.wait_for_selector("#peers li.empty", timeout=10000)
        r.check("closing the tab leaves the room", True)
        r.check(f"no javascript errors {errors}", not errors)

        browser.close()
finally:
    server.terminate()
r.finish()
