// The student side. Capture a screen on a click, hold it, and send it only
// once the instructor puts this student up.

const $ = (sel) => document.querySelector(sel);

const form = $("#join-form");
const stage = $("#stage");
const shareBtn = $("#share");
const stopBtn = $("#stop");
const statusEl = $("#status");

// ?test=1 sends the camera instead of the screen. Same signalling, same ICE,
// same TURN -- so it proves whether video can get from this network to the
// classroom. It exists because the question "will this work from student
// wifi?" is best answered from a phone, and phones cannot capture a screen.
const TEST_MODE = new URLSearchParams(location.search).has("test");

// The hosting proxy drops an idle connection after about 100 seconds. A
// student who joins early and waits would be dropped before they ever shared.
const KEEPALIVE_MS = 25000;

const state = {
  ws: null,
  joined: false,
  name: "",
  code: "",
  ice: [{ urls: ["stun:stun.l.google.com:19302"] }],
  policy: "all",
  stream: null,
  pc: null,
  retry: 0,
  keepalive: null,
  leaving: false,
};

function status(text, tone = "") {
  statusEl.textContent = text;
  statusEl.className = "status" + (tone ? " " + tone : "");
}

function send(message) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(message));
  }
}

// --- joining ---------------------------------------------------------------

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/student`);
  state.ws = ws;

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "join", name: state.name, code: state.code }));
    if (state.keepalive) clearInterval(state.keepalive);
    state.keepalive = setInterval(() => send({ type: "ping" }), KEEPALIVE_MS);
  };

  ws.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    handle(message);
  };

  ws.onclose = () => {
    if (state.keepalive) clearInterval(state.keepalive);
    state.keepalive = null;
    if (!state.joined || state.leaving) return;
    teardownPeer();
    // Laptops sleep, wifi drops, and the server itself may restart. Come back
    // on our own, and if a capture is still held, offer it again without
    // making the student re-pick.
    if (state.retry < 60) {
      state.retry += 1;
      status("Reconnecting…", "wait");
      setTimeout(connect, 1500);
    } else {
      status("Lost the connection to the classroom. Reload to rejoin.", "bad");
    }
  };
}

function handle(message) {
  switch (message.type) {
    case "joined":
      state.joined = true;
      state.retry = 0;
      state.ice = message.ice || state.ice;
      state.policy = message.policy || "all";
      form.hidden = true;
      stage.hidden = false;
      $("#who-name").textContent = state.name;
      if (state.stream) {
        send({ type: "ready" });
        status("Waiting for the instructor to put you up.", "wait");
      } else {
        status("You're in. Click when you're ready to share.");
      }
      break;

    case "error":
      status(message.message || "Could not join.", "bad");
      break;

    case "go":
      startSending();
      break;

    case "stop":
      teardownPeer();
      if (state.stream) {
        status("Paused — your screen is captured but not being shown.", "wait");
      }
      break;

    case "signal":
      onSignal(message.data);
      break;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  state.name = $("#name").value.trim();
  state.code = $("#code").value.trim();
  if (!state.name || !state.code) return;
  status("Joining…");
  connect();
});

// --- capture ---------------------------------------------------------------

async function capture() {
  if (TEST_MODE) {
    return await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
  }

  const video = { frameRate: { ideal: 15, max: 30 } };
  const wantAudio = $("#audio").checked;
  try {
    return await navigator.mediaDevices.getDisplayMedia({ video, audio: wantAudio });
  } catch (err) {
    // Safari has no audio capture here; retry without rather than failing.
    if (wantAudio && err && err.name !== "NotAllowedError") {
      return await navigator.mediaDevices.getDisplayMedia({ video });
    }
    throw err;
  }
}

shareBtn.addEventListener("click", async () => {
  shareBtn.disabled = true;
  try {
    const stream = await capture();
    state.stream = stream;
    stream.getVideoTracks().forEach((track) => {
      // Screens are mostly text: keep it sharp rather than smooth.
      track.contentHint = "detail";
      track.addEventListener("ended", releaseCapture);
    });
    shareBtn.hidden = true;
    stopBtn.hidden = false;
    send({ type: "ready" });
    status("Waiting for the instructor to put you up.", "wait");
  } catch (err) {
    if (err && err.name === "NotAllowedError") {
      status("Nothing shared — you closed the picker or the browser blocked it.");
    } else {
      status("Could not capture your screen: " + (err && err.message ? err.message : err), "bad");
    }
  } finally {
    shareBtn.disabled = false;
  }
});

stopBtn.addEventListener("click", releaseCapture);

function releaseCapture() {
  teardownPeer();
  if (state.stream) {
    state.stream.getTracks().forEach((track) => track.stop());
    state.stream = null;
  }
  shareBtn.hidden = false;
  stopBtn.hidden = true;
  send({ type: "unready" });
  status("Stopped. Nothing is being shared.");
}

// --- WebRTC ----------------------------------------------------------------

async function startSending() {
  if (!state.stream) return;
  teardownPeer();

  const pc = new RTCPeerConnection({
    iceServers: state.ice,
    iceTransportPolicy: state.policy,
  });
  state.pc = pc;

  pc.onicecandidate = (event) => {
    if (event.candidate) send({ type: "signal", data: { candidate: event.candidate } });
  };

  pc.onconnectionstatechange = () => {
    if (pc !== state.pc) return;
    if (pc.connectionState === "connected") {
      status(
        TEST_MODE
          ? "Connected — video reaches the classroom from this network."
          : "Your screen is on the classroom projector.",
        "live"
      );
    } else if (pc.connectionState === "failed") {
      status(
        TEST_MODE
          ? "No video could get through from this network. That is what a TURN server fixes."
          : "Your video could not get through to the classroom computer. Tell the instructor.",
        "bad"
      );
    }
  };

  state.stream.getTracks().forEach((track) => pc.addTrack(track, state.stream));

  const sender = pc.getSenders().find((s) => s.track && s.track.kind === "video");
  if (sender) {
    try {
      const params = sender.getParameters();
      params.encodings = params.encodings && params.encodings.length ? params.encodings : [{}];
      params.encodings[0].maxBitrate = 3000000;
      params.degradationPreference = "maintain-resolution";
      await sender.setParameters(params);
    } catch {
      // Not every browser lets us ask; the defaults are acceptable.
    }
  }

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  send({ type: "signal", data: { sdp: pc.localDescription } });
  status("Connecting to the classroom computer…", "wait");
}

async function onSignal(data) {
  const pc = state.pc;
  if (!pc || !data) return;
  try {
    if (data.sdp) {
      await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
    } else if (data.candidate) {
      await pc.addIceCandidate(new RTCIceCandidate(data.candidate));
    }
  } catch (err) {
    console.warn("signal failed", err);
  }
}

function teardownPeer() {
  if (state.pc) {
    state.pc.onicecandidate = null;
    state.pc.onconnectionstatechange = null;
    state.pc.close();
    state.pc = null;
  }
}

window.addEventListener("pagehide", () => {
  state.leaving = true;
  teardownPeer();
  if (state.ws) state.ws.close();
});

// --- what this browser can do ---------------------------------------------

if (TEST_MODE) {
  $("#heading").textContent = "Connection test";
  $("#test-lead").hidden = false;
  shareBtn.textContent = "Send my camera";
  $("#audio").closest("label").hidden = true;
  stopBtn.textContent = "Stop";
}

const NEEDED = TEST_MODE ? "getUserMedia" : "getDisplayMedia";
if (!navigator.mediaDevices || !navigator.mediaDevices[NEEDED]) {
  form.hidden = true;
  if (!window.isSecureContext) {
    status(
      "This page has to be opened over https. Use the link on the classroom screen.",
      "bad"
    );
  } else if (TEST_MODE) {
    status("This browser has no camera access, so the test can't run here.", "bad");
  } else {
    status(
      "This browser cannot share a screen. Use Chrome, Edge, or Safari on a computer — phones and tablets can't.",
      "bad"
    );
  }
}
