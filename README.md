# F3K Timer — Base Station

Raspberry Pi base station for F3K/F5K hand-launch glider competitions. It runs the field
Wi-Fi network, talks to handheld timers over TCP, records flight times, drives competition
audio cues (GliderScore-identical), and produces GliderScore-compatible results.

Companion handheld-timer firmware lives in a separate repo (`F3K_Timer_1`).

## Architecture

A single asyncio process runs two servers in one event loop:

- **TCP timer server** (`server.py`, port 8765) — handheld timers connect with a
  JOIN/ASSIGN handshake, PING/PONG keepalive (base sends PONG every 15s; a successful
  send resets the ping clock so freshly reconnected timers aren't evicted before their
  first 30s PING), and the round protocol (TASK / START / STOP / PILOTS / COUNT / FLIGHT / ALTITUDE).
- **Web app** (`frontend/app.py`, FastAPI + uvicorn, port 8080) — operator UI plus a
  WebSocket stream of live timing and flight events.

State is stored in SQLite. The web UI is server-rendered Jinja2 with Tailwind (CDN) and
Alpine.js — no front-end build step.

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
    │   └── settings.html
    └── data/                 # GliderScore-derived reference data
        ├── gliderscore_timer_profiles.json   # 18 timer/audio cue profiles
        └── gliderscore_audio_library.json    # 233-row announcement library
tools/
├── gs_sync.py                # Windows bridge: GUI + CLI; fetches JSON from base station → writes scored results direct to GliderScore .mdb (ACE OLEDB via 32-bit PS)
└── build_exe.ps1             # PyInstaller build script → dist/F3KSync.exe (deploy to Pi for CD download)
```

## Web UI

| Route | Purpose |
|-------|---------|
| `/setup` | Two-column: competitions (left, full-width) + sticky pilot registry (right); multi-select checkbox chips to bulk-assign registry pilots to a competition (Select all / Add selected); MIXED (F3K+F5K) competitions supported; per-comp scoring config; bulk pilot CSV import; GliderScore-imported competitions show GS Locked badge — pilot draw and structure are read-only |
| `/rounds` | Round builder — collapsible competition cards (chevron header, round count badge); rounds displayed in a responsive 3-column grid; tasks (A–N), working time, groups with pilot draw + TBD slots; **Draw Wizard**: semi-automated multi-round draw (round count, groups/round, task selection cycled across rounds, avoid back-to-back option) with preview → Accept / Re-shuffle / Cancel, pair-coverage + timekeeper stats, and mid-competition redraw of remaining rounds (completed rounds kept and seeded); add/delete controls hidden for GS Locked competitions |
| `/run` | Operator screen — load/start/abort heats, live M:SS.HH countdown (20fps), flight log with altitude, CD skip, dual F3K/F5K heat queue columns, mark heats done/undone, auto-advance 3s toast, readiness check warning, timer connection status strip (T1/T2 pills), pilot status strip (○ unbound → ✓+T#), CD override form to manually log a flight for any pilot |
| `/results` | Per-heat flight tables — pilots × flights, times in M:SS.hh; F5K altitudes in fuchsia; computed Raw / Score (0–1000) / Rank columns per heat with non-counting flights dimmed and F5K bonus shown per flight; per-heat Edit mode: delete flights, manually add flights (pilot, flight #, split M:SS.HH input, altitude) |
| `/leaderboard` | Live cumulative standings — rank, per-round normalised scores, drop rounds struck out, total; discipline filter for MIXED comps; auto-reloads on flight events; public JSON at `/api/results/{comp_id}/public` |
| `/import` | Upload GliderScore `.mdb`, pick competition, import pilots/rounds/draw |
| `/export` | Download GliderScore-compatible 15-field CSV per competition; download F3KSync.exe (Direct Sync tool) |
| `/settings` | Audio volume + lead compensation, Bluetooth speaker, timer diagnostics, competition DB backup/restore |
| `/health` | JSON status (timers connected) |

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
**implemented** — see `SCORING_ENGINE_PROJECT.md` for scope and status. Existing GliderScore
paths — CSV export, direct DB sync via `F3KSync.exe`, and `.mdb` import — are all retained.
`frontend/scoring.py` provides native task scoring rules (all F3K tasks A–N/U10/U15 incl.
variants, all F5K tasks A–E), group normalisation (best = 1000, truncated to 0.1), cumulative
standings with configurable drop scores and FAI tie-breaking, and the F5K altitude bonus
(BP Table 2020-10). `frontend/draw.py` adds FAI group draw (round 1 random, later rounds
reverse-standings snake seeding) via a Draw button on the Rounds page. Scores are computed
on demand from raw flight data — nothing is persisted, so edits/deletes are always reflected.
The engine is a discipline-dispatched rule table so future disciplines (F3J, F5J, F3B) can be
added as plugins. Unit + integration tests in `base_station/tests/`.

## Audio

The audio engine replicates GliderScore's Big Timer / Digital Timer behaviour exactly.
It loads the GliderScore timer profiles (F3K-3m10m30s, F5K-5m10m15s, etc.), auto-selects
by discipline and working time, and fires announcement `.wav` files + synthesised beeps
through the Bluetooth speaker (`bluez-alsa` / `aplay`). A lead-compensation scheduler
fires each cue slightly early to offset output latency; the lead is tunable in Settings.

## Running

On the Pi (Python venv, PEP 668):

```bash
python3 -m venv venv && ./venv/bin/pip install -r base_station/requirements.txt
./venv/bin/python3 base_station/server.py
```

In production it runs as the `f3k-server.service` systemd unit (auto-start on boot),
behind two on-board Wi-Fi APs (`hostapd` + `dnsmasq`):

- **F3K_BASE** (timer network, 192.168.10.0/24, wlan1 — MT7612U USB with external antenna) — handheld timers connect here
- **F3K_OPS** (operator network, 192.168.20.0/24, wlan0 — built-in) — operator devices connect here; captive portal auto-opens `/run` on connect (dnsmasq resolves all DNS to 192.168.20.1, nftables redirects port 80 → 8080, FastAPI catch-all completes the redirect)

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
