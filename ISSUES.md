# Issues Register

Known defects in the base station app, to be worked through over time.

Raised in the session 55 audit (2026-07-27) unless noted. IDs are stable — never
renumber them, so commits and notes can cite `I-07` and still mean this thing in
six months. Add new issues at the bottom of their priority section with the next
free ID.

**Status:** `OPEN` · `IN PROGRESS` · `FIXED (session N)` · `WONTFIX (reason)`

**Priorities**
- **P1** — corrupts or loses competition data while a comp is running
- **P2** — unhandled exception (HTTP 500) reachable from normal UI use
- **P3** — UI reports something that isn't true, or fails to update
- **P4** — polish, robustness, developer experience

How this was found: every route cross-referenced against every UI action, then
every endpoint driven in-process against a scratch DB. Wiring is clean — there
are no broken buttons. Everything below is input validation or state guards.

---

## P1 — Data integrity during a live competition

### I-01 · A live heat can be silently replaced mid-run · OPEN
`base_station/frontend/app.py:1029`, `base_station/frontend/state_machine.py:48`

`load_heat()` has no state guard. Loading a *different* heat while `state=PREP`
swaps `_loaded` out from under the running sequence and returns `ok:true`.
Confirmed by test: loaded group A, started, loaded group B — state stayed `PREP`
and the loaded group became B.

The client gate is `:disabled="state !== 'IDLE'"` on websocket-derived state, so
any tab whose socket has dropped sees a permanent `IDLE` and the lock never
engages. Client-side gating cannot fix this on its own.

**Fix:** `POST /api/run/load` returns 409 while the state machine is not IDLE.
(Was bug B2, raised session 53.)

### I-02 · Seconds field is unbounded — `1:99` silently becomes `2:39` · OPEN
`base_station/frontend/app.py:92` (`_parse_duration`)

`(int(m) * 60 + int(sec)) * 1000` never checks `sec < 60`, so a CD typo becomes a
plausible-looking wrong time instead of an error. No upper bound either:
`99999:99` is accepted and stored as 6,000,039,000 ms (69 days).

**Fix:** reject `sec >= 60`; reject durations beyond a sane ceiling.

### I-03 · Altitude accepts infinity and NaN · OPEN
Same three handlers as [I-05].

`1e400` and `inf` store as `inf`; `nan` stores as **NULL** (SQLite coerces it),
silently discarding the entry. Negative altitudes (`-10`) are accepted. An `inf`
will poison F5K bonus scoring for that pilot.

**Fix:** require a finite, non-negative float within a plausible range.

### I-04 · No check that flight duration ≤ round working time · OPEN
A 69-day flight was accepted into a 7-minute round. Nothing compares the entered
duration against `rounds.working_time_s`.

**Fix:** reject (or at least warn on) a duration exceeding working time.

---

## P2 — Unhandled exceptions (HTTP 500) reachable from the UI

### I-05 · `float(altitude_m)` is outside the try/except — three handlers · OPEN
`base_station/frontend/app.py:705` — `/results/flight/add`
`base_station/frontend/app.py:1099` — `/api/run/flight/add`
`base_station/frontend/app.py:1397` — `/api/run/altitude/set`

The duration parse immediately above each one *is* guarded; the altitude parse
isn't. A single letter in the altitude box → 500. All three share the identical
line, so fix them together.

### I-06 · `int(flight_no)` unguarded · OPEN
`base_station/frontend/app.py:706` — `/results/flight/add`. Same shape as [I-05].

### I-07 · Draw endpoints crash on any malformed body · OPEN
`base_station/frontend/app.py:1723` — `/api/draw/preview`
`base_station/frontend/app.py:1759` — `/api/draw/accept`

`body["comp_id"]` / `body["num_rounds"]` are direct subscripts and `int()` is
unguarded. Missing key, non-numeric value, empty body, and non-JSON all 500.
**Reproduced on the Pi**, not only in the local harness.

### I-08 · DB constraint violations surface as 500s · OPEN
- `/setup/competition/{ghost}/pilot/add` → `FOREIGN KEY constraint failed`
- `/setup/competition/new` with a discipline outside `F3K/F5K/MIXED` →
  `CHECK constraint failed`
- `/results/flight/add` with an unknown `pilot_id` → FK error

**Fix:** validate the referenced row exists and return 404/422 with a message.

### I-09 · Pilot CSV import 500s on a binary file · OPEN
`base_station/frontend/app.py:1846`. `csv.reader` over binary content raises.
Uploading an `.xlsx` by mistake gives a 500 rather than "that's not a CSV".

---

## P3 — Misleading success and stale screens

### I-10 · Four endpoints always report `{"ok": true}` · OPEN
`base_station/frontend/app.py:1029, 1035, 1053, 1061` — `load`, `start`,
`complete`, `uncomplete`.

The state machine refuses correctly *internally* (`start()` checks
`state != IDLE` and returns), but the API still says it worked. Loading a
nonexistent group also returns `ok:true` while quietly keeping the previous heat.
`skip` is the one that reports honestly (`ok:false`) — copy that pattern.

### I-11 · Leaderboard never refreshes when a heat is completed · OPEN
`base_station/frontend/templates/leaderboard.html:132`

It listens for message types `'complete'` and `'round'`. **Neither is ever
broadcast** — `/api/run/complete` writes to the DB and broadcasts nothing. Dead
handler, permanently stale scoreboard.

**Fix:** broadcast on complete/uncomplete, or drop the dead types.

### I-12 · Missing form fields render raw JSON error pages · OPEN
Clicking **Assign** with no pilots ticked returns a bare
`{"detail":[{"type":"missing"...}]}` instead of the page with a message. Same for
an empty duration field. Easy to hit.

**Fix:** exception handler for `RequestValidationError` that redirects back with
`?error=`, matching the existing pattern.

### I-13 · Cannot abort a heat when the websocket is down · OPEN
`base_station/frontend/templates/run.html`

ABORT is gated `:disabled="state === 'IDLE'"` and `state` only ever changes via
the websocket, so a page whose live link dropped sees a permanent `IDLE` and can
never abort. Server-side abort works correctly; this is purely the client gate.
Same root cause as [I-01].

**Fix:** poll `/api/run/status` as a fallback so `state` is never solely
websocket-derived. (Was bug B1, raised session 53.)

---

## P4 — Polish and robustness

### I-14 · Startup print crashes on a non-UTF-8 console · OPEN
`base_station/server.py:22` — the `→` raises `UnicodeEncodeError` under cp1252.
Harmless on the Pi, fatal on Windows.

### I-15 · `_fmt_date` uses glibc-only `%-d` · OPEN
`base_station/frontend/app.py:57`. The bare `except` silently returns unformatted
dates off-Linux, so it fails invisibly rather than loudly.

### I-16 · `/api/audio/status` has no error handling · OPEN
`base_station/frontend/app.py:1133`. Returns 200 on the Pi today, but if the
bluetooth stack is unavailable the whole settings audio panel 500s instead of
degrading.

### I-17 · Round creation accepts nonsense · OPEN
`working_time_m` of `0` or negative, and unknown task codes, are all accepted.

### I-18 · Inconsistent redirect status · OPEN
`/setup/pilot/{id}/rename` returns **204** where every sibling form POST returns
**303**.

### I-19 · `unittest discover` fails · OPEN
`base_station/tests/` has no `__init__.py`, so only the documented per-file
invocation works. All 63 tests pass when run that way.

### I-20 · `gs_upload.mdb` is not gitignored · OPEN
Written into `base_station/` by `/import/upload`. A real GliderScore upload
leaves an untracked binary — possibly containing competitor data — in the repo,
ready to be committed by accident.

### I-21 · API docs exposed on the public AP · OPEN
FastAPI's `/docs`, `/redoc` and `/openapi.json` are reachable from the OPS
network. Harmless in practice, but it's a surface that need not be there.

---

## Not bugs — verified during the audit

Recorded so they don't get re-raised:

- **No broken buttons.** All 28 `fetch()` calls, 26 form actions and every
  `<a href>` resolve to a real route; every Alpine `@click` resolves to a defined
  function. Several apparent hits were artefacts of the checking regex.
- **No path traversal** via `/downloads/{filename}` — Starlette normalises the
  path and it falls through to the captive-portal catch-all.
- **Duplicate custom task codes** are handled properly, with an error redirect.
- **Cascade deletes are correct** — deleting a pilot removes their flights;
  deleting a competition cleans rounds, groups, flights and group_pilots.
- **`_parse_duration` correctly rejects** negatives and zero.
- **DB restore correctly rejects** non-SQLite files.
- Pyflakes is clean, there are no TODO/FIXME markers, and there are no duplicate
  routes among the 76 defined.

## Not covered by this audit

Nothing below was tested and none of it is cleared:

- Visual and layout bugs, and anything requiring a rendered browser.
- The audio cue sequence and timing.
- The timer TCP protocol and any real timer hardware.
- Websocket reconnect behaviour under genuine network loss.

Given [I-01], [I-11] and [I-13] are all "the UI believes something the server
never confirmed", a hands-on pass on the Run page will likely find more in that
family.
