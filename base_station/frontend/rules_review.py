"""Public rules-review page and its collection API.

Serves RULES.md's contents as a reviewable web page at ``/rules`` and keeps every
reviewer's answers in one JSON file, so a reviewer can carry on from any device
and their comments arrive without anyone emailing files around.

Deliberately isolated from the competition database:

- Review answers live in their own JSON file (``~/f3k_review/reviews.json`` by
  default), **not** in ``f3k.db``. This route is reachable from the public
  internet and the competition DB is not something a stray write should ever be
  able to touch. It also means the reviews survive a DB restore, and a corrupt
  review file cannot take a competition down.
- The file sits outside the repo so ``deploy-pi`` cannot overwrite it.

⚠ **A reviewer is identified by the name they type. That is not a login.** Anyone
who can reach the page can open — and overwrite — anyone else's review by typing
their name. For an invited review group that is usually fine, but it is a choice.
Set ``F3K_REVIEW_TOKEN`` for a shared passphrase, or put HTTP basic auth on
``/rules`` in nginx, which is the better place for it.

Set ``F3K_REVIEW_READONLY=1`` to freeze the review once the round of feedback is
closed: the page still reads, writes are refused.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

router = APIRouter()

_PAGE = Path(__file__).parent / "static" / "rules_review.html"
_DB = Path(os.environ.get(
    "F3K_REVIEW_DB", str(Path.home() / "f3k_review" / "reviews.json")))
_TOKEN = os.environ.get("F3K_REVIEW_TOKEN") or None
_READONLY = os.environ.get("F3K_REVIEW_READONLY") == "1"

_MAX_BODY = 512 * 1024      # a full review is a few KB
_MAX_NAME = 80
_MAX_NOTE = 4000
_MAX_RULES = 500
_MAX_REVIEWERS = 200        # bounds the file on a public endpoint

_lock = threading.Lock()    # the store is read-modify-written whole

_LABELS = {"agree": "Looks right", "change": "Needs changing", "talk": "Let's discuss"}


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def _read() -> dict:
    try:
        with _DB.open("r", encoding="utf-8") as fh:
            db = json.load(fh)
        return db if isinstance(db, dict) else {"reviews": {}}
    except FileNotFoundError:
        return {"reviews": {}}
    except (json.JSONDecodeError, OSError) as exc:
        # Never silently start from empty — that reads as "every review vanished".
        raise HTTPException(500, f"review store unreadable: {exc}") from exc


def _write(db: dict) -> None:
    """Atomic replace — a crash mid-write must not truncate everyone's reviews."""
    _DB.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_DB.parent), prefix=".reviews-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(db, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _DB)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _slug(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip().lower()


def _clean_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", name or "").strip()[:_MAX_NAME]


def _done(verdicts: dict | None) -> int:
    n = 0
    for entry in (verdicts or {}).values():
        if isinstance(entry, dict) and (entry.get("v") or (entry.get("note") or "").strip()):
            n += 1
    return n


def _clean_verdicts(raw) -> dict:
    """Keep only what the page sends.

    This endpoint is public, so the stored shape has to be whatever we allow
    here — not whatever was posted. The export renders it as Markdown and the
    page renders it as text, so arbitrary structure must never get in.
    """
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for rid, entry in list(raw.items())[:_MAX_RULES]:
        if not isinstance(rid, str) or not re.match(r"^R-\d{1,3}$", rid):
            continue
        if not isinstance(entry, dict):
            continue
        clean: dict = {}
        if entry.get("v") in ("agree", "change", "talk"):
            clean["v"] = entry["v"]
        note = entry.get("note")
        if isinstance(note, str) and note.strip():
            clean["note"] = note[:_MAX_NOTE]
        if clean:
            out[rid] = clean
    return out


def _authed(request: Request) -> bool:
    if not _TOKEN:
        return True
    if request.headers.get("X-Review-Token") == _TOKEN:
        return True
    return request.query_params.get("token") == _TOKEN


def _require(request: Request) -> None:
    if not _authed(request):
        raise HTTPException(401, "bad or missing review token")


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

# The page file is a fragment (it is also published as a Claude artifact, which
# supplies its own document shell), so wrap it here. F3K_REVIEW_API points the
# page at this router rather than the base station's own /api namespace.
_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<style>*{box-sizing:border-box}body{margin:0}</style>
<script>window.F3K_REVIEW_API = "/api/rules-review/";</script>
</head>
<body>
%s
</body>
</html>
"""


@router.get("/rules", response_class=HTMLResponse)
async def rules_page():
    try:
        return HTMLResponse(_SHELL % _PAGE.read_text(encoding="utf-8"))
    except OSError:
        raise HTTPException(500, "rules_review.html is missing from frontend/static/")


# ---------------------------------------------------------------------------
# api
# ---------------------------------------------------------------------------

@router.get("/api/rules-review/ping")
async def ping(request: Request):
    _require(request)
    return {"ok": True, "auth": bool(_TOKEN), "readonly": _READONLY}


@router.get("/api/rules-review/reviews")
async def reviewers(request: Request):
    _require(request)
    with _lock:
        db = _read()
    return {
        "ok": True,
        "reviewers": [
            {"name": rec.get("name", k),
             "count": _done(rec.get("verdicts")),
             "updated": rec.get("updated")}
            for k, rec in sorted(db.get("reviews", {}).items())
        ],
    }


@router.get("/api/rules-review/review")
async def get_review(request: Request, name: str = ""):
    _require(request)
    name = _clean_name(name)
    if not name:
        raise HTTPException(400, "name is required")
    with _lock:
        db = _read()
    rec = db.get("reviews", {}).get(_slug(name)) or {}
    return {"ok": True, "name": name, "verdicts": rec.get("verdicts", {})}


@router.put("/api/rules-review/review")
async def put_review(request: Request, name: str = ""):
    _require(request)
    if _READONLY:
        raise HTTPException(403, "this review is closed for comments")

    name = _clean_name(name)
    if len(name) < 2:
        raise HTTPException(400, "name must be at least two characters")

    body = await request.body()
    if not body or len(body) > _MAX_BODY:
        raise HTTPException(413, "body missing or too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "body is not valid JSON")

    verdicts = _clean_verdicts(payload.get("verdicts") if isinstance(payload, dict) else None)

    with _lock:
        db = _read()
        reviews = db.setdefault("reviews", {})
        key = _slug(name)
        if key not in reviews and len(reviews) >= _MAX_REVIEWERS:
            raise HTTPException(429, "too many reviewers on this document")
        reviews[key] = {
            "name": name,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "verdicts": verdicts,
        }
        _write(db)

    return {"ok": True, "saved": _done(verdicts)}


@router.get("/api/rules-review/export", response_class=PlainTextResponse)
async def export_markdown(request: Request):
    """Every response, grouped by rule rather than by reviewer.

    Grouping by person hides the thing that actually matters — where two
    reviewers disagree with each other about the same rule.
    """
    _require(request)
    with _lock:
        db = _read()
    reviews = db.get("reviews", {})

    by_rule: dict = {}
    for rec in reviews.values():
        for rid, entry in (rec.get("verdicts") or {}).items():
            by_rule.setdefault(rid, []).append((rec.get("name", "?"), entry))

    def order(rid: str) -> int:
        try:
            return int(rid.split("-")[1])
        except (IndexError, ValueError):
            return 999

    out = ["# F3K / F5K rules review — collected responses", ""]
    out.append(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    out.append(f"Reviewers: {len(reviews)}")
    out.append("")

    flagged = [r for r in by_rule
               if any(e.get("v") in ("change", "talk") for _, e in by_rule[r])]
    if flagged:
        out += ["## Rules somebody wants changed or discussed", ""]
        for rid in sorted(flagged, key=order):
            who = [n for n, e in by_rule[rid] if e.get("v") in ("change", "talk")]
            out.append(f"- **{rid}** — {', '.join(who)}")
        out.append("")

    out += ["## Every response, by rule", ""]
    for rid in sorted(by_rule, key=order):
        out += [f"### {rid}", ""]
        for who, entry in by_rule[rid]:
            out.append(f"**{who}** · {_LABELS.get(entry.get('v'), '— no verdict —')}")
            if entry.get("note"):
                out.append("")
                out += ["> " + line for line in entry["note"].strip().splitlines()]
            out.append("")
    return "\n".join(out)


@router.get("/api/rules-review/export.json")
async def export_json(request: Request):
    _require(request)
    with _lock:
        db = _read()
    return JSONResponse(db)
