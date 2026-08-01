# F3K Timer — Base Station

Raspberry Pi base station for F3K/F5K hand-launch glider competitions. It runs the field
Wi-Fi network, talks to handheld timers over TCP, records flight times, drives competition
audio cues (GliderScore-identical), and produces GliderScore-compatible results.

Companion handheld-timer firmware lives in a separate repo (`F3K_Timer_1`).

## Architecture

A single asyncio process runs two servers in one event loop:

- **TCP timer server** (`server.py`, port 8765) — handheld timers connect with a
  JOIN/ASSIGN handshake (`JOIN mac=… fw=…`; the `fw` field is fw-v17+, and its absence
  is reported as "behind" rather than guessed at), PING/PONG keepalive (base sends PONG every 15s; a successful
  send resets the ping clock so freshly reconnected timers aren't evicted before their
  first 30s PING), and the round protocol (PREP / TASK / START / STOP / LAND / PILOTS /
  COUNT / SCREEN / FLIGHT / JUMPED / SCRATCH / ALTITUDE). Timers run the prep and landing countdowns locally
  from `PREP t=` / `LAND t=`; COUNT re-syncs the last 10s of prep. JUMPED (launch before
  the start horn) is surfaced to the CD only — never recorded. SCRATCH (the caller
  discarded a flight already reported) **flags** the row rather than deleting it: the
  timer re-reports the round from NVS at the end, so a deleted row would find no dedup
  match and be re-inserted seconds later. Scratched flights are excluded from scoring and
  from the GliderScore export, and shown struck through on Results. FLIGHT, JUMPED,
  SCRATCH, ALTITUDE,
  and SELECT are acknowledged (`ACK <line>`) — **always**, including duplicates and
  messages the base deliberately discards. The timer holds each one until ACKed, so a
  withheld ACK is an unbreakable retry loop rather than a lost message: `ACK` means
  "received and decided", not "stored". See `docs/PROTOCOL_ACK.md`.
- **Web app** (`frontend/app.py`, FastAPI + uvicorn, port 8080) — operator UI plus a
  WebSocket stream of live timing and flight events.

State is stored in SQLite. The web UI is server-rendered Jinja2 with Tailwind and Alpine.js
(both vendored to `frontend/static/` — no CDN, no build step, works offline at the field).

```
base_station/
├── server.py                 # TCP timer server + hosts the web app in one asyncio loop
├── requirements.txt
└── frontend/
    ├── app.py                # FastAPI routes (all pages + API endpoints)
    ├── db.py                 # SQLite schema + migrations
    ├── state_machine.py      # Competition state machine (IDLE→PREP→WORKING→LANDING)
    ├── audio.py              # GliderScore-profile-driven audio cue engine
    ├── audio_control.py      # Bluetooth speaker control, volume, PCM health checks
    ├── gs_import.py          # GliderScore .mdb import via mdbtools
    ├── templates/            # Jinja2 + Tailwind + Alpine.js
    │   ├── base.html
    │   ├── setup.html
    │   ├── rounds.html
    │   ├── run.html
    │   ├── results.html
    │   ├── import.html
    │   ├── export.html
    │   ├── flight_cards.html
    │   └── settings.html
    └── data/                 # GliderScore-derived reference data
        ├── gliderscore_timer_profiles.json   # 18 timer/audio cue profiles
        ├── gliderscore_audio_library.json    # 233-row announcement library
        └── audio/            # 147 announcement wavs (~25 MB) — vendored, see Audio
setup/
├── install.sh                # From-scratch install on a fresh Pi OS image: OS packages, clone, venv, systemd unit. Idempotent. Does the app half only — run upgrade-to-dual-ap.sh after it for the field networks
├── f3k-server.service        # systemd unit, installed by install.sh (paths rewritten for the actual user/home)
├── migrate-to-git.sh         # One-time migration: SCP-copy Pi → git clone; installs git, clones repo, recreates venv, migrates data, updates systemd service
├── upgrade-to-dual-ap.sh     # One-time dual-AP bootstrap: hostapd (both SSIDs), dnsmasq, nftables captive portal, wlan0/wlan1 setup services. Rewrites those files wholesale and resets eth0 — run in person, not unattended
├── apply-system-config.sh    # Idempotent OS config applied on every update: mt76 USB fix, hostapd ctrl_interface, dnsmasq bind-dynamic, wlan1 poll loop, hostapd watchdog. `--check` = read-only drift report, including unmanaged systemd drop-ins (how [I-31] hid for three weeks). Bump CONFIG_VERSION when changing what it applies
├── timer-serial-logger.py    # Captures a USB-cabled timer's serial to ~/f3k_timer_serial.log. Holds the port open (opening it resets the ESP32) and reopens when the device re-enumerates on a flash — turns the base station into a remote lab. Dev aid; not enabled by install.sh
└── f3k-timer-serial.service  # systemd unit for the logger: `sudo systemctl enable --now f3k-timer-serial`
.githooks/
└── pre-commit                # Blocks a commit that changes apply-system-config.sh without bumping CONFIG_VERSION; warns on live Pi drift. Enable with `git config core.hooksPath .githooks`
docs/
└── PROTOCOL_ACK.md           # ACK extension spec: unconditional-ACK rule, dedup, and the timer's ACK-gated pending queue (implemented fw-v16)
tools/
├── gs_sync.py                # Windows bridge: GUI + CLI; fetches JSON from base station → writes scored results direct to GliderScore .mdb (ACE OLEDB via 32-bit PS)
└── build_exe.ps1             # PyInstaller build script → dist/F3KSync.exe (deploy to Pi for CD download)
ISSUES.md                     # Known-defects register: stable IDs (I-01…), priority, file:line, status. Cite the ID in the commit that fixes it
base_station/tests/
├── test_validation.py        # Locks down the ISSUES.md register: input validation + run-control state guards, driven through TestClient
└── test_protocol.py          # The ACK contract the timer's retry depends on — verbatim echo, all four retried types, no-pilot and duplicate messages
```

Known defects live in `ISSUES.md`: **22 fixed, 1 WONTFIX, 0 open**, each fix carrying
its issue ID in a comment at the site. The register also records what was *checked and found sound*, so the
same ground isn't re-covered, and what the audit did **not** touch (anything visual,
audio timing, the timer protocol, real hardware) — none of which is cleared.

Two conventions came out of that pass and are worth keeping:

- **Never call bare `float()`/`int()` on a submitted field.** Use `_parse_duration`
  and `_parse_altitude` in `frontend/app.py` — they reject `1:99`, infinity, NaN and
  out-of-range values that otherwise store as plausible-looking wrong data.
- **The server refuses; the client reports.** Run-control endpoints return 409 with
  a reason rather than an unconditional `{"ok": true}`, because every client gate is
  websocket-derived and a dropped socket makes it read `IDLE` forever.
- **Hiding a form control does not exempt it from validation.** A conditionally
  shown field must be `:disabled` as well as `x-show`n, or the browser silently
  refuses to submit a form whose invalid control it cannot focus — no request, no
  error ([I-22]). Bind both from one predicate so they can't drift.

## Web UI

| Route | Purpose |
|-------|---------|
| `/setup` | Two-column: collapsible competition cards (newest expanded; name/discipline/date/location header) + sticky pilot registry (right) with checkbox selection bound to a competition dropdown (Add to comp); Archive/Unarchive with archived comps viewable in a collapsed section; MIXED (F3K+F5K) competitions supported; per-comp scoring config; bulk pilot CSV import; GS Locked comps read-only; **pilot rename** (inline pencil icon per row, fetch-based, no page reload) |
| `/rounds` | Round builder — collapsible competition cards (chevron header, round count badge); rounds displayed in a responsive 3-column grid; tasks (A–N), working time, groups with pilot draw + TBD slots; **Draw Wizard**: semi-automated multi-round draw (round count, groups/round, task selection cycled across rounds, avoid back-to-back option) with preview → Accept / Re-shuffle / Cancel, pair-coverage + timekeeper stats, and mid-competition redraw of remaining rounds (completed rounds kept and seeded); add/delete controls hidden for GS Locked competitions; **Round edit**: inline pencil icon on each round card edits task and working time (blocked if round has flights recorded or GS-locked); **Custom Tasks**: clone any catalogue task and adjust its rule settings (flights that count, per-flight cap, targets, ladder start/step, max launches, window) — custom tasks appear in all task dropdowns and score natively; **Custom task edit**: edit button on each custom task row pre-populates the form for in-place editing (code/discipline immutable) |
| `/run` | Operator screen — load/start/abort heats (start warns if any pilot has no timer registered), live M:SS.T countdown (tenths, 20fps), flight log with altitude and jumped-start notes, CD skip, competition filter chips (show all, or up to two competitions in side-by-side queue columns), dual F3K/F5K discipline columns otherwise, mark heats done/undone, auto-advance 3s toast, readiness check warning, timer connection status strip (T1/T2 pills), pilot status strip (○ unbound → ✓+T#), CD override form to manually log a flight for any pilot |
| `/results` | **Competition filter chips** (All + per-comp, filters via `?comp_id=`); collapsible per-competition blocks (newest open; date + location in title); per-heat flight tables — pilots × flights, times in M:SS.hh; F5K altitudes in fuchsia; computed Raw / Score (0–1000) / Rank columns per heat with non-counting flights dimmed and F5K bonus shown per flight; per-heat Edit mode: delete flights, manually add flights (pilot, flight #, split M:SS.HH input, altitude) |
| `/leaderboard` | Live cumulative standings — rank, per-round normalised scores, drop rounds struck out, total; discipline filter for MIXED comps; auto-reloads on flight events; public JSON at `/api/results/{comp_id}/public`; **kiosk mode** (`?kiosk=1`): hides nav, larger fonts, shows comp name/date/location header — suitable for projector or big screen; "⛶ Kiosk" button on normal view opens in new tab |
| `/import` | Upload GliderScore `.mdb`, pick competition, import pilots/rounds/draw |
| `/export` | Download GliderScore-compatible 15-field CSV per competition; download F3KSync.exe (Direct Sync tool) |
| `/settings` | Audio volume + lead compensation, Bluetooth speaker, **Connected Timers** (**Renumber** button — timer numbers persist in the DB across restarts, so this is the way to free a number held by a decommissioned or test timer) (T-prefixed IDs matching the timer's own screen; per-timer firmware version — green when it matches the cached timer firmware, orange when behind, light orange when the timer is *ahead*, which means this base station is the stale one and gets a banner saying so. Timers refuse a downgrade, so a stale base cannot undo a firmware update), competition DB backup/restore, **Software Update** (git pull base station code + Pi OS config + sync timer OTA firmware files; shows `build.N` version number + cached firmware version; smart health-poll reload — waits for server restart before navigating. A failed OS-config apply rolls itself back and reports why instead of reloading) |
| `/reports/flight_cards/{id}` | Printable pilot flight cards (A4 landscape, 2×2 per page) — one card per pilot per discipline for the full comp; pre-filled from draw with Rd.Grp, full task name, and blank flight columns; F5K cards include paired Alt columns (blue tint); MIXED comps produce separate F3K and F5K cards per pilot; "🖨 Cards" button on Rounds page |
| `/pilot` | Mobile read-only pilot view — state, live countdown, current heat + pilots, leaderboard link; captive-portal landing page for phones on F3K_OPS |
| `/health` | JSON status (timers connected) |

Tailwind and Alpine are vendored under `frontend/static/` and served by the Pi — no
internet needed at the field. Competitions list newest-first everywhere; archived
competitions (Setup → Archive) are hidden from all pages except Setup. Pilots are
assigned to competitions from the registry panel: tick pilots, pick the competition,
Add to comp.

## GliderScore Integration

The base station is a **data recorder**, not a scorer — GliderScore does all
normalisation and points maths.

**Import (GliderScore → base station):** Upload `GliderScoreData.mdb` from the
operator's Windows machine. The Pi reads it with `mdbtools` and imports the selected
competition: pilots (with GliderScore PilotNo stored), rounds, task assignments, and the
group draw from the `Scores` table. No special export from GliderScore is needed — all
competition setup lives in the `.mdb`.

**Export (base station → GliderScore):** Download a 15-field CSV
(`CompNo, TaskNo, RoundNo, GroupNo, ReFlightNo, PilotNo, Data1–7, Penalty, PilotName`)
in GliderScore's External Scoring System format. Flight times use `mmss.sss` encoding
(e.g. 83.4 s → `123.400`). PilotNo values come from the imported GliderScore registry so
they match exactly on re-import. F5K uses the same format as F3K (raw flight times in
Data1–4); altitude is not in the CSV — the CD enters motor-cut altitudes manually in
GliderScore after import. MIXED competitions export each round with the correct TaskNo
(F3K=5 / F5K=6) per round. Flights are exported in `flight_no` order so task rules like
"last flight counts" (F5K Task B) apply correctly.

**Direct DB write:** `tools/gs_sync.py` is a Windows-side bridge that writes scored results
(full F3K/F5K task scoring + F5K altitude bonus) directly to `GliderScoreData.mdb` via ACE
OLEDB + 32-bit PowerShell — no CSV import step. Ships as `F3KSync.exe` (PyInstaller,
downloadable from the `/export` page). Double-click to open the GUI: browse to GliderScore
folder, enter base station URL, Connect, pick competition, Sync Scores. Also usable as CLI:
`F3KSync.exe --base http://10.0.1.12:8080 --comp-id N`. End-to-end verified (F3K + F5K);
NormalisedScore populates on GliderScore Recalculate; written values survive.

Task catalogues and digital-timer audio cue schedules are extracted verbatim from
GliderScore's own database; the reference data lives in `frontend/data/`.

**Self-contained scoring engine** (making GliderScore optional, not removing it) is
**complete** — all phases including printable flight cards. Existing GliderScore
paths — CSV export, direct DB sync via `F3KSync.exe`, and `.mdb` import — are all retained.
`frontend/scoring.py` provides native task scoring rules (all F3K tasks A–N/U10/U15 incl.
variants, all F5K tasks A–E), group normalisation (best = 1000, truncated to 0.1), cumulative
standings with configurable drop scores and FAI tie-breaking, and the F5K altitude bonus
(BP Table 2020-10). `frontend/draw.py` adds FAI group draw (round 1 random, later rounds
reverse-standings snake seeding) via a Draw button on the Rounds page. Scores are computed
on demand from raw flight data — nothing is persisted, so edits/deletes are always reflected.
The engine is a discipline-dispatched rule table so future disciplines (F3J, F5J, F3B) can be
added as plugins. Unit + integration tests in `base_station/tests/` — 88 tests, run with
`python -m unittest discover -s tests -t .` from `base_station/`. The validation suite
needs `httpx2` — kept in `requirements-dev.txt`, not `requirements.txt`, since the server
does not need it. `install.sh` installs it non-fatally; without it that suite skips
cleanly (64 tests) rather than failing the run.

## Audio

The audio engine replicates GliderScore's Big Timer / Digital Timer behaviour exactly.
It loads the GliderScore timer profiles (F3K-3m10m30s, F5K-5m10m15s, etc.), auto-selects
by discipline and working time, and fires announcement `.wav` files + synthesised beeps
through the Bluetooth speaker (`bluez-alsa` / `aplay`). A lead-compensation scheduler
fires each cue slightly early to offset output latency; the lead is tunable in Settings.

**The announcement wavs are vendored** in `frontend/data/audio/` (147 files, ~25 MB) —
the same reason Tailwind and Alpine are vendored: the field networks have no internet,
and a missing wav is not an error, it is silence. A missing file logs
`[AUDIO] missing wav: …` and the cue is skipped, so this fails quietly at a
competition rather than loudly at setup. Check the log if cues go missing.

That set is the *fixed vocabulary* — countdowns, horns, `Round1`–`Round30`,
`Group1`–`Group20`, task announcements, `NextGroupToReadyBox`. **Pilot-name clips
(`ZZ*.wav`) are gitignored and never committed**: one per pilot per competition, so the
set grows without bound, and they are recordings of real people's names. An install
carries them over from a previous deployment if there is one; otherwise they are
sourced from GliderScore's audio library.

## Running

**A fresh Pi, from a clean Raspberry Pi OS image:**

```bash
bash <(curl -s https://raw.githubusercontent.com/gadjt12a/F3K_Timer_Base_Station/main/setup/install.sh)
```

That installs the OS packages (`alsa-utils`, `mdbtools`, `avahi-daemon`, optionally
`bluez`/`bluez-alsa-utils`), clones the repo, builds the venv, installs the systemd unit
and starts it. It is idempotent, and it never touches eth0 or NetworkManager — that is
the admin lifeline. Follow it with `setup/upgrade-to-dual-ap.sh` for the field networks.

To run it by hand instead (venv is required — PEP 668):

```bash
python3 -m venv venv && ./venv/bin/pip install -r base_station/requirements.txt
./venv/bin/python3 base_station/server.py
```

In production it runs as the `f3k-server.service` systemd unit (auto-start on boot),
behind two on-board Wi-Fi APs (`hostapd` + `dnsmasq`):

- **F3K_BASE** (timer network, 192.168.10.0/24, wlan1 — MT7612U USB with external antenna) — handheld timers connect here
- **F3K_OPS** (operator network, 192.168.20.0/24, wlan0 — built-in) — operator devices connect here; captive portal auto-opens `/run` on connect (dnsmasq resolves all DNS to 192.168.20.1, nftables redirects port 80 → 8080, FastAPI catch-all completes the redirect)

A cron watchdog (`/usr/local/bin/hostapd-watchdog.sh`, every 2 min) probes the hostapd
control interface and restarts hostapd if the MT7612U wedges while still looking healthy
to systemd. Both APs set `ctrl_interface=` so the probe has a socket to talk to. Run
`setup/upgrade-to-dual-ap.sh` to install the whole arrangement on a fresh image.

## Disciplines

F3K and F5K today; F3J, F5J, and F3B planned. F3K and F5K run as separate competitions
on the same day, alternating rounds, sharing a pilot pool.

**F5K altitude entry** is implemented end-to-end: after the working time expires the pilot
presses R on the Time Up screen to begin entering altitudes. The watch steps through each
recorded flight ("FLIGHT N of M"), with R=+1m / L=+10m / hold-R to confirm, then sends
`ALTITUDE pilot=N flight=M alt=X` to the base station which stores it against the flight
record. Altitudes are stored and shown in fuchsia on the results page. F5K CSV export
includes flight times; altitude must be entered separately in GliderScore (altitude is not
part of the CSV format — GliderScore applies its own bonus table internally).
