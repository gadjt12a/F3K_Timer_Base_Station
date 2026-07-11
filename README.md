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
```

## Web UI

| Route | Purpose |
|-------|---------|
| `/setup` | Global pilot registry + competitions (F3K / F5K), per-comp pilot assignment |
| `/rounds` | Round builder — tasks (A–N), working time, groups with pilot draw + TBD slots |
| `/run` | Operator screen — load/start/abort heats, live M:SS.HH countdown (20fps), flight log with altitude, CD skip, dual F3K/F5K heat queue columns, mark heats done/undone, auto-advance 3s toast, readiness check warning, timer connection status strip (T1/T2 pills), pilot status strip (○ unbound → ✓+T#) |
| `/results` | Per-heat flight tables — pilots × flights, times in M:SS.hh; F5K altitudes shown in fuchsia below each flight time |
| `/import` | Upload GliderScore `.mdb`, pick competition, import pilots/rounds/draw |
| `/export` | Download GliderScore-compatible 15-field CSV for each competition |
| `/settings` | Audio volume + lead compensation, Bluetooth speaker, timer diagnostics |
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
in GliderScore's External Scoring System format. F3K flight times use `mmss.sss` encoding
(e.g. 83.4 s → `123.400`). PilotNo values come from the imported GliderScore registry so
they match exactly on re-import. F5K export is not yet supported (altitude→Data mapping
TBD).

Task catalogues and digital-timer audio cue schedules are extracted verbatim from
GliderScore's own database; the reference data lives in `frontend/data/`.

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

- **F3K_BASE** (timer network, 192.168.10.0/24) — handheld timers connect here
- **F3K_OPS** (operator network, 192.168.20.0/24) — operator's laptop connects here

## Disciplines

F3K and F5K today; F5J and (some) F3B planned. F3K and F5K run as separate competitions
on the same day, alternating rounds, sharing a pilot pool.

**F5K altitude entry** is implemented end-to-end: after the working time expires the pilot
presses R on the Time Up screen to begin entering altitudes. The watch steps through each
recorded flight ("FLIGHT N of M"), with R=+1m / L=+10m / hold-R to confirm, then sends
`ALTITUDE pilot=N flight=M alt=X` to the base station which stores it against the flight
record. F5K CSV export is not yet supported (altitude → Data1–7 mapping TBD pending a
scored F5K sample).
