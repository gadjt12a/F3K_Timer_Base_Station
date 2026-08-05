"""The field display feed — one payload, any renderer.

Kris, 2026-08-05: *"I really do not care how we display it but it needs to be
clean, full screen, and 100% transferable to a LED Array when we get to that
stage."*

That requirement decides the whole design, and it rules out the obvious approach.
**An LED array cannot render HTML.** Build a rich web page and none of it
transfers — the panel driver would have to reimplement everything from the state
machine outwards, and the two would drift apart from the first change.

So the transferable thing is not the rendering. It is this feed.

⚠ **The payload is deliberately designed to what an LED PANEL can show**, not to
what a browser can. One big number, one colour, two short lines, a pilot list.
The web page is constrained to exactly that vocabulary, so it can never grow a
feature the panel cannot follow — which is what "100% transferable" actually
requires. If a future display needs something new, it goes in the payload first
and both renderers get it.

Consumers, present and future:

    /display              a full-screen web page (browser on anything)
    an LED array driver   HUB75 panels on a Pi Zero, an ESP32, a commercial
                          controller — reads the same JSON and paints pixels

⚠ **Nothing renders on the base station.** The Pi keeps audio cues on the beat to
within 0-3 ms ([I-33]-[I-36], three sessions of work) and a browser on the same
box is not a quiet neighbour. The renderer is always somebody else's CPU.

Colour names are **MBT's own vocabulary**, taken from the Big Timer's
`options.txt` in the Knowledge_Base:

    prep WHITE · window GREEN · landing RED · gaps MAGENTA · pilots WHITE

Pilots have been reading those colours for years. At thirty metres the colour is
what tells someone whether they are in prep or the window — long before the
digits are legible.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

log = logging.getLogger("f3k")
router = APIRouter()

# Bump ONLY for a breaking change. A fielded LED driver may be an ESP32 nobody
# wants to reflash on competition morning, so additive fields must never bump it.
FEED_VERSION = 1

# MBT's phase colours. The panel gets the NAME, not a hex value — an LED array
# and an LCD do not agree on what "green" looks like, and each renderer should
# pick the value that reads best on its own hardware.
_PHASE = {
    #  state          colour     label shown to pilots
    "IDLE":     ("WHITE",   ""),
    "CALLUP":   ("MAGENTA", "CALLING"),
    "PREP":     ("WHITE",   "PREP"),
    "WORKING":  ("GREEN",   "WORKING"),
    "LANDING":  ("RED",     "LANDING"),
}


def _mmss(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class DisplayFeed:
    """Builds the payload and pushes it to whoever is listening.

    Its own websocket, deliberately not the Run page's. That stream carries the
    CD's UI events — flights, scratches, recovered data, timer pills — and a
    display has no use for any of it. An LED driver must not have to parse an
    operator's console to find a clock.
    """

    def __init__(self) -> None:
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._clients.remove(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def snapshot(self, sm) -> dict:
        """The complete display state. A renderer that has just started, or has
        just reconnected, needs no history — this is everything."""
        state = sm.state
        colour, label = _PHASE.get(state, ("WHITE", ""))
        d = sm._loaded

        remaining = 0
        if state == "PREP":
            remaining = sm._prep_remaining
        elif state == "WORKING":
            remaining = sm._wt_remaining
        elif state == "LANDING":
            remaining = sm._land_remaining

        line1 = ""
        line2 = ""
        pilots: list[str] = []
        if d:
            line1 = f"R{d['round_no']} · {d['heat']}"
            line2 = d["task"]
            pilots = list(d["pilots"])

        return {
            "v": FEED_VERSION,
            "phase": state,
            # ⚠ Name, not hex. See the module docstring.
            "colour": colour,
            "label": label,
            "seconds": int(remaining),
            "clock": _mmss(remaining),
            # Short enough for a panel. A 128-wide LED array fits roughly this
            # much text and no more, so nothing longer may ever appear here.
            "line1": line1,
            "line2": line2,
            "pilots": pilots,
            # The last ten seconds of a phase, where MBT flashes. Computed here
            # so every renderer flashes on the same second rather than each one
            # inventing its own threshold.
            "flash": bool(remaining and remaining <= 10 and state in ("PREP", "WORKING", "LANDING")),
        }

    async def push(self, sm) -> None:
        """Send the current state to every renderer. Never raises — a display
        that has gone away must not disturb a running round."""
        if not self._clients:
            return
        try:
            msg = json.dumps(self.snapshot(sm))
        except Exception:
            log.exception("[DISPLAY] could not build the payload")
            return
        dead = []
        for ws in self._clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


feed = DisplayFeed()


def register(app, templates) -> None:
    """Wire the feed's routes onto the app."""

    @app.get("/display")
    async def display_page(request: Request):
        # ⚠ The annotation is load-bearing. Without it FastAPI reads `request` as
        # a required query parameter and the page answers 422 instead of HTML.
        """Full-screen field display. No nav, no chrome — point any browser at it
        and press F11. It is one consumer of the feed, not the feed itself."""
        return templates.TemplateResponse(request, "display.html", {})

    @app.get("/api/display/state")
    async def display_state():
        """Snapshot, for a renderer starting up or polling.

        An LED driver on a microcontroller may well prefer polling this once a
        second to holding a websocket open — so this is a first-class way to
        consume the feed, not a fallback.
        """
        return feed.snapshot(app.state.state_machine)

    @app.websocket("/ws/display")
    async def display_ws(ws: WebSocket):
        await feed.connect(ws)
        try:
            await ws.send_text(json.dumps(feed.snapshot(app.state.state_machine)))
            while True:
                # Nothing is expected from a display; this is just how a
                # disconnect is noticed.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            feed.disconnect(ws)

    app.include_router(router)
