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

## Status (session 61, 2026-07-29)

**28 fixed · 1 WONTFIX ([I-18], misfiled — see its entry) · 0 open.**

Everything the session-55 audit raised is closed. Eight came from elsewhere and are
registered here rather than tracked separately: [I-22] reported by the user, [I-23]
from the session-57 repo review, [I-24] from the session-61 audio verification, and
[I-25]–[I-29] from running T1/T2 and a real F5K round against hardware in session 61. Each fix
carries its issue ID in a comment at the site, so the reason survives the next
reader.

⚠ **Five of the eight were found by exercising the thing, not by reading it.** The
audit that produced [I-01]–[I-21] read every route and every handler and did not
see any of them. [I-25] needed a reconnect mid-round, [I-27] needed a round to
actually end, [I-28] needed two rounds of different disciplines in sequence, [I-29]
needed a real F5K round with altitude entry, and [I-24] needed the cue schedule
dumped second by second. Static review cannot reach
this class of defect.

Locked down by `base_station/tests/test_validation.py` — 20 tests, one or more per
issue, driving the real endpoints through `TestClient` against a scratch DB. Full
suite is 120 tests and passes under `python -m unittest discover -s tests -t .`
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

### I-23 · Profiles name wavs that GliderScore itself does not ship · FIXED (session 57)
`base_station/frontend/audio.py` (`_WAV_ALIASES`, `_WAV_SUPPRESS`)

Found while vendoring the audio: six wavs are named by
`gliderscore_timer_profiles.json` but exist nowhere.

**There was nothing to fetch.** Checked against `C:\GliderScore6` — its `Audio`
folder is *identical* to what we vendored (211 files; zero present there and absent
here), and `GliderScoreData.mdb` `TimerSettings` genuinely names all six. So these
are dangling references in **GliderScore's own data**, not gaps in our extraction.
GliderScore ships correctly-named equivalents that other profiles use.

The profiles JSON is a verbatim `.mdb` extract, so corrections live in `audio.py`
rather than falsifying the extract:

| wav | resolution |
|---|---|
| `FlyingNotAllowed.wav` | → `NoFlyingAllowed.wav`. Identical announcement; every other profile names the working file. The one real gap — a standalone safety call with 25 s clear air, so nothing masked it. |
| `10.wav` | → `10Secs.wav`. Belt-and-braces: the last-10s beep substitution already catches it, but that window is computed from the *unshifted* cue time while LT cues are re-anchored to the competition's landing length. |
| `ToLand.wav` | **Suppressed.** Fires 1 s before the landing countdown beeps — exactly the clash-and-clip case voice calls were pulled for. `PilotsMustLand.wav` already makes the call 17 s earlier and the timer shows the landing clock. |
| `5MinsToStart.wav`, `WorkingTimeIn-5secs.wav`, `1001 Count down with bells.wav` | Left alone — `F3B-*`, `F3JTimer-*`, `F5JTimer-*`, `GliderTimerSpecial`. Unreachable while we only select F3K/F5K profiles, and no equivalent file exists to alias to. Revisit if those disciplines are added. |

Suppression is listed explicitly rather than left to fail through the missing-wav
path, because **a missing wav is silent, not an error** — it logs
`[AUDIO] missing wav: …` and skips. A warning that fires every round is one nobody
reads, which is precisely how the entire audio set went missing for eight sessions.

---

### I-24 · Cue countdown ran up to 3 s early · FIXED (session 61)
`base_station/frontend/audio.py` (`TimerProfile.__init__`)

Found during the T7 audio verification, by walking every profile second-by-second
through the real engine rather than listening to one.

Cue keys were anchored to the working time encoded in the **profile name**.
GliderScore's own `t`-space does not agree with those names:

| profile | name says | window actually closes | skew |
|---|---|---|---|
| `F3K-3m3m30s` | 180 | 183 | **+3 s** |
| `F3K-1m3m30s` | 180 | 183 | **+3 s** |
| `F5K-5m4m15s` | 240 | 241 | +1 s |
| `F5K-15s4m15s` | 240 | 241 | +1 s |
| the other five | — | — | 0 |

So on the two 3-minute F3K profiles — the common ones — the last-10 countdown
began at 0:07 and ended saying **"three"** as the horn sounded; it never reached
"one". `Remaining-2Mins.wav` came at 1:57. Landing was shifted identically.

`Remaining-2Mins.wav` sits at `t=63`: only `183-63` puts it on a whole 2:00, and
every other cue in that profile falls on a round number the same way. That is
what identifies 183 as the true close rather than a quirk of the countdown.

Both boundaries now come from the schedule — working closes on the first
`StartEndHorn` at `t>0`, landing ends on its last cue. `work_s`/`land_s` stay
name-derived, because `select_profile()` matches them against the competition's
configured working time (the nominal 180, not 183).

WT cues at or before the window opening are also dropped: the 3m profiles carry
`1/2/3` at `t=1..3`, which land there once the close is anchored correctly and
would talk over the open horn the engine fires itself.

Not audible as a fault in isolation — a countdown three seconds early still
*sounds* like a correct countdown, which is why hearing it was never going to
find this. `tests/test_audio_cues.py` pins the cue times per profile.

Two things this surfaced but did not change:

- `F3K-3m15m30s` has no 9/8/7/6 in its landing count (14…10, then 5…1). That is
  GliderScore's data; every cue it does have is correctly anchored.
- No profile exists for F3K at 240 s, or F5K at 180/900 s. Such a heat logs
  `no … profile for working_time=…` and runs **silently**. Not yet raised as an
  issue — it needs a decision about which task/time combinations are legal.

---

### I-25 · `pilot=0` flights were ACKed and binned · FIXED (session 61)
`base_station/server.py` (`TimerClient._attribute`)

The one hole the ACK queue can never plug, and the reason the end-of-round resend
exists at all.

A timer that reconnects mid-round loses its pilot selection, so everything it
reports afterwards arrives as `pilot=0`. That was logged and dropped — and because
**`ACK` means "received and decided", not "stored"** ([I-23] neighbour, see
`docs/PROTOCOL_ACK.md`), the timer still got its ACK, cleared the entry from its
pending queue, and believed the flight delivered. Silent loss at *both* ends.

The base was never actually ignorant: the binding is saved on eviction into
`_mac_to_pilot` and restored by MAC on JOIN. So it knows who the timer was flying
for even when the timer has forgotten. It now falls back to that and logs loudly.

With nothing to fall back to it is still dropped, at `log.error`. **Attribution is
a fallback, not a guess** — an orphan row attributed to the wrong pilot is worse
than a loud failure.

### I-26 · Every F5K altitude in a round landed on the last flight · FIXED (session 61)
`base_station/server.py` (`F3KServer.record_altitude`)

`record_altitude` took `flight_no` and ignored it, updating

```sql
WHERE id = (SELECT id FROM flights WHERE pilot_id = ? AND group_id IS ?
            ORDER BY id DESC LIMIT 1)
```

F5K altitudes are entered *after* the round, one flight at a time, with every
flight already in the DB. So `ORDER BY id DESC LIMIT 1` resolved to the same
(last) row every time: flight 1's height overwrote flight 4's, then flight 2's
did, and flights 1–3 kept none at all.

Now targets the flight the timer named, falling back to the old behaviour only
when there is no `flight_no` match (an older timer, or a flight that never
arrived) and saying so. Returns whether the value actually changed, so an
unchanged resend is not misreported as a recovery.

Found while building the resend — re-sending altitudes is pointless while they
all land on the wrong row, so this had to be fixed for that feature to mean
anything.

### I-27 · The resend created the duplicates it existed to prevent · FIXED (session 61)
`base_station/server.py` (`TimerClient._group_for`, `last_group_id`)

Shipped and caught on hardware within minutes, which is the only reason it is a
short entry rather than a corrupted competition.

The timer reconciles at the results screen. That is *always* after the base has
sent STOP and LAND and unloaded the heat, so `state_machine._loaded` is already
`None`. `record_flight` therefore computed `group_id=None`, the dedup looked for
the flight under `group_id IS NULL`, missed the originals sitting under the real
group, and inserted a full set of orphan duplicates — each one then reported to
the CD as a **RECOVERED** flight.

Observed: a 3-flight round produced rows 63/64/65 under group NULL beside the
genuine 60/61/62.

Connections now remember the group of their last flight, saved on eviction and
restored by MAC on JOIN exactly like the pilot binding, and `rc=1` copies fall
back to it. **Only `rc=1` copies get the fallback** — a live report arriving with
no heat loaded is a different situation and must not be back-dated into the
previous round.

⚠ The general lesson: anything the timer sends *after* a round ends cannot assume
the heat is still loaded. Any future end-of-round message needs the same
treatment.

### I-28 · Stale NVS altitudes leaked into the next round · FIXED (session 61, fw-v21)
`src/timer/RoundHistory.cpp` (`recordFlight`), `src/main.cpp` (`_reconcileRound`)

`startRound()` memsets the in-RAM round, but `_saveSlot()` only writes keys for
`i < count` — and `count` is 0 at that moment, so it writes **no altitude keys at
all**. `recordFlight()` then zeroed `altitudeM[i]` in RAM while persisting only
the flight key, never the altitude key. So the previous round's `r0aN` values
survived in NVS and came back on the next `load(0, …)`.

Caught when an **F3K** round reconciled `ALTITUDE flight=2 alt=20` and
`flight=3 alt=30`, left over from earlier F5K testing.

This was not introduced by the resend — **ROUND RECALL had been showing those
wrong heights all along**, on a screen nobody cross-checks against the base. The
resend merely made it visible by putting the values somewhere they could be
compared.

`recordFlight` now clears the stored altitude for the slot it fills, and
`_reconcileRound` refuses to send altitudes unless the round is F5K. Two lines of
defence deliberately: a bogus altitude silently corrupts F5K scoring, and the
bonus formula is stepped, so a wrong height does not look wrong in the results.

---

### I-29 · Every live F5K altitude was dropped and then "recovered" · FIXED (session 61)
`base_station/server.py` (`TimerClient._group_for`)

The immediate sequel to [I-27], and only findable by running a real F5K round.

F5K heights are entered on the timer **after** the round has ended — that is how
the state machine works, `ALTITUDE_ENTRY` follows `WORKING_TIME_EXPIRED`. So a
live `ALTITUDE` *always* arrives with the heat already unloaded. [I-27]'s group
fallback was gated on `rc=1`, so every live altitude resolved to `group_id=None`,
matched no flight, and was dropped:

```
<< [id=1] ALTITUDE pilot=29 flight=3 alt=23
ERROR ALTITUDE ... has no flight to attach to — DROPPED.
```

The end-of-round resend then applied it and — correctly, by its own logic —
announced each one as a **RECOVERED** loss. So the data did land, but only via
the safety net, and the CD got a spurious alert for every single flight. A
warning that fires every round is one nobody reads, which is exactly how the
audio set stayed missing for eight sessions ([I-23]).

The fallback now applies to **every** `ALTITUDE`. It stays gated for `FLIGHT`:
a live flight arriving with no heat loaded is a different situation and must not
be back-dated into the previous round.

Also stopped logging `Altitude: …` as success on the line immediately after
`DROPPED` — it now logs only when the value actually changed.

⚠ Note how [I-26], [I-27] and [I-29] stack: altitudes hit the wrong row, then the
right row via the wrong path, then the right row via the right path. Each fix
exposed the next. **F5K altitude entry had never once been run end to end against
the base station** before session 61.

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
- ~~The audio cue sequence and timing.~~ Covered in session 61 — cue *times* are
  now pinned per profile by `tests/test_audio_cues.py`, and playback through the
  A2DP transport was confirmed live. What is still uncovered is how it **sounds**:
  overlap, clipping and whether `lead_s` is right on the competition speaker.
- The timer TCP protocol and any real timer hardware.
- Websocket reconnect behaviour under genuine network loss.

Given [I-01], [I-11] and [I-13] are all "the UI believes something the server
never confirmed", a hands-on pass on the Run page will likely find more in that
family.
