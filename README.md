# Classroom screen share

Students put their own laptop screens on the classroom projector. They open a
link, type a code, pick a screen or window, and wait; the instructor, on the
display page at the classroom computer, decides whose screen goes up.

The video never touches this server. It goes browser to browser over WebRTC —
directly when the two machines can reach each other, through a TURN relay when
the campus network keeps them apart. All the server carries is the handshake
and a short list of who is in the room, which is why a free 0.1-vCPU instance
is enough for a full class.

This version is hosted, on Koyeb. There is nothing to install on the classroom
computer and no tunnel to start: the app has a fixed https address that works
the same every week.

## How it is deployed

It runs on Koyeb, built from this repository's Dockerfile, on a paid `micro`
instance in Washington D.C. — 0.5 vCPU and 512 MB, which is far more than
signalling needs and is chosen for headroom rather than throughput.

The instance type is the one setting here that is not really about cost. A free
instance sleeps after an hour idle and cannot be told not to; a paid one at
min-scale 1 simply stays up, so the address is live when the first student
opens it and there is no cold start in front of a class.

Scaling is pinned at one instance, and has to be. The room lives in that
instance's memory, so two of them would each hold half a class and neither
would see the other.

```
koyeb service create screenshare \
  --app screenshare \
  --git github.com/kerryback/screenshare \
  --git-branch main \
  --git-builder docker \
  --git-docker-dockerfile Dockerfile \
  --instance-type micro \
  --regions was \
  --scale 1 \
  --ports 8000:http \
  --routes /:8000 \
  --checks 8000:http:/healthz \
  --checks-grace-period 8000=10 \
  --env SCREENSHARE_CODE=<code> \
  --env 'SCREENSHARE_DISPLAY_KEY={{secret.screenshare_display_key}}'
```

A push to `main` redeploys. That is safe to do mid-week and even mid-class: the
room empties for a few seconds and everyone reconnects by themselves, keeping
the screen they had already picked.

### The variables

Under Environment variables:

| variable | what it does |
| --- | --- |
| `SCREENSHARE_CODE` | the code students type, e.g. `4271`. Set it, or it changes every restart. |
| `SCREENSHARE_DISPLAY_KEY` | the secret in your `/display` URL. Set it to a long random string, as a **secret**. |
| `SCREENSHARE_CF_TURN_KEY_ID` | Cloudflare TURN key ID (see below) |
| `SCREENSHARE_CF_TURN_TOKEN` | its API token, as a **secret** |

Both of the first two matter more than they look. Without them, every restart —
a redeploy included — invents a new code and a new display key, so your
bookmarked display URL stops working and the code on the projector no longer
matches what you told the class.

Generate a display key with:

```
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

and keep it, and the TURN token, in Koyeb secrets rather than in plain
variables:

```
koyeb secrets create screenshare_display_key --value-from-stdin
koyeb service update screenshare/screenshare \
  --env 'SCREENSHARE_DISPLAY_KEY={{secret.screenshare_display_key}}'
```

### Bookmark the display page

```
https://<your-app>.koyeb.app/display?key=<SCREENSHARE_DISPLAY_KEY>
```

That is the instructor's page, and the key is the only thing protecting it —
the app is on the public internet, so where a request comes from proves
nothing. Students get the plain address, with no key and no path:
`https://<your-app>.koyeb.app`.

## TURN: the part that decides whether video arrives

Signalling is over https to Koyeb, which any network allows. The video is a
different matter: it is a direct connection between a student's laptop and the
classroom computer, and many campuses deliberately keep student wireless and
classroom machines from reaching each other. When that is the case, the only
thing that works is a relay both ends can reach — TURN.

Set one up before the first class, not during it.

In the Cloudflare dashboard: Realtime → TURN Keys → create a key. It gives a
key ID and an API token. Cloudflare does not issue fixed passwords — it mints
short-lived ones from that key, so the app fetches a set at startup and
refreshes them in the background.

Adding them is a variable change, not a rebuild:

```
koyeb secrets create screenshare_cf_turn_token --value-from-stdin
koyeb service update screenshare/screenshare \
  --env SCREENSHARE_CF_TURN_KEY_ID=<key id> \
  --env 'SCREENSHARE_CF_TURN_TOKEN={{secret.screenshare_cf_turn_token}}'
```

The instance restarts, and `/api/state` then says `"turn_source": "cloudflare"`.
Anything else means the credentials were rejected — read `turn_error`.

Cloudflare's reply includes `turns:` on 443/TCP, which is the variant that gets
through networks blocking UDP. This is Cloudflare's TURN service, not
`trycloudflare.com` tunnels; a firewall category that blocks tunnelling
services does not touch it.

Free-tier TURN is 1 TB/month. A class period of relayed video is on the order
of a gigabyte per student shown.

## Before the first class

Twenty minutes, once per room, and it answers the only question that can't be
predicted from a desk.

1. Open the display page on the classroom computer, on the projector.
2. On a phone, on the **student** wifi — not staff, not cellular — open
   `https://<your-app>.koyeb.app/?test=1`. That page sends the camera instead
   of the screen, so it works from a phone, and it exercises the same
   signalling, ICE and TURN a laptop's screen would.
3. Join with the code, tap **Send my camera**, and put it up from the display.

| what you see | what it means |
| --- | --- |
| video arrives, path says *direct* | this room needs no relay |
| video arrives, path says *TURN relay* | the campus is blocking the direct path and TURN is carrying it — working as intended |
| the phone says no video could get through | TURN is missing or not working; fix it before class |

To prove the relay itself rather than just the app, set `SCREENSHARE_FORCE_RELAY=1`,
redeploy, and repeat. Direct paths are refused, so video arriving means it came
through TURN. Take the variable off afterwards — relaying a class that doesn't
need to costs bandwidth and adds latency.

On a paid instance at min-scale 1 there is no cold start to wait out — the
service is up between classes, and the address answers immediately.

## In class

1. A student opens the address, types their name and the code, and clicks
   **Share my screen**. Their browser asks what to share; nothing leaves their
   machine until they pick.
2. They appear in the sidebar marked *ready*.
3. Click **Show** next to their name. Their screen fills the projector.
4. **Take down** clears it; clicking Show on someone else swaps directly. A
   student who has been taken down keeps their capture, so putting them back up
   is one click.

*Show the first student automatically* promotes whoever is ready when nothing
is up — useful when students present in turn.

Press `f` on the display for full screen, Escape to take down.

### Browsers

| | screen sharing |
| --- | --- |
| Chrome, Edge on any desktop OS | yes, including a single tab with its audio |
| Safari on macOS | yes; video only, no audio capture |
| Firefox | yes; no audio capture |
| iPhone, iPad, Android | no — mobile browsers cannot capture a screen |

On a Mac, the first attempt may need Screen Recording permission for the
browser in System Settings → Privacy & Security, and the browser has to be
restarted afterwards. Worth doing before class rather than during.

## When something goes wrong

The overlay on the display's video says how the connection was made: *direct,
same network*, *direct, through NAT*, or *TURN relay*. That line is usually the
whole diagnosis. For the rest:

```
https://<your-app>.koyeb.app/api/state?key=<display key>

{"code": "4271", "stage": "…", "peers": [...],
 "path": {"local": "srflx", "remote": "relay", "relayed": true},
 "turn_configured": true, "turn_source": "cloudflare", "ice_policy": "all"}
```

| what you see | what it is |
| --- | --- |
| student joins, goes *ready*, video never arrives, `turn_configured` false | the campus is blocking the direct path and there is no fallback |
| `turn_source: "cloudflare (failing)"` | the key ID or token is wrong — read `turn_error` |
| the display keeps saying *Reconnecting* | the instance is restarting, or Koyeb is down |
| a student's page says *Reconnecting* | their wifi, or the instance restarting; both recover on their own |

A restart is survivable and needs nothing from you: students reconnect by
themselves and keep the screen they already picked, so the room refills within
a few seconds. That is covered by a test.

## Working on it

```
python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt
.venv/bin/playwright install chromium

SCREENSHARE_CODE=4271 SCREENSHARE_DISPLAY_KEY=dev \
  .venv/bin/uvicorn app.main:app --port 8030 --reload
```

Then <http://127.0.0.1:8030> as a student and
<http://127.0.0.1:8030/display?key=dev> as the instructor. `127.0.0.1` is a
secure context, so screen capture works locally without https.

Each test starts its own server:

```
cd tests
../.venv/bin/python test_signalling.py   # joining, staging, the handshake
../.venv/bin/python test_browser.py      # real browsers, real video arriving
../.venv/bin/python test_class.py        # two students, switching, a server restart
```

The browser tests use `?test=1` and Chromium's fake camera, so they need no
screen and no display.

There is a fourth that points at the deployment rather than a local server —
worth running after a config change, and the quickest way to confirm TURN is
actually live:

```
SCREENSHARE_URL=https://<your-app>.koyeb.app \
SCREENSHARE_DISPLAY_KEY=<key> SCREENSHARE_CODE=<code> \
  ../.venv/bin/python test_live.py
```

It checks that TURN appears in the ICE servers the live instance hands out. It
cannot tell you whether the classroom will work: both browsers it drives sit on
one network, so they find a direct path even where a campus would not allow
one. Only the phone check answers that.

## Notes

- The room code is what keeps a leaked link out. It is on the display page, not
  in the URL, so forwarding the address to someone outside the room gets them
  nowhere.
- Sharing is always explicit: the student picks what to send in their own
  browser's picker and can stop from the page or from the browser's own sharing
  indicator.
- One screen shows at a time. That is deliberate — it is a projector.
- The app records nothing and writes nothing to disk. When the instance
  restarts, the room is empty again.
- The room might record it anyway. Lecture capture grabs the classroom
  computer's screen, so a student's shared screen goes into the recording with
  the rest of the class. Sometimes that is a feature; sometimes it is a problem,
  since a laptop can show more than its owner meant during the moment they pick
  what to share. Sharing one window rather than a whole screen solves it.
