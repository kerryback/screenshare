"""Room state and message routing.

One classroom, one room. The hub knows who has joined, who is holding a
captured screen ready to show, and who is on the projector right now. It never
touches the video itself -- that travels browser to browser. All that passes
through here is the WebRTC handshake and a few words of state.

Because it lives in memory, the service has to run as a single instance. Two
instances would each hold half a classroom and neither would see the other.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

# A peer's state, in the order it moves through:
#   joined  -- in the room, nothing captured
#   ready   -- has picked a screen and is waiting to be shown
#   live    -- on the projector
JOINED, READY, LIVE = "joined", "ready", "live"


@dataclass
class Peer:
    id: str
    name: str
    ws: Any
    state: str = JOINED
    joined_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "state": self.state}


class Hub:
    def __init__(self, code: str) -> None:
        self.code = code
        self.auto = False
        self.stage: str | None = None
        self.peers: dict[str, Peer] = {}
        self.displays: set[Any] = set()
        self.path: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    # --- state ------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        peers = sorted(self.peers.values(), key=lambda p: p.joined_at)
        return {
            "type": "state",
            "code": self.code,
            "auto": self.auto,
            "stage": self.stage,
            "peers": [p.public() for p in peers],
            "path": self.path,
        }

    async def broadcast(self) -> None:
        message = self.snapshot()
        for ws in list(self.displays):
            await self._send(ws, message, self.displays)

    async def _send(self, ws: Any, message: dict[str, Any], bucket: set[Any] | None = None) -> bool:
        try:
            await ws.send_json(message)
            return True
        except Exception:
            if bucket is not None:
                bucket.discard(ws)
            return False

    # --- displays ---------------------------------------------------------

    async def add_display(self, ws: Any) -> None:
        self.displays.add(ws)
        await self._send(ws, self.snapshot(), self.displays)

    async def remove_display(self, ws: Any) -> None:
        self.displays.discard(ws)

    async def to_display(self, message: dict[str, Any]) -> None:
        for ws in list(self.displays):
            await self._send(ws, message, self.displays)

    # --- students ---------------------------------------------------------

    async def add_peer(self, name: str, ws: Any) -> str:
        async with self._lock:
            peer_id = secrets.token_urlsafe(8)
            self.peers[peer_id] = Peer(id=peer_id, name=name, ws=ws)
        await self.broadcast()
        return peer_id

    async def remove_peer(self, peer_id: str) -> None:
        async with self._lock:
            self.peers.pop(peer_id, None)
            cleared = self.stage == peer_id
            if cleared:
                self.stage = None
                self.path = {}
        if cleared:
            await self.to_display({"type": "gone", "peer": peer_id})
        await self.broadcast()

    async def to_peer(self, peer_id: str | None, message: dict[str, Any]) -> None:
        peer = self.peers.get(peer_id or "")
        if peer is not None:
            await self._send(peer.ws, message)

    async def set_ready(self, peer_id: str) -> None:
        peer = self.peers.get(peer_id)
        if peer is None or peer.state == LIVE:
            return
        peer.state = READY
        # With auto on, the first student to offer goes up by themselves; the
        # instructor only intervenes when someone is already showing.
        if self.auto and self.stage is None:
            await self.set_stage(peer_id)
            return
        await self.broadcast()

    async def set_unready(self, peer_id: str) -> None:
        """The student released their capture, on purpose or by closing the tab."""
        peer = self.peers.get(peer_id)
        if peer is None:
            return
        peer.state = JOINED
        if self.stage == peer_id:
            self.stage = None
            self.path = {}
            await self.to_display({"type": "gone", "peer": peer_id})
        await self.broadcast()

    # --- the projector ----------------------------------------------------

    async def set_stage(self, peer_id: str | None) -> None:
        # Someone who has not captured anything has nothing to send, and putting
        # them up would leave the projector blank with their name on it.
        if peer_id is not None and self.peers.get(peer_id, None) is not None:
            if self.peers[peer_id].state == JOINED:
                return

        previous = self.stage
        if previous and previous != peer_id:
            old = self.peers.get(previous)
            if old is not None:
                # Their capture stays alive so putting them back up is instant;
                # they see that they are paused, not being shown.
                old.state = READY
                await self._send(old.ws, {"type": "stop"})
            await self.to_display({"type": "gone", "peer": previous})

        self.path = {}
        if peer_id and peer_id in self.peers:
            self.stage = peer_id
            self.peers[peer_id].state = LIVE
            await self._send(self.peers[peer_id].ws, {"type": "go"})
        else:
            self.stage = None
        await self.broadcast()

    async def set_auto(self, on: bool) -> None:
        self.auto = on
        if on and self.stage is None:
            waiting = [p for p in self.peers.values() if p.state == READY]
            if waiting:
                await self.set_stage(sorted(waiting, key=lambda p: p.joined_at)[0].id)
                return
        await self.broadcast()

    # --- how the video is actually travelling -----------------------------

    async def record_path(self, path: dict[str, Any]) -> None:
        """Reported by the display: host/srflx means direct, relay means TURN."""
        self.path = path or {}
        await self.broadcast()
