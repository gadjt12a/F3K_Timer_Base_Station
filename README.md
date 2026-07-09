# F3K Timer — Base Station

Raspberry Pi base station for F3K/F5K hand-launch glider competitions. It runs the field
Wi-Fi network, talks to the handheld timers over TCP, records flight times, drives the
operator web UI, and produces GliderScore-compatible results.

Companion handheld-timer firmware lives in a separate repo (`F3K_Timer_1`).

## Architecture

A single asyncio process runs two servers in one event loop:

- **TCP timer server** (`server.py`, port 8765) — handheld timers connect, JOIN/ASSIGN
  handshake, PING/PONG keepalive, and the round protocol (TASK / START / STOP / PILOTS /
  COUNT / FLIGHT).
- **Web app** (`frontend/app.py`, FastAPI + uvicorn, port 8080) — operator UI and a
  WebSocket stream of live timing/flight events.

State is stored in SQLite. The web UI is server-rendered Jinja2 with Tailwind (CDN) and
Alpine.js — no front-end build step.

```
base_station/
├── server.py                 # TCP timer server + hosts the web app in one asyncio loop
├── requirements.txt
└── frontend/
    ├── app.py                # FastAPI routes: /setup /rounds /run /ws /health /api/run/*
    ├── db.py                 # SQLite schema (competitions, pilots, rounds, groups, flights)
    ├── state_machine.py      # Competition state machine (IDLE→PREP→WORKING→LANDING)
    ├── audio.py              # Audio cue engine (in progress)
    ├── templates/            # base / setup / rounds / run  (Jinja2 + Tailwind + Alpine)
    └── data/                 # GliderScore-derived reference data (task + timer/audio cues)
```

## Web UI

| Route | Purpose |
|-------|---------|
| `/setup` | Global pilot registry + competitions (F3K / F5K), per-comp pilot assignment |
| `/rounds` | Round builder — tasks + groups with pilot draws |
| `/run` | Operator screen — load/start/abort heats, live countdown + flight log |
| `/health` | JSON status (timers connected) |

## GliderScore integration

The base station is a **data recorder**, not a scorer — GliderScore does all
normalisation/points maths. Flight times are exported as a CSV that GliderScore desktop
imports. Task catalogues and the digital-timer audio cue schedules are sourced from
GliderScore itself; the extracted reference data lives in `frontend/data/`.

## Running

On the Pi (Python venv, PEP 668):

```bash
python3 -m venv venv && ./venv/bin/pip install -r base_station/requirements.txt
./venv/bin/python3 base_station/server.py
```

In production it runs as the `f3k-server.service` systemd unit (auto-start on boot),
behind the on-board Wi-Fi AP (`hostapd` + `dnsmasq`).

## Disciplines

F3K and F5K today; F5J and (some) F3B planned. F3K and F5K run as separate competitions on
the same day, alternating rounds, sharing a pilot pool.
