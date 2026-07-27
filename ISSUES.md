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

## Status: all closed (session 56, 2026-07-27)

21 fixed, 1 WONTFIX ([I-18], which was misfiled — see its entry). [I-22] was
reported by the user during the same session and is registered here too. Each fix
carries its issue ID in a comment at the site, so the reason survives the next
reader.

Locked down by `base_station/tests/test_validation.py` — 20 tests, one or more per
issue, driving the real endpoints through `TestClient` against a scratch DB. Full
suite is 83 tests and passes under `python -m unittest discover -s tests -t .`
(which [I-19] made possible).

Note what that suite **cannot** reach: [I-22] was a browser refusing to submit, so
no server-side test could have caught it. The test added for it pins only that the
server accepts the new POST shape. Client-side form behaviour still needs a human.

Themes worth carrying forward:

- **Two parsers now guard every entered number.** `_parse_duration` rejects
  `sec >= 60` and anything past an hour; `_parse_altitude` rejects non-finite,
  negative and absurd values and is used by all three handlers that previously
  called bare `float()`. Adding a fourth entry point means calling these, not
  writing a fresh `float()`.
- **The server refuses; the client reports.** [I-01] and [I-13] were both "the UI
  believes something the server never confirmed". `/api/run/load` and
  `/api/run/start` now answer 409 with a reason, and the Run page polls
  `/api/run/state` whenever the websocket is down, so `state` is never solely
  websocket-derived.
- **Validation errors go back to the page they came from.** A single
  `RequestValidationError` handler redirects browser form posts to the referer
  with `?error=`/`?msg=`, and still returns JSON to `fetch()` callers.

---

## P1 — Data integrity during a live competition

### I-01 · A live heat can be silently replaced mid-run · FIXED (session 56)
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

### I-02 · Seconds field is unbounded — `1:99` silently becomes `2:39` · FIXED (session 56)
`base_station/frontend/app.py:92` (`_parse_duration`)

`(int(m) * 60 + int(sec)) * 1000` never checks `sec < 60`, so a CD typo becomes a
plausible-looking wrong time instead of an error. No upper bound either:
`99999:99` is accepted and stored as 6,000,039,000 ms (69 days).

**Fix:** reject `sec >= 60`; reject durations beyond a sane ceiling.

### I-03 · Altitude accepts infinity and NaN · FIXED (session 56)
Same three handlers as [I-05].

`1e400` and `inf` store as `inf`; `nan` stores as **NULL** (SQLite coerces it),
silently discarding the entry. Negative altitudes (`-10`) are accepted. An `inf`
will poison F5K bonus scoring for that pilot.

**Fix:** require a finite, non-negative float within a plausible range.

### I-04 · No check that flight duration ≤ round working time · FIXED (session 56)
A 69-day flight was accepted into a 7-minute round. Nothing compares the entered
duration against `rounds.working_time_s`.

**Fix:** reject (or at least warn on) a duration exceeding working time.

Fixed by rejecting, not warning (`_duration_fits_round`); a warning on the Run page
during a live round is a warning nobody reads.

Rule-checked (session 56) against FAI SC4 Vol. F3 **5.7.7** — timing stops at a
landing *or the expiry of working time*, whichever comes first. Working time is the
correct ceiling; the implementation was already right.

⚠ **Do not add the landing window to this ceiling.** 5.7.9.3 is about *validity*,
not duration. This is an inviting mistake — see the code comment on
`_duration_fits_round`.

Caveats: the check gates **manual CD entry only**, and Volume F3 has no F5K section,
so the F5K half is not primary-source confirmed.

Clause text and the fetchable rules mirror are in the local research notes
(`Spec_Sheet_URLS.md` in the timer repo), not here.

---

## P2 — Unhandled exceptions (HTTP 500) reachable from the UI

### I-05 · `float(altitude_m)` is outside the try/except — three handlers · FIXED (session 56)
`base_station/frontend/app.py:705` — `/results/flight/add`
`base_station/frontend/app.py:1099` — `/api/run/flight/add`
`base_station/frontend/app.py:1397` — `/api/run/altitude/set`

The duration parse immediately above each one *is* guarded; the altitude parse
isn't. A single letter in the altitude box → 500. All three share the identical
line, so fix them together.

### I-06 · `int(flight_no)` unguarded · FIXED (session 56)
`base_station/frontend/app.py:706` — `/results/flight/add`. Same shape as [I-05].

### I-07 · Draw endpoints crash on any malformed body · FIXED (session 56)
`base_station/frontend/app.py:1723` — `/api/draw/preview`
`base_station/frontend/app.py:1759` — `/api/draw/accept`

`body["comp_id"]` / `body["num_rounds"]` are direct subscripts and `int()` is
unguarded. Missing key, non-numeric value, empty body, and non-JSON all 500.
**Reproduced on the Pi**, not only in the local harness.

### I-08 · DB constraint violations surface as 500s · FIXED (session 56)
- `/setup/competition/{ghost}/pilot/add` → `FOREIGN KEY constraint failed`
- `/setup/competition/new` with a discipline outside `F3K/F5K/MIXED` →
  `CHECK constraint failed`
- `/results/flight/add` with an unknown `pilot_id` → FK error

**Fix:** validate the referenced row exists and return 404/422 with a message.

### I-09 · Pilot CSV import 500s on a binary file · FIXED (session 56)
`base_station/frontend/app.py:1846`. `csv.reader` over binary content raises.
Uploading an `.xlsx` by mistake gives a 500 rather than "that's not a CSV".

---

## P3 — Misleading success and stale screens

### I-10 · Four endpoints always report `{"ok": true}` · FIXED (session 56)
`base_station/frontend/app.py:1029, 1035, 1053, 1061` — `load`, `start`,
`complete`, `uncomplete`.

The state machine refuses correctly *internally* (`start()` checks
`state != IDLE` and returns), but the API still says it worked. Loading a
nonexistent group also returns `ok:true` while quietly keeping the previous heat.
`skip` is the one that reports honestly (`ok:false`) — copy that pattern.

### I-11 · Leaderboard never refreshes when a heat is completed · FIXED (session 56)
`base_station/frontend/templates/leaderboard.html:132`

It listens for message types `'complete'` and `'round'`. **Neither is ever
broadcast** — `/api/run/complete` writes to the DB and broadcasts nothing. Dead
handler, permanently stale scoreboard.

**Fix:** broadcast on complete/uncomplete, or drop the dead types.

Both: `_set_completed` now broadcasts `{"type": "complete", ...}` on each path, and
`'round'` — which nothing anywhere broadcasts — was removed from the listener so
the next reader doesn't take it for a live event.

### I-12 · Missing form fields render raw JSON error pages · FIXED (session 56)
Clicking **Assign** with no pilots ticked returns a bare
`{"detail":[{"type":"missing"...}]}` instead of the page with a message. Same for
an empty duration field. Easy to hit.

**Fix:** exception handler for `RequestValidationError` that redirects back with
`?error=`, matching the existing pattern.

The handler discriminates on `Accept: text/html` — browser form posts get the
redirect, `fetch()` callers keep getting JSON, which is what their error paths
already expect. It sends both `?error=` and `?msg=` because `/setup` reads `msg`
while `/rounds` and `/results` read `error`; one handler then covers every form on
the site without a per-page lookup table.

### I-13 · Cannot abort a heat when the websocket is down · FIXED (session 56)
`base_station/frontend/templates/run.html`

ABORT is gated `:disabled="state === 'IDLE'"` and `state` only ever changes via
the websocket, so a page whose live link dropped sees a permanent `IDLE` and can
never abort. Server-side abort works correctly; this is purely the client gate.
Same root cause as [I-01].

**Fix:** poll `/api/run/status` as a fallback so `state` is never solely
websocket-derived. (Was bug B1, raised session 53.)

### I-22 · Custom task Save did nothing for half the tasks · FIXED (session 56)
`base_station/frontend/templates/rounds.html`

Reported by the user, not found by the audit — it is invisible from the server
side, because **no request was ever made**.

Cloning a `ladder`, `targets` or `sequence` task (F3K **D, H, K, M**; F5K **A, D**)
produced a form whose "Flights that count (N)" input held `0` while carrying
`min="1"`. The field is `x-show`n only for `last_n`/`best_n`/`first_n`/`poker`, so
for those kinds it was `display:none` — and a browser will not submit a form whose
constraint-violating control cannot be focused. The click did nothing at all: no
request, no error, just a console warning. It worked for A, B, C, E, F, G, I, J, L
and N, which is why it read as intermittent.

**Fix:** conditional fields are now bound to both `x-show` and `:disabled` from one
`shows(field)` predicate, so the two can't drift. A disabled control is skipped by
validation *and* by submission, and the server's `Form()` defaults cover the fields
that don't apply to the chosen rule kind.

Worth generalising: **hiding a form control does not exempt it from validation.**
The only sound way to switch a field off is to disable it.

---

## P4 — Polish and robustness

### I-14 · Startup print crashes on a non-UTF-8 console · FIXED (session 56)
`base_station/server.py:22` — the `→` raises `UnicodeEncodeError` under cp1252.
Harmless on the Pi, fatal on Windows.

Fixed by reconfiguring `sys.stdout`/`sys.stderr` to UTF-8 at import, rather than
de-Unicoding that one line. The arrow was only the first hit — there are a dozen
em dashes and arrows in the `log.*` calls below it, and any new one would
reintroduce the same crash.

### I-15 · `_fmt_date` uses glibc-only `%-d` · FIXED (session 56)
`base_station/frontend/app.py:57`. The bare `except` silently returns unformatted
dates off-Linux, so it fails invisibly rather than loudly.

### I-16 · `/api/audio/status` has no error handling · FIXED (session 56)
`base_station/frontend/app.py:1133`. Returns 200 on the Pi today, but if the
bluetooth stack is unavailable the whole settings audio panel 500s instead of
degrading.

### I-17 · Round creation accepts nonsense · FIXED (session 56)
`working_time_m` of `0` or negative, and unknown task codes, are all accepted.

### I-18 · Inconsistent redirect status · WONTFIX (not a form POST)
`/setup/pilot/{id}/rename` returns **204** where every sibling form POST returns
**303**.

Re-examined in session 56: this one was misfiled. Rename is not a form POST — it
is called by `fetch()` from the inline editor in `setup.html:286`, which checks
`r.ok` and updates the row in place. 204 is the correct answer for that, and a
303 would be actively worse: `fetch` follows redirects, so every rename would pull
the whole `/setup` page down and throw it away. Left as is.

### I-19 · `unittest discover` fails · FIXED (session 56)
`base_station/tests/` has no `__init__.py`, so only the documented per-file
invocation works. All 63 tests pass when run that way.

### I-20 · `gs_upload.mdb` is not gitignored · FIXED (session 56)
Written into `base_station/` by `/import/upload`. A real GliderScore upload
leaves an untracked binary — possibly containing competitor data — in the repo,
ready to be committed by accident.

### I-21 · API docs exposed on the public AP · FIXED (session 56)
FastAPI's `/docs`, `/redoc` and `/openapi.json` are reachable from the OPS
network. Harmless in practice, but it's a surface that need not be there.

Fixed with `docs_url=None, redoc_url=None, openapi_url=None`. Note the paths still
answer **200** — the captive-portal catch-all claims every unrouted path — so a
status check would look like this never landed. The test asserts on the body.

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
