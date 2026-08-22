"""Quit, with a student actually on the projector and video flowing.

The session tests cover the signalling; this covers what the two browsers do
about it. Ending a class while someone is live has to take the picture off the
projector and let go of the student's capture -- a camera light still on after
the class ended is the version of this bug that matters.

Runs with SCREENSHARE_CODE unset, so the second session mints its own number
and the test can watch the first one stop working.
"""
from playwright.sync_api import sync_playwright

from serve import KEY, Results, start

PORT = 8046
BASE = f"http://127.0.0.1:{PORT}"
r = Results()

server = start(PORT, code=None)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
        ])

        display = browser.new_context().new_page()
        display.on("dialog", lambda dialog: dialog.accept())
        display.goto(f"{BASE}/start?key={KEY}")
        display.wait_for_selector("#peers li.empty")
        first_code = display.inner_text("#join-code").strip()
        r.check(f"the bookmark opened a session ({first_code})",
                len(first_code) == 4 and first_code.isdigit())
        r.check("quit is offered", display.is_visible("#quit"))

        student = browser.new_context(permissions=["camera"]).new_page()
        errors: list[str] = []
        student.on("pageerror", lambda e: errors.append(str(e)))
        student.goto(f"{BASE}/?test=1")
        student.fill("#name", "Ada")
        student.fill("#code", first_code)
        student.click("#join-form button[type=submit]")
        student.wait_for_selector("#stage:not([hidden])")
        student.click("#share")

        display.wait_for_selector("#peers li.ready", timeout=10000)
        display.click("#peers li.ready button")
        display.wait_for_function(
            "() => { const v = document.querySelector('#video');"
            " return v && v.videoWidth > 0 && !v.paused; }", timeout=20000)
        r.check("a student is live on the projector", True)

        # --- the moment itself ---------------------------------------------
        display.click("#quit")

        display.wait_for_function(
            "() => document.querySelector('#overlay').hidden", timeout=10000)
        r.check("quit takes the picture off the projector", True)
        r.check("and drops the stream", display.evaluate("() => !video.srcObject"))

        display.wait_for_selector("#join-ended:not([hidden])", timeout=10000)
        r.check("the panel offers a new session", display.is_visible("#restart"))
        r.check("and stops offering quit", not display.is_visible("#quit"))

        student.wait_for_selector("#join-form:not([hidden])", timeout=10000)
        r.check("the student is told the session ended",
                "ended" in student.inner_text("#status").lower())
        # The student page holds its stream in a module variable, out of reach
        # here, so what is checked is the state the page moved to: capture let
        # go, sharing offered afresh. That `stop()` was called on the tracks is
        # read in sessionClosed, not observed from out here.
        r.check("the sharing controls are put away",
                not student.is_visible("#stage"))
        r.check("and reset for the next session, not left mid-share",
                student.evaluate(
                    "() => !document.querySelector('#share').hidden"
                    " && document.querySelector('#stop').hidden"))

        # The dead code is the point of the whole feature.
        student.fill("#code", first_code)
        student.click("#join-form button[type=submit]")
        student.wait_for_function(
            "() => document.querySelector('#status').textContent.includes('no session')",
            timeout=10000)
        r.check("the old code no longer works", True)

        # --- the next class -------------------------------------------------
        display.click("#restart")
        display.wait_for_selector("#join-live:not([hidden])", timeout=10000)
        second_code = display.inner_text("#join-code").strip()
        r.check(f"a new session comes up ({second_code})",
                len(second_code) == 4 and second_code.isdigit())

        student.fill("#code", second_code)
        student.click("#join-form button[type=submit]")
        student.wait_for_selector("#stage:not([hidden])", timeout=10000)
        r.check("the student rejoins without reloading", True)

        student.click("#share")
        display.wait_for_selector("#peers li.ready", timeout=10000)
        display.click("#peers li.ready button")
        display.wait_for_function("() => video.videoWidth > 0", timeout=20000)
        r.check("video works again in the new session", True)
        r.check(f"no javascript errors {errors}", not errors)

        browser.close()
finally:
    server.terminate()
r.finish()
