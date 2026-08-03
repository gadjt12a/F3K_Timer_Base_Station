"""T8 — the browser pass, as a page you can tick off while you do it.

Everything in this project that a server-side test can reach is tested. What is
left is the half no test can see: whether a control actually works when a human
clicks it in a real browser. That is what T8 has always meant, and it has been
carried on the plan for eight sessions because a paper list is easy to lose track
of halfway through.

⚠ It exists because of [I-22]: a *browser* silently refused to submit a custom
task. Every server-side test passed, the endpoint was perfect, and the feature was
unusable. Nothing but a person clicking it would ever have found that.

Design notes:

- Ticks live in their own JSON file (``~/f3k_checks/ui_checks.json``), never in
  ``f3k.db``. Same reasoning as the rules review: a checklist is scratch working,
  and it must not be able to write to a competition database. It also survives a
  DB restore, and it sits outside the repo so a deploy cannot wipe a pass in
  progress.
- The *items* are code (below, version controlled, reviewable in a diff); the
  *state* is data. Adding a check is a commit; ticking one is not.
- An item can be **pass**, **fail** or **skip**, and every one takes a note. A
  failure with no note is worth very little the next morning.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

router = APIRouter()

_STORE = Path(os.environ.get(
    "F3K_CHECKS_DB", str(Path.home() / "f3k_checks" / "ui_checks.json")))
_MAX_NOTE = 2000
_VALID = {"pass", "fail", "skip", "todo"}

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# The checks themselves
#
# `why` is not decoration. A checklist without it becomes a ritual: the tester
# clicks the thing, sees something happen, ticks the box. Knowing which bug the
# check exists to catch is what turns "it did something" into "it did the right
# thing".
# ---------------------------------------------------------------------------

SECTIONS = [
    {
        "id": "known-holes",
        "title": "The bugs that only a browser can find",
        "intro": "Each of these shipped with every server-side test passing. "
                 "They are first because they are proof the rest of this list is worth doing.",
        "checks": [
            {
                "id": "custom-task-save",
                "title": "Clone and save a custom task (D, H, K and M)",
                "how": "Rounds → Custom Tasks → clone each of D, H, K, M → change a "
                       "setting → Save. Then reopen each one.",
                "expect": "All four save and reopen with the values you set.",
                "why": "[I-22] — the browser refused to submit this form. The endpoint "
                       "was correct and every server-side test passed. Task D/H/K/M are "
                       "named because their rule shapes differ (ladder, targets, "
                       "sequence), so they exercise different form fields.",
            },
            {
                "id": "run-409-refusals",
                "title": "Run controls refuse mid-round, with a reason",
                "how": "Start a round, then try Load (another heat), ✕ Clear and "
                       "the Callup toggle.",
                "expect": "Each refuses and tells you why — never a silent no-op, "
                          "never a success message.",
                "why": "[I-01]/[I-13] — client-side gating cannot be the lock, because "
                       "a dropped socket leaves the page reading IDLE for ever.",
            },
            {
                "id": "socket-cut-fallback",
                "title": "Pull the network, keep using the Run page",
                "how": "With a heat loaded, disable the browser's WiFi (or stop the "
                       "AP) for ~10 s, then restore it.",
                "expect": "The page keeps showing the right state and recovers on its "
                          "own — no stuck countdown, no permanent 'IDLE'.",
                "why": "[I-01] again. The poll only runs while the socket is DOWN, so "
                       "this is the path that exercises it.",
            },
            {
                "id": "i63-stale-fields",
                "title": "⚠ Hunt the [I-63] shape on the other pages",
                "how": "On Results, Setup and Leaderboard: change something from a "
                       "SECOND browser tab (or another device), then watch the first "
                       "tab without reloading it.",
                "expect": "The first tab catches up on its own within a few seconds.",
                "why": "[I-63] — the Run page's poll returns early while the websocket "
                       "is HEALTHY, so any field the tick stream does not carry stopped "
                       "updating. The callup toggle looked broken for a whole afternoon. "
                       "Results, Setup and Leaderboard have their own refresh logic and "
                       "have NEVER been audited for the same shape. This is the highest "
                       "value item on the list.",
            },
        ],
    },
    {
        "id": "callup",
        "title": "Pilot callup (new, session 68)",
        "intro": "Never clicked in a browser — only driven through the API.",
        "checks": [
            {
                "id": "callup-global",
                "title": "Settings → Pilot callup toggles and sticks",
                "how": "Settings → tick 'Pilot callup before prep' → reload the page.",
                "expect": "Still ticked after the reload, and the confirmation line says on.",
                "why": "It writes to audio_config.json, not the DB. A save that does not "
                       "survive a reload is the classic failure.",
            },
            {
                "id": "callup-per-heat",
                "title": "Run → 🔊 Callup overrides one heat only",
                "how": "With the global default ON: load a heat, turn callup OFF on Run, "
                       "then load a DIFFERENT heat without running the first.",
                "expect": "The second heat comes back ON. An amber dot marks the heat "
                          "that was overridden.",
                "why": "This is exactly what [I-63] broke, and how it was found. Do it "
                       "the other way too — global OFF, force one heat ON.",
            },
            {
                "id": "callup-plays",
                "title": "The callup actually plays, in draw order",
                "how": "With callup on, start a heat and listen. Compare the order "
                       "against the draw on the Rounds page.",
                "expect": "Attention → round → group → pilots IN DRAW ORDER → task, "
                          "all before the prep clock starts.",
                "why": "Draw order came from a new DB column; competitions drawn before "
                       "it existed fall back to name order, which is correct but worth "
                       "seeing. ⚠ A pilot with no name file is skipped SILENTLY.",
            },
            {
                "id": "callup-coverage",
                "title": "Pilot name coverage is honest",
                "how": "Open /api/audio/pilot-coverage?comp_id=N for a real competition.",
                "expect": "Everyone who should have a name file is listed as covered; "
                          "anyone missing is named.",
                "why": "A silent gap in a callup is worse than a mispronounced name — "
                       "nobody notices until the pilot is not at the line.",
            },
        ],
    },
    {
        "id": "firmware",
        "title": "Firmware management (new, sessions 67-68)",
        "intro": "The API paths are hardware-verified; the buttons are not.",
        "checks": [
            {
                "id": "fw-banner",
                "title": "Run page firmware banner",
                "how": "With a timer on an older build, watch the Run page. Then load a "
                       "heat and watch it again.",
                "expect": "Names the timers, and the second line changes: updates on its "
                          "own vs held because a heat is loaded.",
                "why": "A loaded heat blocks the push, and a heat only unloads when its "
                       "round completes — so an abandoned heat holds updates all day. "
                       "The banner is the only thing that says so.",
            },
            {
                "id": "fw-update-now",
                "title": "Update now, on Run and on Settings",
                "how": "Click it with a heat loaded (should work), then during a "
                       "running round (should refuse).",
                "expect": "Pushes when idle; 409 with a reason during a round.",
                "why": "It overrides the loaded-heat hold but NOT the live-round one. "
                       "Nothing reboots a timer with a glider in the air.",
            },
            {
                "id": "fw-downgrade",
                "title": "Settings downgrade button appears only when the base is stale",
                "how": "Look at Settings with all timers current — then with a timer "
                       "AHEAD of the base if you can arrange one.",
                "expect": "Hidden normally. When shown, it confirms before doing "
                          "anything and says updating the Pi is the better fix.",
                "why": "It is the only thing in the system that sends force=1 and the "
                       "only path past the timer's own refusal to flash backwards.",
            },
        ],
    },
    {
        "id": "run-page",
        "title": "Run page",
        "checks": [
            {
                "id": "clear-vs-abort",
                "title": "✕ Clear and Abort do different things",
                "how": "Load a heat → Abort → check it is still loaded. Load → ✕ Clear "
                       "→ check it is gone.",
                "expect": "Abort keeps the heat (so you can restart a round that went "
                          "wrong); Clear puts it down.",
                "why": "Kris's ruling. Before ✕ Clear existed the only way to put a heat "
                       "down was to load a different one.",
            },
            {
                "id": "recovered-banner",
                "title": "Recovered-data banner",
                "how": "Break the timer link during a flight so the end-of-round resend "
                       "carries it (see ~/t2-link.sh), then land.",
                "expect": "Amber banner naming the pilot and the flight; dismissed by hand.",
                "why": "It must be impossible to scroll past — the flight log changing "
                       "after a round has closed is something the CD has to acknowledge.",
            },
            {
                "id": "lost-pill",
                "title": "A timer that dies shows LOST rather than vanishing",
                "how": "Switch a timer off mid-heat and wait ~90 s.",
                "expect": "It stays on screen marked LOST.",
                "why": "[I-48] — an entry disappearing is not a notification. The CD "
                       "looking away sees nothing at all.",
            },
        ],
    },
    {
        "id": "other-pages",
        "title": "Everything else",
        "checks": [
            {
                "id": "draw-wizard",
                "title": "Draw Wizard: order list, Random mode, preview → accept",
                "how": "Rounds → Draw Wizard. Try both modes, re-shuffle, then accept.",
                "expect": "The preview matches what gets written.",
                "why": "Long-standing T8 item; the wizard writes the draw order the "
                       "callup now reads.",
            },
            {
                "id": "leaderboard-unofficial",
                "title": "Leaderboard UNOFFICIAL banner on a GS-imported comp",
                "how": "Open the leaderboard for a competition imported from "
                       "GliderScore, then for one created here. Check kiosk mode too.",
                "expect": "Amber banner naming the GS comp number on the imported one, "
                          "nothing on the local one.",
                "why": "Two totals for one pilot is only a hazard if nobody labels them.",
            },
            {
                "id": "import-audio-zip",
                "title": "Import: upload a zip of the GliderScore Audio folder",
                "how": "Zip C:\\GliderScore6\\Audio and upload it on /import.",
                "expect": "Reports added / updated / already current. Re-uploading the "
                          "same zip adds nothing.",
                "why": "The offline twin of the live sync. Re-uploading the whole folder "
                       "each event is the expected way to use it, so it has to be cheap.",
            },
            {
                "id": "results-edit",
                "title": "Results edit mode: add, correct and delete a flight",
                "how": "Results → Edit on a heat → add a flight, change one, delete one.",
                "expect": "All three stick, and the scores recompute.",
                "why": "This is how a CD fixes a bad time on the day. It is also the "
                       "page with the most hand-written parsing behind it.",
            },
            {
                "id": "mobile-pilot",
                "title": "/pilot on an actual phone",
                "how": "Join F3K_OPS on a phone; the captive portal should land you there.",
                "expect": "Readable, live countdown, no horizontal scrolling.",
                "why": "The one page built mobile-first. The operator pages are known "
                       "not to be, and that is tracked separately.",
            },
        ],
    },
]

_ALL_IDS = {c["id"] for s in SECTIONS for c in s["checks"]}


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def _read() -> dict:
    try:
        with _STORE.open("r", encoding="utf-8") as fh:
            db = json.load(fh)
        return db if isinstance(db, dict) else {"checks": {}}
    except FileNotFoundError:
        return {"checks": {}}
    except (json.JSONDecodeError, OSError) as exc:
        # Never silently start from empty — that reads as "the whole pass vanished".
        raise HTTPException(500, f"check store unreadable: {exc}") from exc


def _write(db: dict) -> None:
    """Atomic replace — a crash mid-write must not truncate a pass in progress."""
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_STORE.parent), prefix=".checks-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(db, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _STORE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/api/ui-checks")
async def get_checks():
    db = _read()
    state = db.get("checks", {})
    counts = {"pass": 0, "fail": 0, "skip": 0, "todo": 0}
    for cid in _ALL_IDS:
        counts[state.get(cid, {}).get("status", "todo")] += 1
    return {"sections": SECTIONS, "state": state, "counts": counts,
            "total": len(_ALL_IDS), "build": db.get("build"), "updated": db.get("updated")}


@router.post("/api/ui-checks/reset")
async def reset_checks():
    """Start a fresh pass. The old one is not kept — a half-finished list from
    three weeks ago is worse than an empty one, because it looks current.

    ⚠ Declared BEFORE the `{check_id}` route below. FastAPI matches in
    declaration order, so with these the other way round "reset" is read as a
    check id and answered with 404 "no such check". It was, until this was tested.
    """
    with _lock:
        db = _read()
        db["checks"] = {}
        db["updated"] = _now()
        _write(db)
    return {"ok": True}


@router.post("/api/ui-checks/{check_id}")
async def set_check(check_id: str, request: Request):
    if check_id not in _ALL_IDS:
        raise HTTPException(404, "no such check")
    body = await request.json()
    status = str(body.get("status", "todo"))
    if status not in _VALID:
        raise HTTPException(400, f"status must be one of {sorted(_VALID)}")
    note = str(body.get("note", ""))[:_MAX_NOTE]

    with _lock:
        db = _read()
        db.setdefault("checks", {})[check_id] = {
            "status": status, "note": note, "at": _now(),
        }
        db["updated"] = _now()
        _write(db)
    return {"ok": True, "id": check_id, "status": status}


@router.get("/api/ui-checks/export")
async def export_checks():
    """Plain text, for pasting into a session note or an issue."""
    db = _read()
    state = db.get("checks", {})
    out = [f"T8 UI check sheet — {db.get('updated') or 'not started'}", ""]
    for sec in SECTIONS:
        out.append(f"## {sec['title']}")
        for c in sec["checks"]:
            st = state.get(c["id"], {})
            mark = {"pass": "[x]", "fail": "[FAIL]", "skip": "[--]"}.get(
                st.get("status"), "[ ]")
            out.append(f"{mark} {c['title']}")
            if st.get("note"):
                out.append(f"      note: {st['note']}")
        out.append("")
    return PlainTextResponse("\n".join(out))
