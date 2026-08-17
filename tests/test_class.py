"""Two students, switching between them, and surviving a server restart.

The restart is the one that matters for a hosted deployment. Koyeb redeploys on
a push, and a free instance that has scaled to zero comes back as a fresh
process holding an empty room. Everyone has to find their way back without the
instructor doing anything and without students re-picking a screen.
"""
import signal

from playwright.sync_api import sync_playwright

from serve import CODE, KEY, Results, start

PORT = 8043
BASE = f"http://127.0.0.1:{PORT}"
r = Results()


def join(context, name):
    page = context.new_page()
    page.goto(f"{BASE}/?test=1")
    page.fill("#name", name)
    page.fill("#code", CODE)
    page.click("#join-form button[type=submit]")
    page.wait_for_selector("#stage:not([hidden])")
    page.click("#share")
    return page


server = start(PORT)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
        ])

        display = browser.new_context().new_page()
        display.goto(f"{BASE}/display?key={KEY}")

        ada = join(browser.new_context(permissions=["camera"]), "Ada")
        grace = join(browser.new_context(permissions=["camera"]), "Grace")
        display.wait_for_function(
            "() => document.querySelectorAll('#peers li.ready').length === 2", timeout=15000)
        r.check("both students are ready", True)

        display.click("#peers li.ready button")  # Ada, the first to join
        display.wait_for_function("() => video.videoWidth > 0", timeout=20000)
        r.check("first student is up", "Ada" in display.inner_text("#live-name"))

        # Switching is the move an instructor makes most: one click, no take-down.
        display.click("#peers li.ready button")  # now Grace
        display.wait_for_function(
            "() => document.querySelector('#live-name').textContent.includes('Grace')",
            timeout=20000)
        display.wait_for_function("() => video.videoWidth > 0", timeout=20000)
        r.check("switching students swaps the projector", True)
        r.check("only one student is live", display.locator("#peers li.live").count() == 1)
        ada.wait_for_function(
            "() => document.querySelector('#status').textContent.includes('Paused')",
            timeout=10000)
        r.check("the student taken down is told they are paused", True)

        # --- the restart ----------------------------------------------------
        server.send_signal(signal.SIGKILL)
        server.wait()
        display.wait_for_function(
            "() => document.querySelector('#net').textContent.length > 0", timeout=15000)
        r.check("display notices the server went away", True)

        server = start(PORT)

        display.wait_for_function(
            "() => document.querySelectorAll('#peers li.ready').length === 2", timeout=40000)
        r.check("both students rejoin by themselves", True)
        r.check("no student had to re-pick a screen",
                ada.is_hidden("#share") and grace.is_hidden("#share"))

        display.click("#peers li.ready button")
        display.wait_for_function("() => video.videoWidth > 0", timeout=25000)
        r.check("video works again after the restart", True)

        browser.close()
finally:
    server.terminate()
r.finish()
