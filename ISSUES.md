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

## Status (session 67, 2026-08-02)

**62 fixed · 1 WONTFIX ([I-18], misfiled — see its entry) · 0 open.**

[I-52]–[I-58] are the Poker picker's first contact with a human thumb. Every one
of them was invisible to the code and to the test suite, and four of the first five made
the feature **unusable rather than wrong** — the picker was shipped in session 66
marked "not yet pressed by a human", and that label turned out to be the only
honest thing in the entry.

⚠ **The lesson is about what "done" means.** [I-52] was written, correct, and
listed as Done in `SESSION_STATE.md` — it just never reached the glass, because
only the *full-redraw* path used it and the in-air path is *incremental*. That is
the third defect in this project of exactly that shape ([I-37]/[I-38] were the
others). **A render helper that is not called from `_updateRunningInc()` is not
running during a flight.**

[I-46]–[I-51] all came out of the tester's 2026-08-01 session and are all closed.
The rest of that list is feature work, tracked in `TESTER_FEEDBACK.md`.

⚠ The last two were found by *checking the source of truth* rather than by review
— [I-50] by reading the FAI clause, [I-51] by trying to make the fast-forward move
the timer. Neither would have surfaced from the code alone.

⚠ **[I-46] and [I-49] are one idea, found twice.** A launch that happened counts
as a launch and scores zero, whether the caller scratched it (land-out) or the
pilot jumped the start. Both were letting a pilot buy extra attempts on every
launch-limited task. If a third way to void a flight is ever added, it must set
`scratched` and a `void_reason` — not vanish.

⚠ **[I-46] revised [I-42] from one session earlier** and is fixed: a scratched
flight is a land-out, so it consumes a launch and scores zero rather than
disappearing. Getting that wrong handed extra attempts to a pilot on every
launch-limited task, and let a pilot un-fly a bad last flight.

Session 66 closed the three items that had been carried as "open, deliberately
unfixed" since session 62: [I-43] (the drift scan could not see systemd drop-ins),
[I-44] (dead code that looked like the horn mechanism — the follow-up [I-34] asked
for), and [I-45], which turned out **not to be a defect at all**: the 3.5 mm jack
was cured by [I-31] in the same session it was raised, and nobody re-ran the
failing case. It was carried forward three times on a wrong theory.

⚠ Two of the three were failures of *record-keeping*, not code. [I-43] made a
whole class of drift invisible to the check built to find it, and [I-45] was fixed
for three sessions while sitting in the notes as open. Both argue the same thing:
re-run the failing case before you write "still open".

### Previously (session 61, 2026-07-29)

**35 fixed · 1 WONTFIX ([I-18], misfiled — see its entry) · 0 open.**

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
suite is 151 tests and passes under `python -m unittest discover -s tests -t .`
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
- ~~No profile exists for F3K at 240 s, or F5K at 180/900 s — such a heat runs
  silently.~~ **Raised and fixed as [I-30] (session 62):** schedules are generated
  for any working time, so no ruling on legal task/time combinations is needed.

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

### I-30 · A round at any unlisted working time ran in silence · FIXED (session 62)
`base_station/frontend/audio.py` (`_generate_profile`, `select_profile`)

GliderScore ships profiles for a fixed set of working times — F3K at 3/7/10/15
minutes, F5K at 4/7/10. Any other length matched nothing, `select_profile()`
returned None, and **the entire heat ran with no audio**: no prep calls, no
minute calls, no countdown, no horns. The only trace was one log line nobody was
reading.

That is a competition-affecting defect, not a configuration gap. A CD is free to
set a 4-minute round, and the app accepts it everywhere else.

Kris's observation is what reframed it: *"the audio is the same every time … if
the round is 1 min long or 100 min long."* True, and the code already agreed in
two places:

- The last 10 s of **every** phase are already replaced with 880 Hz beeps
  (`build_schedule`), because the voice clips run over a second and clip against
  the horn. The most important part of the audio was already length-independent.
- The **landing** window was already generalised — `lt_shift` re-anchors every LT
  cue to the competition's configured landing time rather than the profile's.

So nothing was length-specific except *which* "N minutes remaining" calls fit
inside the window. Schedules are now generated for any prep/work/land when no
profile matches: each threshold that falls strictly inside its window, then the
countdown. A 40 s round goes straight to 30/20; a 30-minute one starts calling at
10 minutes, which is as far as the vocabulary goes.

**A real profile always wins when one matches.** It is the cadence pilots know,
and it carries `TestFlyingStartsIn` / `TestFlyingEnding` / `NoFlyingAllowed` —
when practice flying is permitted during prep. That is competition *rules*, not
timing, so it is deliberately not invented. A generated schedule gives timing
calls only, and the log line says so.

This closes the open question left by [I-24] about which task/time combinations
are legal: it no longer matters, they all work.

---

### I-31 · Audio output was pinned by a hidden systemd drop-in · FIXED (session 62)
`/etc/systemd/system/f3k-server.service.d/audio.conf` (removed),
`base_station/frontend/audio_control.py` (`output_mode`, `set_output`)

There was no way to choose the audio output in the app. It was inferred from
whether a `bt_mac` happened to be saved — and even that was overridden by a
hand-added drop-in on the field Pi, dated **10 July**:

```
[Service]
Environment="F3K_AUDIO_DEVICE=bluealsa:DEV=C0:28:8D:74:69:FD,PROFILE=a2dp"
```

`output_device()` checks `F3K_AUDIO_DEVICE` before anything else, so for three
weeks every cue went to one specific WONDERBOOM by MAC. Editing
`audio_config.json` did nothing, and the config on disk was simply a lie about
where sound was going. Found only because a 3.5 mm speaker was plugged in and the
app kept insisting the output was Bluetooth.

Two things let it hide that long:

- **The drop-in is invisible in the unit file.** `cat` on
  `f3k-server.service` shows nothing; only `systemctl cat` reveals the `.d`
  directory. The first removal attempt edited the main unit and silently changed
  nothing.
- **`setup/apply-system-config.sh` does not watch `/etc/systemd/system`.** It
  covers `/etc/hostapd`, `/etc/dnsmasq.d`, `/etc/nftables.d` and
  `/etc/NetworkManager/conf.d`. A unit drop-in is exactly the kind of "quick fix
  on the Pi" that the drift check exists to catch, and it was out of scope.
  ⚠ Still true — worth adding.

Output is now an explicit setting: **3.5 mm jack / USB audio / Bluetooth**,
selected in Settings and stored as `output`. Cards are resolved by NAME, never by
index — on this Pi the jack is card 0, the two HDMI outputs are 1 and 2, and USB
audio is 3, so anything assuming "USB is card 1" would play to an unplugged HDMI
port. Volume follows the selection, because `amixer -D bluealsa` only exists
while an A2DP transport is up and silently did nothing on the jack.

`F3K_AUDIO_DEVICE` still wins — it is a genuine developer escape hatch — but the
settings page now shows a red warning naming it when set, rather than displaying
a selection that has no effect.

**Volume percentages must be MAPPED (`amixer -M`), not raw.** A hardware mixer's
percentage is linear across its *raw* range, and the Pi's jack control runs
-102.39dB..+4dB — so the saved "20%", perfectly sensible on bluealsa's linear
softvol, landed at **-81dB** on the jack: playing, `aplay` returning 0, and
completely inaudible. The first test on the 3.5 mm speaker produced silence for
exactly this reason. With `-M`, 20% is -37.94dB and 75% is -3.50dB, so the slider
means roughly the same thing on every output. bluealsa is left alone — its
softvol is already linear and `-M` would shift levels tuned by ear.

⚠ **`lead_s` is per-output in reality but stored globally.** 0.8 s was tuned for
A2DP latency; on a cable it fires every cue 0.8 s early. Re-tune after switching.

---

### I-32 · USB audio played every cue 3x too fast · FIXED (session 62)
`base_station/frontend/audio_control.py` (`ensure_alsa_config`, `_slave_rate`)

The Jabra SPEAK 510 advertises `RATE: [8000 48000]` but actually runs at 48000
whatever it is asked for. Our cues are 16 kHz, so ALSA saw a device that claimed
16 kHz support, did no conversion, and the speaker consumed the samples three
times too quickly.

Measured against a 1.54 s file:

| output | time |
|---|---|
| 3.5 mm jack | 1.638 s |
| USB (before) | **0.557 s** — 3x fast |
| USB (after) | 1.636 s |

Confirmed by ear as well: a 48 kHz copy said "30 seconds to start" correctly, the
16 kHz original chipmunked.

**This is worse than silence.** A silent heat is obvious. A heat where every
announcement is an unintelligible chirp sounds like equipment working badly, and
a CD could waste a round on it before realising.

`plughw:` cannot express "resample even though the device claims it can take this
rate", and a rate cannot be pinned inline in a device string — ALSA rejects
`plug:{slave.pcm ...}` from the command line. It needs a named PCM, so the app now
generates one in `~/.asoundrc` between markers, keyed to the detected card and to
a rate the device really reports. Anything else in that file is preserved.

Generated rather than hand-written deliberately: a hand-added audio override in a
systemd drop-in is what made the output setting a lie for three weeks [I-31], and
a hand-made `.asoundrc` would hide exactly as well. If it cannot be written the
code degrades to `plughw:` — wrong speed, but not silence.

The service runs as `User=pi`, so `~/.asoundrc` does apply to it. If that ever
changes to `root`, this breaks silently.

---

### I-33 · Timer's working-time alerts fired a full second early · FIXED (session 62, fw-v22)
`src/timer/WorkingTime.cpp` (`update`)

```cpp
int currentSec = (int)(_remainingMs / 1000);   // truncates
```

`_remainingMs / 1000` rounds down, so `currentSec` became 30 the instant remaining
dropped below 31.000 s — i.e. with **30.999 s** left. Every working-time alert
fired a second before its mark: 30, 20, 15 and the whole 10..1 countdown.

In a real round the caller's timer counts "3, 2, 1" while a full second of working
time is still on the clock, and the base station's close horn lands *after* the
timer has already said zero.

Measured by logging the base station's cue dispatch (`[AUDIO-T]`) and comparing it
against the timer's serial log on the same wall clock:

| cue | before | after |
|---|---|---|
| 30 s remaining | **-985 ms** | +30 ms |
| 20 s remaining | **-993 ms** | +25 ms |
| 10 s remaining | **-986 ms** | +32 ms |

Prep measured +7 ms and the landing window emits no timer tones at all, so only
working time sounded wrong — which is exactly why it was first mistaken for audio
latency worth tuning `lead_s` for. It was not: the base's dispatch is exact to
0-3 ms in all three phases. **A whole-second error hid as a "slightly out of sync"
feeling** because the beeps are one second apart and both devices were beeping.

Uses a ceiling for the alert second only. `getRemaining()` is deliberately left
alone — it drives the display and the arc, and changing what is on screen is not
part of fixing what is heard.

The residual ~30 ms is the timer's own dispatch plus ~47 ms of ES8311 amp-enable
per tone. `lead_s` cannot close it (it only moves the speaker *earlier*), and it
does not matter: two independent devices metres apart, and sound covers 30 m in
the time under discussion.

### I-34 · Generated rounds had no start horn · FIXED (session 62)
`base_station/frontend/audio.py` (`_generate_profile`)

A generated schedule signalled the **close** of the working window but never the
open. The round simply began in silence — no launch signal, the single most
important cue in a heat — with the first sound 30 s later.

Real GliderScore profiles carry a 1 s 1000 Hz tone at `WT t=0` and are unaffected,
so this arrived with [I-30] and only ever hit generated schedules.

Two things made it easy to write:

- `AudioEngine.horn()` exists to fire the window horns and **is called from
  nowhere at all**. `TimerProfile`'s comment claiming "the engine fires the
  window-open/close horns explicitly at the phase boundaries" describes something
  that does not happen, and that comment is what the generator was written
  against.
- The close horn *did* work, which made the open look like it should too. It only
  worked because `build_schedule()` drives a heat from the RAW cue list, not the
  bucketed tables the comment is attached to.

⚠ `horn()` is still dead code. Either wire it up or delete it — leaving a method
that looks like the mechanism, next to a comment saying it is the mechanism, is
how this happened.

---

### I-35 · Every timer beep paid a 50 ms amp power-up · FIXED (session 63, fw-v23)
`src/audio/Tones.cpp` (`_ampEnable`, `holdAmp`), `src/main.cpp`

Powering the ES8311 amp costs 50 ms of settling, and `_playToneBlocking()` dropped
the amp again as soon as each tone finished — so every single beep paid it:

```
20:57:44.763  [TONE] Playing tone: 880 Hz
20:57:44.812  [TONE] Amp enabled          <- 49 ms, before a sample is written
```

That was roughly half the audible gap between the timer's beeps and the base
station's speaker.

The amp is now held up while a round is live (`_roundLive()`, hooked to the same
state-transition point as the end-of-round reconcile), so only the first tone of
the round pays the settle:

```
21:25:53.264  [TONE] Playing tone: 880 Hz
21:25:53.266  [TONE] Amp enabled          <- 2 ms
```

`holdAmp()` deliberately does **not** power the amp up itself: `_ampEnable(true)`
blocks for 50 ms and `holdAmp` is called from the main loop, which must not stall.
The first tone enables it from the tone task, where blocking is already the norm.

No hiss with the amp held — checked on the bench, since some amps do hiss when
powered with no signal. If a future board does, narrow the hold to the final-10
countdown windows.

⚠ **`[TONE] Amp enabled` is printed unconditionally**, whether or not the amp
actually changed state, so counting those lines does not show whether the hold is
working. The gap between `Playing tone` and it is the real signal.

### I-36 · The base station's audio latency was its own ALSA buffer · FIXED (session 63)
`base_station/frontend/audio_control.py` (`buffer_args`)

`aplay`'s default buffer is several hundred ms and it does not start playing until
the buffer fills, so the whole thing sat in front of every cue. Measured against a
file of known length on the field Pi:

| buffer | overhead |
|---|---|
| aplay default | **+95 ms** (±4) |
| `--buffer-time=60000` | **+16 ms** |

No underruns at 60 ms or even 30 ms across a full set of cues, so 60 ms is taken
with margin — the Pi is also serving the web UI and the timer TCP link during a
round.

**This is why swapping speakers does not need re-tuning.** The latency was never
the speaker's; it was ours. With the buffer pinned, output latency is dominated by
our own setting rather than by the device, so any jack or USB output lands in the
same place.

⚠ **Deliberately not applied to Bluetooth.** A2DP has its own transport buffering
that a small ALSA buffer cannot shorten, and squeezing it there risks dropouts for
no gain. Bluetooth remains the one output that genuinely needs `lead_s` tuned per
speaker.

Latency removed rather than compensated: `lead_s` stays at **0**. A number you
delete is exact; a number you compensate with has to be re-guessed for every
device.

### I-37 · Waking the screen repainted only the battery · FIXED (session 64, fw-v24)
`src/display/UI.cpp` (`blank`), `include/config.h` (comment)

Found running **T3** on hardware. With the screen blanked on IDLE, a press brought
the panel back showing the battery indicator alone — no GLIDE title, no timer ID,
on an otherwise black screen.

There are two independent "what is on screen" caches, and waking reset only one:

| cache | reset on wake? | what it controls |
|---|---|---|
| `main.cpp::_lastState` | yes, by `_wakeScreen()` | whether `UI::render()` is *called* |
| `UI.cpp::_prevState` | **no** | what `render()` actually *draws* |

So `render()` was correctly called, but inside it `_prevState` still claimed IDLE
was on the panel. `screenChanged` and `connChanged` both came out false and IDLE
fell to its incremental branch, which redraws the battery and only when the
percentage differs.

`UI::blank()` now invalidates the whole incremental cache (`_prevState`,
`_prevConnState`, `_prevBatteryPct`, `_prevWtSecs`, `_prevFlashSecs`,
`_prevAltFlightNo`, `_prevPrepDs`, `_arcVisible`), so the next render takes the
`screenChanged` path — `_clearScreen()` plus a full draw — for **every** screen.

⚠ **IDLE was not the only screen affected**, just the one T3 happened to sit on.
The running screens would have shown the same failure in a worse place: with
`screenChanged` false they take `_updateRunningInc()`, painting bare digits onto
black with no arc and no flight log. Fixing this in `blank()` rather than in the
IDLE branch is what covers those.

The pattern to hold to: **anything that changes the panel behind the UI object's
back has to tell it.** `blank()` wrote pixels the cache knew nothing about, which
is the whole bug. Two more instances of it turned up the same evening — see
[I-38], which is why the reset now lives in one shared helper rather than in
`blank()` alone.

Also corrected a stale comment at `config.h:45` claiming sleep "only ever applies
in STATE_IDLE, so it cannot blank a live round" — untrue since `_screenMaySleep()`
was generalised; the serial log shows it blanking in `state=4`.

### I-38 · OTA and round-recall screens left stale pixels on exit · FIXED (session 64, fw-v25)
`src/display/UI.cpp` (`renderOtaCheck`, `renderHistory`, `_invalidateCache`), `src/display/UI.h`

The same defect as [I-37] in two more places, found by inspection while fixing it.

`renderOtaCheck()` and `renderHistory()` are reached through their own branches in
`_doRender()` and never go through `UI::render()`. Both `_clearScreen()` and draw a
completely different screen, and neither touches `_prevState`. So on leaving either
one for IDLE, `render()` compared IDLE against a `_prevState` that still said IDLE,
found `screenChanged == false`, and took the incremental branch — battery only.

The visible result is a timer that looks hung: the FIRMWARE or ROUND RECALL screen
stays on the panel while the state machine is already back in IDLE, ticking `[DBG]`
and answering `PONG` normally. **The serial log is the tiebreaker** — if `state=`
disagrees with what is on the glass, the pixels are stale and the timer is fine.

Fixed by giving all three paths one `_invalidateCache(AppState nowShowing)` helper,
each recording what it actually left on the panel: `blank()` passes 255 (nothing),
the other two pass their own state. Doing it in a shared helper is the point — this
is now the third instance, and a fourth screen added later gets it right by using
the same call.

⚠ **Not the same thing as a failed OTA check.** A check started before WiFi
associates genuinely fails, and looks identical from the outside. On the night this
was found the timer reached the OTA screen at `22:13:18` with the radio not up until
`22:13:24` — six seconds early. That case is [I-39], and the status for it already
existed — `OTA_NO_WIFI` / red `NO WIFI`. What was missing was any way back out of it.

### I-39 · OTA screen blanked mid-decision, and NO WIFI was a dead end · FIXED (session 64, fw-v26)
`src/main.cpp` (screen-sleep activity, `STATE_OTA_CHECK`), `src/ota/OtaUpdater.{h,cpp}` (`retryIfWifiReturned`), `src/display/UI.cpp` (hint)

Two separate defects on the firmware screen, both found while verifying T4.

**a · It blanked while asking a question.** Only `OTA_CHECKING` and `OTA_DOWNLOADING`
counted as activity, so once the check settled on `UPDATE AVAILABLE` nothing kept the
screen alive and it went black after 2 min with the offer still open:

```
22:12:06.301  [MAIN] Screen asleep (state=7)
```

`OTA_AVAILABLE` now counts too. The terminal states (`UP_TO_DATE`, `FAILED`,
`NO_WIFI`, `IDLE`) are deliberately still blankable — this screen has **no auto-exit**,
so a timer parked on it overnight would otherwise ghost the panel, which is the whole
reason screen sleep exists.

**b · `NO WIFI` was terminal.** `check()` runs once, on entry via `_exitHistory()`.
The screen is reachable in far less than the ~20 s the radio needs after a boot, so
arriving early left a red `NO WIFI` that never cleared — and the only way to re-check
was to exit and walk the four settings holds again. Measured on the night: screen at
`22:13:18`, WiFi associated `22:13:24`, TCP `22:13:29`.

`retryIfWifiReturned()` re-fires the check as soon as WiFi associates. It lives in
`OtaUpdater` rather than `main.cpp` so the WiFi dependency stays where the rest of the
radio knowledge already is, and it is a no-op unless the status is actually
`OTA_NO_WIFI`, so it is safe to call every loop. **L also re-checks manually**, which
covers `OTA_FAILED` (base unreachable, or serving something unparseable) — that had
the same dead end and no auto-recovery is possible for it. The screen shows
`L = RETRY` on both, since an invisible affordance is not one.

⚠ ~~The version check still cannot tell an upgrade from a downgrade~~ — **fixed as
[I-41]** the same session, once the decision was made: a timer must refuse the
downgrade *and* the base station must admit it is the stale one.

### I-40 · OTA screen stuck on CHECKING — the status was sampled twice · FIXED (session 64, fw-v28)
`src/main.cpp` (`_doRender`, `STATE_OTA_CHECK` branch)

The firmware screen would sit on `CHECKING…` indefinitely. It was never a hang: the
check completes in **30 ms**, proven once `OtaUpdater` had logging ([I-39] work):

```
22:50:26.925  [BTN] A (PWR) clicked
22:50:26.927  [OTA] Check start, free heap=253156, stack hwm=7676
22:50:26.945  [OTA] GET .../ota/version.json -> 200
22:50:26.950  [OTA] Payload (71 bytes): {"version":"fw-v27",...}
22:50:26.953  [OTA] Available=fw-v27 running=fw-v27 -> UP_TO_DATE
22:50:26.955  [OTA] Check done, status=2, stack hwm=6248
```

`_doRender()` read the status **twice**:

```cpp
g_ui.renderOtaCheck(g_ota.getStatus(), ...);   // read 1 -- draws CHECKING
_lastOtaStatus = g_ota.getStatus();            // read 2 -- records UP_TO_DATE
```

`_status` is `volatile`, written by the check task on another core, and a full-screen
clear+draw+flush on the 466×466 canvas takes tens of ms — longer than the check. So the
status routinely changed *between the two reads*: the screen drew `CHECKING` while
`_lastOtaStatus` recorded `UP_TO_DATE`. `_needsRender()` then compared
`UP_TO_DATE != UP_TO_DATE`, returned false, and never repainted again.

**A race, not a hang** — which is why it was intermittent all evening. When the status
happened to resolve before the first read, both reads agreed and the screen was right;
that is why the successful fw-v24 update earlier the same night looked fine.

Fixed by sampling once into a local and using that one value for both the draw and the
record.

⚠ **The general rule: never read a `volatile` twice in a render path.** Anything shared
with a FreeRTOS task must be sampled once per frame, or what is drawn and what is
recorded as drawn can disagree — and the incremental-render gate then locks the stale
frame in permanently. This is the same failure family as [I-37]/[I-38] (the cache
believing something that is not on the glass), reached by a different route.

⚠ **The 4096-byte check-task stack was NOT the cause**, though it was suspected and
raised to 8192 in fw-v27. `stack hwm=6248` of 8192 shows under 2 KB was ever used. The
larger stack is harmless insurance; the race was the defect. Recorded so the stack size
is not "re-fixed" later.

### I-41 · A stale base station would offer to downgrade a timer · FIXED (session 64, fw-v29)
`src/ota/OtaUpdater.cpp` (`_fwNum`, `_checkTask`), `include/config.h` (`OTA_BASE_OLDER`), `src/display/UI.cpp`, `base_station/frontend/app.py` (`_fw_num`, `api_timers`), `base_station/frontend/templates/settings.html`

The check was `strcmp(ver, FW_VERSION) != 0` — **any** difference read as "an update is
available". So a base station holding an old `firmware.bin` would offer to take a timer
*backwards*, with nothing on either screen to say so. Hit for real in this session: the
Pi sat on fw-v21 while the device ran fw-v23.

**The field failure this prevents:** a CD updates the timers, forgets the Pi, and then
"updates" a timer straight back onto older firmware in the middle of a competition —
silently undoing whatever the update was for.

Fixed on **both** sides, because they fail differently:

| side | behaviour |
|---|---|
| **Timer** | Only a *strictly newer* build is an update. Equal → `UP TO DATE`. Older → new `OTA_BASE_OLDER`, shown as orange `BASE IS OLDER` / `UPDATE THE BASE`. |
| **Base** | `/api/timers` serves `fw_state` per timer (`current`/`behind`/`ahead`/`unknown`) plus `ota_version` and `base_firmware_stale`; the Settings page shows an orange banner. |

The refusal is **enforced, not merely displayed** — `startUpdate()` already gated on
`OTA_AVAILABLE`, so an `OTA_BASE_OLDER` screen cannot be made to flash anything.

⚠ **The comparison must be numeric.** `"fw-v9"` sorts *above* `"fw-v28"` lexically, so
comparing the version strings would have been **worse than the equality test it
replaces** — it would mark a genuinely out-of-date timer as current. Pinned by
`tests/test_fw_version.py::test_ordering_is_numeric_not_lexical`.

⚠ **Unparseable versions fall back to the old "any difference means available"**, so a
future naming scheme cannot silently disable updates altogether.

**The duplicate was removed, not added to.** `settings.html` already did its own numeric
comparison client-side, with its own `ahead` state. The rule now lives on the server and
the page consumes `fw_state` — version ordering is exactly the kind of thing that drifts
when two copies exist.

**A timer reporting no `fw=` is `behind`, not `unknown`** — it predates fw-v17, which is
knowledge rather than absence of it. `unknown` is reserved for the base having no cached
firmware to compare against.

Verified end to end on hardware: Pi backdated to fw-v24 with the device on fw-v29 gave
`fw_state: "ahead"`, `base_firmware_stale: true`, the Settings banner, and `BASE IS
OLDER` on the timer with no update offered. Pi restored to fw-v29 afterwards.

### I-42 · A scratched flight stayed valid at the base and in the export · FIXED (session 65, fw-v30)
`src/main.cpp`, `src/timer/FlightLog.{h,cpp}`, `src/comms/TimerComms.{h,cpp}`, `base_station/server.py` (`SCRATCH`, `scratch_flight`), `frontend/db.py`, `frontend/scoring.py`, `frontend/app.py`, `frontend/templates/results.html`

A flight is reported to the base **the instant it is flown**, so scratching it on the
timer was only half the job: `g_log.scratchLast()` updated the live `FlightLog` and
nothing else. The flight stayed valid in the base's log, in scoring, and **in the CSV
that goes to GliderScore**. Carried open from session 61 because it needed a decision,
not code.

**The decision: flag the row, do not delete it.** The deciding argument is mechanical
rather than philosophical — the timer re-reports the whole round from NVS when it ends,
and `record_flight()` dedups on `(pilot, group, duration)`. A **deleted** row would match
nothing on that resend and be **re-inserted seconds later**, silently undoing the
scratch. Flagging makes the resend a no-op and leaves an audit trail for a disputed call.

Measured on hardware, exactly that sequence:

```
10:14:22  ACK FLIGHT pilot=29 dur=4047          <- flight flown and stored
10:14:24  SCRATCH: pilot=29 4.05s group=37 — flight marked scratched
10:14:58  Duplicate FLIGHT suppressed: dur=4047ms group=37   <- the resend, absorbed
```

`SCRATCH pilot=N dur=M` goes through the **ACK-gated pending queue**, so a scratch
cannot be lost to a dropped link — which would otherwise leave the flight scoring at the
base while the timer showed it struck through. Round trip measured at **72 ms**.

The flight is identified **by duration**, the same key the dedup uses. That is
unambiguous by construction: a second flight with an identical duration in the same
group cannot exist, because `record_flight()` would have suppressed it.

Every consumer had to honour the flag, or the flag would be worse than useless:

| consumer | behaviour |
|---|---|
| `scoring.py` | excluded — `AND NOT scratched` |
| CSV export + sync JSON | excluded — a scratched flight is not a result |
| Results page | **shown**, struck through in red — the CD must see it was flown and discarded |
| "round has flights" guards | still counted — it *was* flown, so the round is not editable |

Proof it matters: the test round was **task A, which scores the last flight**. Scratching
a 4.05 s relaunch after a 15.03 s flight gave `raw_s = 15.0`. Without the fix the pilot
would have been scored on the launch they threw away.

⚠ **No `SCRATCH` for a jumped start.** That flight is never sent as a `FLIGHT` at all —
`JUMPED` goes instead, as a CD note — so there is no row at the base to scratch.

⚠ **fw-v30 requires a base with this handler.** An older base logs `Unknown command` and
sends no ACK, so the timer retries forever, the entry never leaves the 32-slot pending
queue, and that queue **drops the newest when full** — enough stuck scratches would start
discarding live `FLIGHT`s. Accepted as a development-only window; production ships both
together.

---

### I-43 · The drift scan could not see systemd drop-ins · FIXED (session 66)
`setup/apply-system-config.sh` (section 8, unmanaged config scan)

`/etc/systemd/system` was in `WATCH_DIRS`, so the scan looked correct — but it ran
`find -maxdepth 1`, and **a drop-in lives one level down** in `<unit>.d/`. The
whole class of file was unreachable.

That is the precise mechanism by which [I-31] hid for three weeks. A hand-added
`f3k-server.service.d/override.conf` pinned audio output to one speaker's MAC, so
the app's own output setting did nothing and the config on disk was a lie. A
drop-in is also invisible in the unit file itself — only `systemctl cat` shows one
— so the scan was the only thing that could have caught it, and it structurally
could not.

The clue was sitting in the file: `/etc/systemd/system/hostapd.service.d/override.conf`
has been in `MANAGED_PATHS` all along, and was equally unreachable by the scan
that is supposed to check it.

Two parts to the fix:

- `maxdepth` is 2 for `/etc/systemd/system` only. `-type f` still excludes the
  `*.wants/` symlink farms, which are systemd's bookkeeping and not config.
- An unmanaged drop-in is flagged **on its path alone, with no content test**.
  ⚠ This matters: [I-31]'s drop-in set an `Environment=` line, and the scan's
  `f3k|wlan|hostapd|…` content grep is a coin toss on whether such a file mentions
  anything it looks for. Anything under `/etc` that overrides a unit is admin-made
  by definition — vendor drop-ins ship in `/usr/lib/systemd/system` — so if we do
  not own it, it is drift.

`CONFIG_VERSION` 2 → 3. The change is check-mode only, so strictly nothing applied
moved; bumped anyway because the pre-commit guard fires on any edit to this script
and the alternative is teaching `--no-verify`, which would disable the guard
entirely. An idempotent re-run costs a fielded Pi nothing — it reports no changes
and restarts nothing, which is what the script is built for.

---

### I-44 · A dead method that looked like the horn mechanism · FIXED (session 66)
`base_station/frontend/audio.py`

Follow-up to [I-34], which that entry explicitly asked for: *"either wire it up or
delete it."* Deleted.

`AudioEngine.horn()` and `AudioEngine.cue()` were called from nowhere at all, and
`TimerProfile` carried a comment stating that the engine fired the window horns at
the phase boundaries. It never did. The generated-schedule code in [I-34] was
written against that promise, which is why generated rounds began in **total
silence** — no launch signal, the one cue a heat cannot do without.

A heat is driven entirely by `build_schedule()`, from the raw cue list. There is
now exactly one playback path and the comments say so. The bucketed
`prep`/`working`/`landing` tables stay, relabelled as what they actually are — an
*index* by seconds-remaining, used to pin cue timings in tests. Rebuilding that in
the test would mean duplicating the sign and phase handling.

⚠ The same false claim had propagated into two test docstrings. Corrected — a
wrong explanation in a passing test is how the next person re-learns it wrong.

**Pinned by four new tests** asserting the open and close signals survive into the
schedule `build_schedule()` actually dispatches, which is the level [I-34] broke;
the existing tests only checked the cue list. Verified they fail against a
reconstructed [I-34] (6 subtest failures). A fifth asserts `horn`/`cue` do not come
back, naming the reason.

---

### I-45 · "3.5 mm has never produced sound" was stale, not open · FIXED (session 62 by [I-31]; confirmed session 66)
`base_station/frontend/audio_control.py`

Carried as an open item through sessions 62, 63 and 65, with the working theory
that it was speaker-side. It was neither — it was [I-31], **already fixed in the
same session it was raised in, and never retested on the jack.**

Measured on the field Pi. The saved volume is 20, and on the jack's -102.39dB..+4dB
control:

```
raw    20%  ->  -81.11dB     <- inaudible: the reported symptom, exactly
mapped 20%  ->  -37.94dB     <- audible; what the -M fix already produces
```

Confirmed by ear at 60% (-9.31dB): the jack plays. Everything else measured
healthy too — switch `on`, `aplay` exiting 0 at the correct duration,
`output_device()` returning `plughw:0,0`.

⚠ **The lesson is about the register, not the audio.** A fix and the symptom it
cures can land in the same session and still leave the symptom recorded as open,
because nobody re-ran the failing case. The "speaker-side" theory then made it
look like it needed hardware nobody had to hand, so it was deferred three times.
**Re-run the failing case before carrying an item forward.**

Two things changed on the back of this:

- Volume changes now open the playback switch (`_unmute`), as its own call so a
  switchless control cannot fail the volume set. ⚠ **Defensive only — this was not
  the cause**, and the docstring says so, because a muted card is otherwise a
  perfect impostor: it consumes the stream in real time and `aplay` exits 0.
- `apply_saved_volume()` falls back to the default instead of doing nothing when
  no volume has been saved — that call is also what opens the switch, and a Pi
  whose operator never touched the slider is the one that would come up muted.

---

## Raised by the tester, 2026-08-01 (session 66) — OPEN

Three defects out of a 16-item field-test list. The rest of that list is feature
work and lives in `TESTER_FEEDBACK.md` (local, per the repo scope rule); only
things that are *wrong* are registered here.

---

### I-46 · A scratched flight does not consume a launch · FIXED (session 66) · P1
`base_station/frontend/scoring.py`, `app.py` (CSV export), `db.py`

**Revises [I-42], shipped one session earlier.** That entry made a scratched
flight invisible to scoring and to the GliderScore export. It should instead be a
**land-out: the launch happened and it scores zero.**

`Rule.max_flights` is documented as *"launches allowed in the window"*, and
`_flights()` filters `WHERE ... NOT scratched`. So a scratched flight frees its
slot, and on any task with a launch limit a pilot gets extra attempts:

| Task | Limit |
|---|---|
| F3K F | best 3 of **max 6 launches** |
| F5K A | targets, **max 4** |
| F5K B | last 1, **max 3** |
| F5K D | targets, **max 3** |
| F5K E | poker, **max 3** |

On Task F a pilot may launch eight times, scratch the two worst and still present
six — which is the scoring outcome, not a display detail.

**Decision (Kris, 2026-08-01): counts as a launch, scores 0.00, and exports to
GliderScore as a zero flight.** Exporting the zero matters: if we score it and
GliderScore does not, the two disagree on the same competition.

⚠ Also update `RULES.md` and the `/rules` page, which currently state in writing
that a scratched flight is "not scored, not exported". Reviewers are reading that
now, so it must not be left contradicting the code.

⚠ [I-42]'s flag-don't-delete decision still stands and is *reinforced* — the row
must exist to consume the slot, so deleting it was never viable.

**Fixed.** `scoring.py` now selects scratched rows and passes `0` for their
duration; `max_flights` therefore sees them and the slot is consumed. The CSV and
the public JSON export them as a zero in their own position.

⚠ **This inverts the worked example recorded in [I-42].** There, Task A with a
15.03 s flight then a scratched 4.05 s relaunch gave `raw_s = 15.0`. It now gives
**0** — the land-out *is* the last flight. That is the point: under the old rule a
pilot could land out on their final launch and scratch their way back to the
previous flight, un-flying the bad last flight the task exists to make them live
with.

⚠ **A scratch is now unrecoverable by the timekeeper** — it is a scored zero, not
an erasure. A clock started by mistake is a different thing and needs the CD to
delete the row on the base station. That route exists (Results → Edit → ×) but
R-27's end-of-round resend can re-create a deleted row, so it is not fully
reliable. **Raised as a challenge on the `/rules` page rather than settled here.**

Pinned by four tests in `test_scoring_db.py` (zero-but-counted, the Task F
extra-launch hole with its 150-vs-410 demonstration, no F5K bonus on a land-out,
row still never deleted) and one in `test_validation.py` for the CSV slot
position. `RULES.md` R-15a and the `/rules` page updated in the same change —
they had stated the opposite in writing to reviewers who are reading it now.

---

### I-47 · The Run page loses every recorded flight on any page load · FIXED (session 66) · P3
`base_station/frontend/templates/run.html`

`flights: []` is initialised empty, populated **only** by live websocket pushes,
and reset to `[]` on load. Nothing ever seeds it from the database.

So the times vanish from the screen on a refresh *and* on any navigation away and
back — while the heat is still running. Reported as two separate symptoms and they
are one bug:

> *"you might go to results to update a previous heat while one is in play … when
> you go back to the running heat you have lost all the times being displayed,
> they are in the database but they are no longer displayed"*

⚠ **The data is not lost** — this is display state only, which is why it has
survived unnoticed. But the Run page is where the CD works, and going to
`/results` to correct an earlier heat is a normal thing to do mid-competition, so
the CD is blinded by an ordinary action.

**Fixed** by hydrating from the DB rather than trusting the socket to have been
listening — the same lesson as [I-01]/[I-13]: *the client must not assume it saw
everything.*

`CompetitionStateMachine.get_status()` now carries the loaded heat's flights,
shaped exactly like the live `flight` event so the page needs no second code
path. ⚠ **One change, both routes**: `get_status()` already fed *both* the
server-side page render and `/api/run/state`, so the reload case and the
socket-down poll are covered by the same fix. `pollState()` replaces rather than
merges — the server's list is authoritative and ordered, so appending would
duplicate.

Scratched flights are carried and rendered struck through, so the live log agrees
with Results. The client also gained a handler for the `scratch` websocket event,
which the server has **always** broadcast — nothing was listening, so a scratch
stayed green on the CD's screen until a reload.

⚠ **JUMPED notes deliberately do not survive a reload.** A jumped start is never
written to the database (recording it would make it look like a result), so it
cannot be rehydrated. `pollState()` carries them across explicitly rather than
dropping them on a poll.

⚠ **A near miss worth recording:** the first attempt added a scratch broadcast
inside `scratch_flight()` — which already existed in `_dispatch()`. The duplicate
would have double-fired. It was caught only because the protocol tests use fake
servers that do not inherit new methods, so five tests errored immediately.
**Check for an existing broadcast before adding one.**

Pinned by three tests in `test_validation.py`: flights present after load,
correct `scratched` flag, empty when nothing is loaded, and scoped to the loaded
heat (hydrating another heat's flights would show times against pilots who have
not flown this round).

Verified on the Pi against the tester's own data — the `/run` HTML now carries
all four recorded flights in its initial state, including the 4.047 s scratch.

---

### I-48 · A timer powered off is not reflected on the Run page · FIXED (session 66) · P3
`base_station/server.py` (`evict_silent_timers`), `frontend/templates/run.html`

> *"If watch turned off run screen not updating"*

A powered-off watch sends no FIN, so the socket stays open and only the missing
PINGs give it away. Timers ping every 30 s and are evicted at 90 s.

**The root cause was not what it looked like.** The amber-from-45 s pill already
existed, so the first theory was that only the *eviction* went unannounced. A live
test on the Pi disproved it:

```
silent for 150s, socket held open, no FIN
  last_ping_age_s:  7.1 → 8.0        <- never moved
```

⚠ **`_keepalive()` resets `last_ping_at` on every successful *send*.** A
powered-off watch never sends FIN, so the socket stays open and our writes keep
succeeding into the kernel buffer for minutes. The ping clock was therefore reset
continuously and **the amber pill could never fire for the one case it exists
for**. Nor could the 90 s eviction.

⚠ **A successful write proves nothing about the far end.** `last_ping_at` was
carrying two different facts — "the timer spoke to us" and "our write did not
error" — and conflating them made a dead watch indistinguishable from a healthy
one. The reset itself is deliberate and correct (it stops the watchdog evicting a
timer that is receiving keepalives but has not yet hit its 30 s PING interval), so
it stays; a separate `last_rx_at` now records genuine receipt, exposed as
`last_rx_age_s`, and the UI judges staleness on that.

The second fault was real too: at eviction **nothing was announced.** `remove()`
deleted the client and the pill simply vanished from the strip on the Run page's
next 3 s poll.

⚠ **An entry disappearing is not a notification.** A CD looking away sees nothing
at all — which is precisely the report. Connect and JOIN both call
`broadcast_timers()`; eviction never did.

Four parts:

- **A new `last_rx_at`, set on any received line** — not per command, so an
  unrecognised message still counts as a sign of life. Exposed as `last_rx_age_s`
  and used by the UI for staleness. `last_ping_at` and its keepalive reset are
  untouched, so the earlier ping-timeout fix stands.
- **Eviction also fires on receive silence** (`RX_TIMEOUT_S = 180`), because the
  ping clock alone can never age out a dead-but-writable socket. Deliberately
  generous — six missed PINGs — since a healthy timer keeps its rx age under 30 s
  and evicting mid-round forces pilot re-selection.
- **Eviction now broadcasts.** One broadcast per sweep, not per timer, and none at
  all when nothing was evicted — a heartbeat broadcast every 30 s would fight the
  poll for no gain.
- **A lost timer stays on screen**, as a red `LOST` pill, until it reconnects or
  the next heat is loaded. Per-heat, so they cannot accumulate all day.
- **The amber pill shows the age** (`⚠ 52s`) instead of a bare `⚠`, so the CD can
  watch it climb toward eviction rather than guess whether it is a blip.

The watchdog's eviction step was split out as `evict_silent_timers()` so the
notification could actually be tested — the whole defect was that nobody was told,
and that is untestable inside a `while True: await sleep(30)` loop.

Pinned by eight tests: evicted and announced, healthy timer untouched with **no**
broadcast, a single missed ping does not drop a working timer, the socket is
closed (a powered-off watch never sends FIN, so it would otherwise leak), one
broadcast covers several evictions, rx age is not reset by our own keepalives, a
silent timer is evicted even while our sends succeed, and the rx window stays
generous.

⚠ **The lesson, and it is the same one as [I-45]:** the first fix was written
against a plausible theory and the live test disproved it. Watching `ping_age`
sit at 7–8 s through 150 s of total silence is what found the real cause; no
amount of reading would have. **Reproduce the failure before believing the
diagnosis.**

---

### I-49 · A jumped start cost the pilot nothing · FIXED (session 66) · P1
`server.py` (`record_jumped`, JUMPED dispatch), `db.py`, `state_machine.py`,
`run.html`, `results.html`, `RULES.md` R-09/R-10

> Kris: *"Jumped flights still need to score 0, we can not ignore them."*

Same hole as [I-46], and worse. A jumped start — a launch before the window
opened — was broadcast to the CD as a note and **never written to the database at
all**. So it did not merely score nothing: it consumed no launch either. On every
launch-limited task the pilot could jump the start and simply throw again, free.

⚠ R-10 stated *"the pilot simply loses that launch"*. The code did not implement
that, and had not since the feature was written. **A rule that reads as
implemented is worse than a missing one** — the review page asked whether a jumped
start should carry an *additional* penalty, a question that presumes the basic
cost was already being applied.

**Fixed** by recording it as a voided flight: `scratched = 1`, `void_reason =
'jumped'`, with a real `flight_no`. `scratched` remains the single "scores zero
and consumes a launch" flag, so [I-46]'s scoring and export logic covers this with
no change — the fix is entirely in *creating* the row.

A new `void_reason` column (`'scratch'` | `'jumped'`) records **why**. Both score
zero, but they are different offences, a dispute turns on which, and R-10's open
penalty question is unanswerable if the reason was never stored.

⚠ **No firmware change, and reconciliation cannot double-count it.** The timer
never writes a jumped start to NVS round history and never sends it as a `FLIGHT`
(`main.cpp::_recordFlight` sends `JUMPED` instead), so the end-of-round resend
does not carry it. Verified in the firmware source before relying on it.

⚠ **The Run page's separate unnumbered "jumps" list had to go.** Its comment said
jumped starts were held apart *"so they don't shift flight numbering"* — correct
while they were never stored, wrong the moment they take a real `flight_no`. F5K
altitudes are matched to flights **by index**, so a list that numbered differently
from the database would have put heights on the wrong flights. One list now, in
flight-number order, labelled `JUMPED` or `SCRATCH`.

Pinned by five tests in `test_protocol.py` — stored and voided, consumes a flight
number, reason distinguishes it from a scratch, repeats deduped, and `pilot=0`
still stores nothing ([I-25]'s rule).

---

### I-50 · Poker scores the flown time, not the announced one · FIXED (session 66) · P1
`base_station/frontend/scoring.py` (`poker` rule)

Found by looking up the FAI text for TF-10 rather than by review. **`F3K.11.5`
Task E, SC4 Vol. F3 `SOARING_25 V2`, effective 1 June 2025**: *"If the target is
reached or exceeded, then **the target time is credited**."* The worked example
scores 45 s for a 46 s flight against a 45 s call, and totals **142 s** entirely
from announced times.

⚠ **Quote the 2025 edition, not the 2011 mirror in `Spec_Sheet_URLS.md`.** Task E
changed materially between them and the 2011 text is wrong on three points now:
five targets became **three**, and the "end of working time" call went from
*explicitly forbidden* to *explicitly permitted with a single attempt*. The 2025
PDF is at
<https://www.fai.org/sites/default/files/ciam_f3_soaring_2025v2_final.pdf> and is
fetchable with a browser `User-Agent` — the "fai.org 403s scripted fetches" note
in the spec sheet is only true of a bare request.

We do the opposite:

```python
elif rule.kind == "poker":
    ranked = sorted(idx, key=lambda i: (-times[i], i))
    for i in ranked[: rule.n]:
        scores[i] = times[i]          # the FLOWN time
```

So every Poker round is scored wrongly — generously, and by a different amount for
each pilot depending on how far they overflew their calls. It also ignores
declarations entirely, so a pilot who never announced anything scores the same as
one who called and hit every time.

This was recorded as a known limitation in session 38 (*"declared targets are not
recorded"*) and treated as a missing feature. It is not: it is a **wrong score**
on a task GliderScore lists for both F3K and F5K.

**Needs the declared target to be recorded per flight**, which means the timer
side (TF-10) lands with it — the announcement is made to the timekeeper, so the
timer is where it is captured.

Rules that fall out of the same clause and must be implemented together:

- A failed call **cannot be changed** — re-fly the same target until achieved.
- **Launches are unlimited**; the limit is on *targets*, and 2025 puts it at
  **three**. ⚠ Deliberately **not** enforced by us — Kris's call: GliderScore
  takes the first 3 called lengths on sync.
- A failed attempt scores 0 and costs nothing but time.
- **A "Window" target is permitted** (`"end of working time"`, written `W`) — the
  rest of the working time, resolved to a concrete number when chosen.
  ⚠ **It has ONLY ONE attempt**, the single exception to the retry rule, and is
  for the competitor's last flight. Must be enforced on the timer.
- **Announcing after the launch is explicitly allowed** — *"shown to the
  timekeeper in written numbers immediately after the launch"* — which is why the
  target picker has to show the running flight time.

**Target count settled (Kris, 2026-08-01): three, following FAI 2025.** Applied —
`("E", None)` is now `n=3`, matching the `E(1)`/`E(2)` variants and F5K E.
GliderScore's base task text still says five; that is the 2011 rule.

⚠ **The count is the only part of Poker that is right.** The rule still scores the
flown times of the N longest flights. What remains is the actual defect above, and
it needs declared targets recorded per flight — so it lands with the timer work
rather than alone.

---

### I-51 · No way to sync a running working-time clock to a timer · FIXED (session 66, fw-v31) · P2
`F3K_Timer_1/src/comms/TimerComms.cpp`, `src/timer/WorkingTime.h`, `src/main.cpp`,
`base_station/frontend/state_machine.py`

Three separate needs all want the same missing capability: **tell a timer the
working time now remaining, mid-round, in seconds.**

1. **The test-mode fast-forward** (TF-16). Kris: *"Timer should jump to shortened
   WT time as this is what we are testing."* Correct — a fast-forward the watch
   does not follow tests only half the system.
2. **Late pilot/timer assignment** (TF-02). Kris: *"When timer is assigned to a
   pilot late in WT, timer should jump to existing WT countdown, same place as
   all other timers on the field."*
3. **Reconnect mid-working** — and this one is already broken today, silently.

⚠ **The existing reconnect path is wrong.** `send_catchup()` sends
`TASK wt=<seconds remaining>` then `START`, but on the firmware:

```c
g_wtMinutes = g_comms.getTaskWtSeconds() / 60;   // integer division
...
g_wt.begin(g_wtMinutes * 60);                    // whole minutes only
```

So a timer rejoining with 8:30 left is told **8:00**, and one rejoining with 45 s
left is told **zero**. `START` is also ignored unless the timer is in
`IDLE`/`PILOT_SELECT`/`COUNTDOWN`/`PREP` (`main.cpp:402`), so on a timer that is
already running the whole catch-up is a no-op — which is why nobody noticed.
`_startRound()` additionally calls `g_log.reset()`, so a catch-up that *does* land
wipes the timer's local flight log.

**Fix: a new `WTSYNC t=<seconds>` message.** Firmware release, so it ships with
the TF-10/TF-11 timer work rather than alone.

- `WorkingTime` needs a `syncRemaining(int seconds)` that sets `_remainingMs`
  without touching `_total`, the running state, the flight log or a running
  flight. ⚠ It must also mark every alert above the new remaining as already
  fired, or a jump from 10:00 to 0:15 fires the 2-minute, 1-minute and 30-second
  calls all at once.
- Accepted while `WORKING`; ignored otherwise.
- Base sends it on fast-forward, on late assignment, and from `send_catchup()`
  in place of the broken `TASK`+`START` pair.

⚠ `LAND t=` already does exactly this job for the landing window, which is why a
LANDING fast-forward re-syncs correctly today and a WORKING one does not. `WTSYNC`
is the same idea, and should have existed from the start.

---

### I-52 · The flight clock never counted down to the target · FIXED (session 67, fw-v32) · P2
`F3K_Timer_1/src/display/UI.cpp` (`_updateRunningInc`)

Found by the tester on the first Poker round ever flown: *"FT timer was counting
up not down."*

`_flightShowMs()` — which counts the flight **down** to the declared target and
turns it orange past it — was correct, tested, and used by every full-redraw path.
It was not used by `_updateRunningInc()`, which drew `ft.elapsed()` raw in green.

⚠ **`_updateRunningInc()` is the only path that redraws the flight clock while a
glider is in the air.** It runs every 50 ms; the full-redraw paths run on state
*changes*. So the countdown was live for exactly the frames where nothing was
flying, and absent for the entire flight — the one time it exists to be read.

This is the third instance of the family that produced [I-37] and [I-38]. The rule
is now explicit: **if a render helper is not called from the incremental path, it
does not run during a flight.**

---

### I-53 · R was dead on "---", so a sub-minute call was unreachable · FIXED (session 67, fw-v32) · P2
`F3K_Timer_1/src/main.cpp` (`STATE_TARGET_SET`)

Reported as *"you can not set a sub 1 minute flight"*, and the serial log shows
exactly how it felt from the outside:

```
19:41:38  [BTN] A clicked          ← L opens the picker, on "---"
19:41:40  [BTN] B clicked          ← R … nothing
19:41:41  [BTN] B clicked          ← R … nothing
19:41:42  [BTN] B clicked          ← R … nothing
19:41:45  [MAIN] Target picker cancelled
```

The picker opens on `---` and R was guarded by `if (!g_pickNone && !g_pickWindow)`,
so **the timekeeper's first three presses did nothing at all.** Reaching `0:45`
required knowing to press L exactly once first — and L only moves forward, so
overshooting meant cycling the whole minute range to get back.

Fixed by letting R leave `---` the same way it leaves any other value: first click
gives `0:05`. Sub-minute calls are now the *shortest* path in the picker rather
than the least reachable.

⚠ Confirming `0:00` is now refused and logged. It stores as `TARGET_NONE_S`, so it
would have looked like a declared call and behaved like no call at all.

---

### I-54 · The +1 s fine adjust could never be reached · FIXED (session 67, fw-v32) · P2
`F3K_Timer_1/src/input/Buttons.cpp`, `main.cpp` (`STATE_TARGET_SET`)

Found by reading the button thresholds while investigating [I-53], not by pressing.

`+1 s` was bound to R **very-long** (2000 ms) and confirm to R **hold** (800 ms).
Both fire *while the button is still down*, so the 800 ms confirm always ran first
and left `STATE_TARGET_SET` before 2000 ms could arrive. **The fine adjust was
unreachable by construction**, which meant the picker could only express multiples
of five seconds — and the rulebook's own worked example call is **2:38**.

Fixed by adding `btnBLongClicked()`, classified **on release** (pressed 800–2000 ms
and let go). Confirm moved to the 2 s hold, `+1 s` to the medium press.

⚠ **Accepted trade-off, do not "fix" it back.** Kris: *"+1 is on R release which
feels strange, but is really the only way we will get +1s in there and they are
not used very often."* Acting on release is the only way to distinguish two hold
lengths on one button — anything that fires while held commits to the shorter one.

---

### I-55 · A Poker call could not be made during prep, twice over · FIXED (session 67, fw-v32) · P2
`F3K_Timer_1/src/main.cpp` (`STATE_PREP`), `base_station/frontend/state_machine.py`

Requested by the tester — *"needs to have L available through full prep time"* —
because prep is the only part of a round when the caller has time to think and
write the call down. It was blocked at both ends:

1. **Firmware:** `STATE_PREP` had no `btnL` handler at all.
2. **Base:** `TASK … mode=poker` was broadcast *after* the prep loop ended, so for
   the whole of prep the timer did not know it was flying Poker and
   `_pokerCanDeclare()` was false regardless. Fixing only the firmware would have
   changed nothing visible.

Also fixed, and each would have been its own defect:

- `_startRound()` reset `g_targetS` unconditionally, so a call made in prep was
  **discarded the instant the window opened** — the exact thing being asked for.
  Now guarded by `g_prepDeclared`.
- The prep clock, its beeps and the no-START fallback all lived inside
  `case STATE_PREP`, so opening the picker **froze the countdown behind it** and
  the window would never have opened. Extracted to `_tickPrepClock()`.
- `START` and `COUNT` were ignored in `STATE_TARGET_SET`, so a caller still
  holding the picker open at zero would never have started the round.
- `W` resolves against `g_wt.getRemaining()`, which is zero during prep. In prep it
  now resolves to the full working time — the rest of a window that has not opened
  is the whole window.

⚠ R stays locked until `PREP_UNLOCK_S` (2 s) exactly as before, so this cannot
turn into an accidental launch.

---

### I-56 · W was the slowest call to enter, and it is the one that decays · FIXED (session 67, fw-v32) · P2
`F3K_Timer_1/src/main.cpp` (`STATE_TARGET_SET`)

Raised by the tester mid-test. The L cycle ran `--- → 0 → 1 → … → max → W`, putting
`W` **last** — up to eleven presses away on a ten-minute window.

Kris: *"if the pilot does a quick turn around and calls window you want to select
it quickly. Selecting any other FT does not matter as we are counting down from
the started time, but W is getting shorter while you are selecting the time."*

That is the whole argument: every numeric call is worth the same whenever it is
entered, but `W` resolves to *the working time remaining at the moment of confirm*,
so time spent dialling it is time taken off the call. It is also the natural
quick-turn-around call, i.e. the one made under the most time pressure.

Cycle is now `--- → W → 0 → 1 → … → max → ---`. **W is one press from opening.**

---

### I-57 · A W call resolved at the call, so it could not be flown · FIXED (session 67, fw-v33) · P1
`F3K_Timer_1/src/main.cpp` (`_confirmTargetPicker`, `_resolveWindowTarget`, `_judgeTarget`)

Found by the tester on the second press-through: *"there is an interesting thing
that happens with W — when you select it but do not launch straight away, you are
not able to fly the window."* Exactly right, and it is a **scoring** defect.

`W` was converted to a concrete number of seconds the instant it was confirmed —
"the working time remaining right now". But the working clock keeps running while
the pilot walks to the line, so the target was always longer than the window that
was actually left to fly. Measured on the old build:

```
19:22:53  Target declared: W (58s remaining)
19:23:52  Target 58s (W) vs flight 55.7s -> MISSED     ← flew the whole window
```

The pilot flew to the horn — the literal definition of the call — and was scored
zero. **A W was unflyable unless the glider was launched in the same second it was
called**, and it gets one attempt only, so the pilot could not even retry.

Fixed by keeping `W` unresolved (`g_targetWindowPending`) until the glider leaves
the hand, then resolving against the clock at that moment. Verified on the same
hardware minutes later:

```
19:36:19  Target declared: W (292s now; resolves at launch)
19:36:27  W resolved at launch: 284s to the end of the window
19:41:11  Target 284s (W) vs flight 284.5s -> ACHIEVED
```

Three consequences, all deliberate:

- **The screen shows `TGT W m:ss`, ticking down** while the pilot walks up — what
  the call is worth *if thrown now*, which is the number the timekeeper needs.
- **A W is judged on still being airborne when the window shuts**, not on
  arithmetic. Its resolved target is the time left at launch, so the flight ends
  within milliseconds of it and the truncating `(int)(durMs/1000) >= target`
  compare could put it one second under — failing the one call that can be flown
  perfectly.
- **A W called while already flying resolves immediately**: the launch has
  happened, so there is nothing left to wait for.

⚠ `g_windowUsed` survives `_startRound()` alongside a prep-declared W, or the
one-attempt-only rule would be silently lost at the window opening.

---

### I-59 · The timer's log silently dropped everything past ten flights · FIXED (session 67, fw-v36) · P1
`F3K_Timer_1/include/config.h` (`MAX_FLIGHTS`)

Found while answering a display question, which is the only reason it was found at
all: *"we need to record up to 20 flights (can be normal to fly 8-10 for learners)."*

`MAX_FLIGHTS` was **10**. `addFlight()` returns false past the cap and the caller
does not check, so an 11th flight vanished from the timer's live log **and** from
the NVS round history. It fails **silently and only on a long round** — exactly the
learner's round nobody stress-tests, and exactly the pilot least able to tell you
their times were wrong.

⚠ The base station was never affected: each flight is reported as it happens, so
the database and the export always had all of them. This was the timer's own
record — the failsafe you reach for when the base station is the thing that died.

Now 20. NVS is keyed per flight (`r0f19`), not a fixed-size blob, so raising the
cap does not invalidate history already written. The ACK-pending queue went 32 → 64
with it: an end-of-round `resendRound()` can now queue 20 flights plus 20
altitudes, and overflowing it drops precisely the messages the resend exists to
protect.

---

### I-60 · Nothing stopped the settings chain being used on the base · FIXED (session 67, fw-v37) · P2
`F3K_Timer_1/src/main.cpp` (`STATE_IDLE`)

Raised by the tester: *"when connected to base I can still hold R at home and
change WT / category type / time history / OTA."*

`STATE_IDLE` + R-hold opened `STATE_SETTINGS` unconditionally — the connection was
never tested. Working time and discipline both arrive with the next `TASK`, so
changing them on the watch is pointless at best, and at worst the CD watches a
value they just set revert mid-round with no explanation.

The rule now: **the base owns everything the base sends.** Connected, R-hold goes
straight to Round Recall and nowhere else.

⚠ Round Recall deliberately survives, and is the reason R-hold still does
anything. It is the timer's own record of what it flew, and the moment you most
need it is when the base station is the thing that has failed. It lost its other
entry point in the same session — L on the Time Up screen now pages the flight
list ([I-59]) — so this is its only door.

---

### I-61 · Firmware was watch-managed, so timers went stale in the field · FIXED (session 67, fw-v37) · P2
`F3K_Timer_1/src/main.cpp`, `src/ota/OtaUpdater.cpp`, `base_station/frontend/app.py`

Kris: *"OTA should not be watch managed, it should be base managed. If base has
newer firmware all timers should be pushed up to that FW."*

Updating a timer took four R-holds through the settings chain, on each timer, with
somebody who knew the sequence. So in practice timers ran whatever they were last
flashed with, and a fleet drifted apart — which matters because the protocol is
versioned and a mixed fleet is a mixed protocol.

Now the base pushes: `OTAPUSH` over the existing socket, swept every 20 s.

⚠ **The safety gate is worth more than the feature.** An update reboots the timer,
so the base only pushes with the round machine `IDLE` **and no heat even loaded** —
a loaded heat means the CD is about to press Start. The timer re-checks its own
state on arrival and refuses unless it is idle, because a push can be in flight
when a round begins. Pinned by `OtaPushSafetyTests`.

**Downgrades are the interesting half.** A timer *ahead* of the base means somebody
updated the timers and not the Pi, so the auto-push never moves a timer backwards —
it only ever upgrades. The base says it is the stale one and offers the CD a
deliberate downgrade button, which is the only thing in the system that sends
`force=1`, and `forceUpdate()` is the only path past the timer's own refusal to
flash backwards ([I-41]).

Verified end to end on hardware, with no human in the loop:

```
21:26:35  JOIN fw=fw-v36
21:26:54  [OTA] pushing fw-v37 to T1 (on fw-v36)
21:26:54  RX: OTAPUSH -> accepted -> AVAILABLE -> downloading
21:27:09  JOIN fw=fw-v37
```

⚠ Testing this needs a timer that reports an old version but understands
`OTAPUSH` — a genuine older build ignores the command. Build with `FW_VERSION`
backdated and the current code.

⚠ **And it needs `otadata` cleared.** A device that has just taken an OTA is
running from `ota_1`, so the wired `write_flash 0x10000` lands in the slot that is
**not** running: esptool says `Hash of data verified`, the device reboots, and
rejoins on the version you were trying to replace. Hit during this very test —
the timer came back as fw-v37 when it had just been flashed with fw-v36. This is
trap 1 in the flashing notes, and taking an OTA is exactly what makes it fire.

**The gate was then tested properly, and the second half found a gap:**

```
21:39:25  JOIN fw=fw-v36
21:39:44  [OTA] holding push of fw-v37 to T1 — a heat is loaded and about to run
```

Held correctly. But `abort` does **not** clear `_loaded` — only a round running to
completion does (`state_machine.py:454`). So a CD who loads a heat and aborts it,
or just leaves one loaded over lunch, blocks every firmware update indefinitely.
The automatic gate was left conservative and an explicit override added instead:
`POST /api/timers/update-now`, surfaced as an **Update now** panel on Settings that
appears only while a timer is behind, and which says why the push is waiting.

⚠ The override skips the *loaded* check only. A live round still refuses, on both
it and the downgrade — verified: `409 Not now — a round is PREP`.

---

### I-62 · The whole suite could pass against an app that dies on boot · FIXED (session 67) · P2
`base_station/tests/test_validation.py`

Found by causing it. The OTA auto-push task was added with no `import asyncio`;
230 tests went green and the service crash-looped on the Pi, restarting every 7
seconds.

`TestClient(app)` **without** a `with` block never runs the startup hooks. Every
test in `test_validation.py` was built that way, so nothing registered with
`@app.on_event("startup")` had ever been executed by a test — a growing list that
now includes the audio volume restore, the system-config apply and the OTA sweep.

One test, `StartupTests`, enters the context manager. It covers every startup hook
that exists and every one added later.

⚠ The tell that it is working: the suite now prints `sudo` usage noise from the
audio hook. That noise is the hooks actually running.

---

### I-58 · The screen blanked mid-flight · FIXED (session 67, fw-v34) · P1
`F3K_Timer_1/src/main.cpp` (`_screenMaySleep`)

Seen live: *"watch went to sleep in flight, we can't have that."* The log agrees —
`Screen asleep (state=2)` twice during a single 284-second flight, `state=2` being
`STATE_FLIGHT_RUNNING`.

`_screenMaySleep()` refuses to blank a live round **unless** `_benchMode()` is
true, which it is whenever a USB cable is attached. The rule was written to stop an
unattended simulated round burning the AMOLED. But **the timer is permanently
wired to the Pi for development**, so bench mode is true for every round anyone
actually tests — the protection was active in exactly the situation it was never
meant for.

Now: a live round with the base station connected never blanks, cable or not. Bench
blanking applies only to a *standalone* round, and a round the base is driving is by
definition attended.

⚠ This is the second defect caused by `_benchMode()` being true on the test rig.
Anything gated on it needs asking: *is this true of the test setup, and is that
what I meant?*

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
