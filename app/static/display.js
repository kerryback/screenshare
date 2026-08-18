// The classroom computer. Shows the join details until somebody is up, then
// the student's screen. The instructor decides who that is.

const $ = (sel) => document.querySelector(sel);

const stageEl = $("#stage");
const video = $("#video");
const peersEl = $("#peers");
const overlay = $("#overlay");
const netEl = $("#net");

// The hosting proxy closes an idle connection after about 100 seconds, and a
// classroom sits idle for far longer than that between students. A ping well
// inside that window is what keeps the display attached to the room.
const KEEPALIVE_MS = 25000;

const state = {
  ws: null,
  pc: null,
  peer: null,
  peers: [],
  stage: null,
  timer: null,
  keepalive: null,
  ice: [{ urls: ["stun:stun.l.google.com:19302"] }],
  policy: "all",
};

function send(message) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(message));
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/display?key=${window.DISPLAY_KEY}`);
  state.ws = ws;
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
    netEl.textContent = "Reconnecting to the classroom server…";
    setTimeout(connect, 1500);
  };
  ws.onopen = () => {
    netEl.textContent = "";
    if (state.keepalive) clearInterval(state.keepalive);
    state.keepalive = setInterval(() => send({ type: "ping" }), KEEPALIVE_MS);
  };
}

function handle(message) {
  switch (message.type) {
    case "config":
      state.ice = message.ice || state.ice;
      state.policy = message.policy || "all";
      break;
    case "state":
      render(message);
      break;
    case "signal":
      onSignal(message.from, message.data);
      break;
    case "gone":
      if (state.peer === message.peer) teardown();
      break;
  }
}

// --- the room --------------------------------------------------------------

const LABEL = { joined: "in the room", ready: "ready", live: "on screen" };

function render(snapshot) {
  state.peers = snapshot.peers || [];
  state.stage = snapshot.stage;
  $("#auto").checked = !!snapshot.auto;

  peersEl.innerHTML = "";
  if (!state.peers.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "Nobody has joined yet.";
    peersEl.append(li);
    return;
  }

  for (const peer of state.peers) {
    const li = document.createElement("li");
    li.className = peer.state;

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = peer.name;

    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = LABEL[peer.state] || peer.state;

    li.append(name, tag);

    if (peer.state === "live") {
      li.append(button("Take down", () => send({ type: "stage", id: null })));
    } else if (peer.state === "ready") {
      li.append(button("Show", () => send({ type: "stage", id: peer.id })));
    }
    peersEl.append(li);
  }
}

function button(label, onClick) {
  const el = document.createElement("button");
  el.type = "button";
  el.className = "ghost small";
  el.textContent = label;
  el.addEventListener("click", onClick);
  return el;
}

// --- WebRTC ----------------------------------------------------------------

async function onSignal(peerId, data) {
  if (!data) return;

  if (data.sdp && data.sdp.type === "offer") {
    teardown();
    state.peer = peerId;
    const pc = new RTCPeerConnection({
      iceServers: state.ice,
      iceTransportPolicy: state.policy,
    });
    state.pc = pc;

    pc.onicecandidate = (event) => {
      if (event.candidate) {
        send({ type: "signal", to: peerId, data: { candidate: event.candidate } });
      }
    };
    pc.ontrack = (event) => {
      video.srcObject = event.streams[0];
      stageEl.classList.add("live");
      overlay.hidden = false;
      const peer = state.peers.find((p) => p.id === peerId);
      $("#live-name").textContent = peer ? peer.name : "";
      video.play().catch(() => {});
    };
    pc.onconnectionstatechange = () => {
      if (pc !== state.pc) return;
      if (pc.connectionState === "failed") {
        $("#path").textContent = "connection failed";
        send({ type: "path", path: { state: "failed" } });
      }
    };

    await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    send({ type: "signal", to: peerId, data: { sdp: pc.localDescription } });
    watchPath(pc);
    return;
  }

  if (data.candidate && state.pc && state.peer === peerId) {
    try {
      await state.pc.addIceCandidate(new RTCIceCandidate(data.candidate));
    } catch (err) {
      console.warn("candidate failed", err);
    }
  }
}

function teardown() {
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
  if (state.pc) {
    state.pc.onicecandidate = null;
    state.pc.ontrack = null;
    state.pc.onconnectionstatechange = null;
    state.pc.close();
    state.pc = null;
  }
  state.peer = null;
  video.srcObject = null;
  stageEl.classList.remove("live");
  overlay.hidden = true;
  $("#path").textContent = "";
}

// Where the video is actually travelling. This is the answer to "is the campus
// network blocking us" -- relay means it is, and TURN is carrying the class.
const PATH_WORDS = {
  host: "direct, same network",
  srflx: "direct, through NAT",
  prflx: "direct, through NAT",
  relay: "TURN relay",
};

function watchPath(pc) {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(async () => {
    if (pc !== state.pc) return;
    let pair = null;
    const byId = new Map();
    const report = await pc.getStats();
    report.forEach((entry) => {
      byId.set(entry.id, entry);
      if (entry.type === "candidate-pair" && entry.state === "succeeded" && entry.nominated) {
        pair = entry;
      }
    });
    if (!pair) return;

    const local = byId.get(pair.localCandidateId);
    const remote = byId.get(pair.remoteCandidateId);
    const localType = local ? local.candidateType : "";
    const remoteType = remote ? remote.candidateType : "";
    const relayed = localType === "relay" || remoteType === "relay";
    const words = relayed ? PATH_WORDS.relay : PATH_WORDS[localType] || localType;
    const kbps = pair.availableOutgoingBitrate
      ? ` · ${Math.round(pair.availableOutgoingBitrate / 1000)} kbps`
      : "";
    $("#path").textContent = words + kbps;
    send({
      type: "path",
      path: { local: localType, remote: remoteType, relayed, rtt: pair.currentRoundTripTime },
    });
  }, 3000);
}

// --- controls --------------------------------------------------------------

$("#auto").addEventListener("change", (event) => {
  send({ type: "auto", on: event.target.checked });
});

$("#clear").addEventListener("click", () => send({ type: "stage", id: null }));

$("#fullscreen").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else stageEl.requestFullscreen().catch(() => {});
});

$("#unmute").addEventListener("click", (event) => {
  video.muted = !video.muted;
  event.target.textContent = video.muted ? "Unmute" : "Mute";
});

document.addEventListener("keydown", (event) => {
  if (event.key === "f") $("#fullscreen").click();
  if (event.key === "Escape" && !document.fullscreenElement) send({ type: "stage", id: null });
});

connect();
