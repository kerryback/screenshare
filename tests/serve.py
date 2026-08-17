"""Start the app the way the container does, for a test to talk to.

Every test drives its own server on its own port with a known code and display
key, so the tests can run in any order and none of them depends on something
already being up.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CODE = "4271"
KEY = "test-display-key"


def start(port: int) -> subprocess.Popen:
    env = dict(os.environ, SCREENSHARE_CODE=CODE, SCREENSHARE_DISPLAY_KEY=KEY)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--proxy-headers", "--forwarded-allow-ips=*"],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        if proc.poll() is not None:
            raise SystemExit("the server exited during startup")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            return proc
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise SystemExit(f"the server never came up on port {port}")


class Results:
    """A tally, so a test reports every failure rather than only its first."""

    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def check(self, label: str, cond: bool) -> None:
        (self.passed if cond else self.failed).append(label)
        print(("PASS  " if cond else "FAIL  ") + label, flush=True)

    def finish(self) -> None:
        print(f"\n{len(self.passed)} passed, {len(self.failed)} failed")
        sys.exit(1 if self.failed else 0)
