# F3K Timer System — Session State
*Update this file at the end of every working session. Claude reads it at the start of each new session.*
*Last updated: 2026-07-10 (session 14 — fully complete)*

---

## Active Phase

**Phase 1 — Handheld Timer** complete. WiFi client implemented and verified on hardware.
**Phase 2 — Base Station** in progress. Two-radio AP setup live. Full round flow (TASK/START/STOP/FLIGHT/SQLite) hardware-verified.

---

## Current Status

### Phase 1 — Timer (`C:\Kris\Projects\F3K_Timer_1`)

| Item | Status |
|---|---|
| Wokwi sim path (`[env:wokwi]`) | Complete |
| Waveshare hardware path (`[env:waveshare]`) | Complete, working on device |
| WiFi client — connect to base AP, JOIN/ASSIGN | **Done & hardware-verified** |
| Bidirectional protocol (TASK/START/STOP/PILOTS/FLIGHT/PING/COUNT) | **Done & hardware-verified** |
| Pilot selection UI (`STATE_PILOT_SELECT`) | **Done & hardware-verified** |
| Reconnect robustness | **Done (session 4)** — see notes below |
| 10s countdown arc (`STATE_COUNTDOWN`) | **Done (session 14)** — green arc anticlockwise, short beeps, long beep on START |
| OTA client | Not started |

**Hardware-verified boot sequence (2026-07-05):**
- AXP2101, ES8311 audio, UI canvas all init correctly
- `[COMMS] Starting WiFi connect to F3K_BASE` fires on boot
- Tries continuously for 5 minutes (WiFi retried every 60s, TCP every 5s)
- After 5 minutes without connecting → standalone mode, reboot to retry
- If TCP drops while connected → fresh 5-minute reconnect budget, no wait
- Device MAC: `28:84:85:55:1e:b0` on COM4

**Known non-issues (pre-existing, do not fix):**
- ES8311 log: `0x32 (vol): 0xBF (expect 0x00)` and `0x37 (cfg): 0x08 (expect 0x48)` — expect values in log are wrong, audio works fine
- Opening serial monitor triggers DTR reset mid-boot → `AXP2101 init FAILED` on next boot — known, harmless during development. Do NOT connect serial monitor while testing connections; the DTR reset breaks the WiFi session.

### Phase 2 — Base Station (`C:\Kris\Projects\F3K_Timer_Project`)

| Item | Status |
|---|---|
| Pi 4 OS (Raspberry Pi OS Trixie 64-bit) | **Done** |
| hostapd — F3K_BASE AP on wlan1 (AR9271, external antenna) | **Done & hardware-verified** |
| hostapd — F3K_OPS AP on wlan0 (onboard, operator interface) | **Done & hardware-verified** |
| dnsmasq — DHCP 192.168.10.10–50 (timer), 192.168.20.10–50 (ops) | **Done & hardware-verified** |
| Boot persistence (wlan0-setup.service) | **Done** |
| TCP server port 8765 (`~/f3k_base/server.py`) | **Done** |
| JOIN→ASSIGN handshake | **Done & hardware-verified** |
| PING→PONG keepalive | **Done & hardware-verified** |
| Operator control interface (stdin CLI) | **Done** — type commands, broadcast to all timers |
| PILOTS command → timer pilot select UI | **Done & hardware-verified** |
| FLIGHT recording to SQLite | Wired up, untested (need START/STOP test) |
| TASK + START → timer countdown | **Done & hardware-verified** |
| FLIGHT recording to SQLite | **Done & hardware-verified** (3 flights confirmed in db) |
| SQLite schema (db.py) | **Done** — competitions, rounds, groups, group_pilots, extended flights |
| FastAPI + uvicorn (app.py) | **Done & hardware-verified** — /health returns timers_connected, port 8080 |
| Setup UI (/setup) | **Done & hardware-verified** — create/update competition, add/delete pilots |
| Web UI | In progress — Task 6 complete (Runner UI); Tasks 7–11 next |
| systemd service for server.py | **Done** (`f3k-server.service`, auto-starts on boot) |

**Pi 4 hardware details:**
- Hostname: `f3kbase`, IP: `10.0.1.12` (static on eth0 — works on home network and direct cable)
- wlan0 (onboard): F3K_BASE AP, 192.168.10.1/24 (timer network) — reverted from AR9271 due to ath9k_htc AP mode instability (crashes every ~60-90s)
- wlan1: unused until MT7612U arrives (~5 days)
- Timer connects to 192.168.10.1:8765, gets DHCP lease e.g. 192.168.10.17
- F3K_OPS AP config is ready (hostapd-ops.conf, f3k-ops.conf) — will activate when MT7612U is in place
- USB WiFi adapter (external antenna) needed for field use — get MT7612U or similar chipset

**Boot service chain:**
1. `wlan0-setup.service` — rfkill unblock, ip link up, ip addr 192.168.10.1 (runs after NetworkManager)
2. `hostapd.service` — F3K_BASE AP
3. `dnsmasq.service` — DHCP for 192.168.10.0/24
4. `f3k-server.service` — auto-starts `server.py` on boot (enabled)

---

## Immediate Next Steps (in order)

1. **Test Task 6 thoroughly** — full checklist in `NEXT_SESSION_PROMPT.md` before moving on. Countdown arc, reconnect robustness, flight hundredths, ABORT, multi-heat sequence all need hardware verification.
2. **Frontend Tasks 7–11** — in order per build plan (memory file `project_frontend_architecture.md`). Only start once Task 6 testing passes.
2. **Get GliderScore desktop export** — load a sample F3K competition (3–4 pilots, 2 rounds, a few scores entered), export it, and share the file. Unblocks: import format, F5K CSV fields, TaskNo/CompNo/PilotNo values. See `GLIDERSCORE.md`.
3. **Add F5K support to timer firmware** — `TASK wt=600 disc=F5K` flag; altitude entry screen on timer after WT expires; send `ALTITUDE flight=N alt=Xm` to base station.
4. **When MT7612U adapter arrives** — plug into Pi, confirm detected, move F3K_BASE AP to wlan1. Config already written.
5. **Flight data buffering** — timer loses FLIGHT messages if TCP drops mid-round; add local buffer + retry-on-reconnect.

---

## Protocol Reference (text lines over TCP port 8765)

**Timer → Base:**
```
JOIN mac=28:84:85:55:1e:b0      on connect
FLIGHT pilot=2 dur=83450        flight recorded (ms)
PING                             keepalive every 30s
```

**Base → Timer:**
```
ASSIGN id=3                      response to JOIN; triggers send_catchup() for reconnects
TASK wt=600                      set working time (seconds)
START                            all timers start WT simultaneously
STOP                             abort round
PILOTS 1:Alice Smi,2:Bob Jon     pilot list → triggers STATE_PILOT_SELECT on timer
COUNT 10                         last 10s of prep → STATE_COUNTDOWN, green arc, short beep
PONG                             keepalive response
```

**Reconnect catchup (`send_catchup`):**
- On JOIN, base resends: PILOTS in any non-IDLE state; also TASK+START if state=WORKING
- Timer that drops during PREP rejoins in pilot select; drop during WORKING restarts WT (known limitation)

**Timer connection behaviour (session 4):**
- On boot: tries WiFi+TCP continuously for 5 minutes. WiFi restarted every 60s if not associated. TCP attempted every 5s once WiFi is up.
- After 5 minutes: enters standalone mode. **Reboot to retry** — no background reconnect.
- If TCP drops while connected: immediately tries to reconnect with a fresh 5-minute budget.
- Stale socket detection: if no data received from server for 90s (server sends PONG every 30s), forces reconnect. Fixes the half-open socket problem where `_tcp.connected()` was returning true on a dead connection.
- `_tcp.flush()` added to `_sendLine()` — required for small packets (PING = 5 bytes) to bypass Nagle algorithm buffering.

---

## Open Blockers (do not write code against these)

- **LED wall interface** — controller box vs raw HUB75? Blocks Phase 2c (LED driver).
- **GliderScore integration** — export-only (no API). F3K CSV format now known (Data1–Data7 = FlightTime1–7 in mmss.sss). F5K CSV format unknown — need GliderScore desktop sample. Import format (loading competition into our system) also unknown. Full details in `GLIDERSCORE.md`. Partially unblocked for F3K export; fully unblocked once a GliderScore desktop export is provided.

Neither blocks Phase 2 foundation work (networking, SQLite, pilot/task management).

---

## Key Implementation Notes

**Timer button layout:**
- BOOT (top-right after rotation) = **R** = primary — start/stop flight, confirm pilot
- PWR (top-left after rotation) = **L** = secondary — scratch, WT-only start

**CO5300 QSPI display quirks:**
- `writeFastHLine` doesn't render → custom `ws_fillRing()` / `ws_eraseRingRadial()` using `fillRect` with 2px min height
- Software rotation via `Arduino_Canvas` (hardware rotation not supported)
- FreeFonts resolved from GFX Library for Arduino include path (no local `fonts/` dir needed)
- `_needsRender()` in main.cpp controls re-render gating — must track `_lastConnState` alongside `_lastState` or IDLE screen won't update when connection state changes

**PlatformIO command (not on PATH — use full path):**
```powershell
& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" run -e waveshare --target upload --project-dir "C:\Kris\Projects\F3K_Timer_1"
& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" device monitor -e waveshare --baud 115200 --project-dir "C:\Kris\Projects\F3K_Timer_1"
```

**Audio:**
- ES8311 via I2S, FreeRTOS background task for non-blocking playback
- PA_EN (GPIO46) HIGH before audio, LOW when idle

---

## Recent Decisions / Context

- **2026-07-09 (session 14):** Frontend Task 6 (Runner UI) complete and hardware-verified. Fixed three bugs during bring-up: (1) `x-data` Alpine.js attribute broken by JSON double-quotes — fixed by moving initial state into `const INITIAL_STATE = ...` in a `<script>` block; (2) `websockets` Python package not installed in venv — downloaded wheel on Windows, scp'd to Pi, installed offline; (3) `PILOTS` command never sent to timers — added to start of PREP sequence in `state_machine.py`, also added pilot IDs to `load_heat()` query. Added `websockets` to `base_station/requirements.txt`. Added 10s countdown feature: base station broadcasts `COUNT N` (10..1) during last 10s of PREP; timer firmware adds `STATE_COUNTDOWN` with green anticlockwise arc, short beep per tick, long beep on START (`playWindowOpen`). Added `send_catchup()` to state machine + called on timer JOIN — resends PILOTS in PREP, PILOTS+TASK+START in WORKING — fixes timer missing WT start after connection drop. Flight times now display to hundredths (MM:SS.hh) in run.html flight log.
- **2026-07-09 (session 13):** Frontend Task 5 complete. `state_machine.py` (IDLE→PREP→WORKING→LANDING→IDLE), `audio.py` stub, WebSocket `ConnectionManager` + `/ws` endpoint in `app.py`, `/debug/load_heat|start|abort` routes. Hardware-verified: heat load, focus_bell at 45s remaining, prep countdown cues, abort → IDLE. Tick events broadcast to WS (no logging, pure push). State machine wired into `F3KServer.__init__`; FLIGHT handler now fires `state_machine.on_flight()` via `create_task`.
- **2026-07-09 (session 12):** Frontend Task 4 complete. `/rounds` page: add rounds (task A–N, working time in minutes), add groups with pilot checkboxes (filtered per competition), TBD dummy slots (+/- per group for unassigned pilots). Working time stored as seconds in DB, displayed/entered in minutes. SSH key auth set up for Pi deploys (`~/.ssh/f3k_pi`, host alias `f3kpi`).
- **2026-07-09 (session 11):** Frontend Task 3 complete + design correction. Setup page redesigned after discovering "Mixed" discipline was wrong — F3K and F5K run as separate competitions on the same day, alternating rounds. Added `competition_pilots` junction table to schema. Setup page now: global pilot registry + multiple competition cards (F3K or F5K only), each with their own pilot subset assigned from the global pool. Routes: full CRUD for competitions and competition_pilots. Key gotcha: newer Starlette `TemplateResponse` API is `(request, name, context)` not `(name, {request: ..., ...})`. Alpine.js `$refs` only resolve within the same `x-data` scope — use `x-data="{ showNew: false }"` on parent, `x-show` on child.
- **2026-07-09 (session 10):** Frontend Task 2 complete. Created `base_station/frontend/app.py` (FastAPI, `/health` endpoint). Wired uvicorn into same asyncio process as TCP server via `uvicorn.Config(loop="none")` + `asyncio.create_task`. Hardware-verified: `curl localhost:8080/health` returns `{"status":"ok","timers_connected":0}`. venv at `~/f3k_base/venv` (Pi enforces PEP 668 — no system pip). Service file updated to use `venv/bin/python3`. Note: add `sleep 1` after `systemctl restart` before curling — uvicorn takes ~0.5s to bind after systemd reports started.
- **2026-07-09 (session 9):** Frontend Task 1 complete. Created `base_station/frontend/db.py` with full schema (competitions, rounds, groups, group_pilots + extended flights). Removed inline `init_db` from server.py — now imports from `frontend.db`. Schema verified locally; backward-compat confirmed against simulated existing 3-flight DB.
- **2026-07-09 (session 8):** Frontend design fully planned. Pi replaces MBT completely — drives audio (mpg123 via asyncio subprocess), two field display boards (browser /display pages, future Kevin LED boards via WebSocket JSON), and web UI. Tech stack locked: FastAPI + uvicorn (async, WebSocket native), Jinja2, Tailwind CSS CDN, Alpine.js CDN, mpg123. Single asyncio process (TCP server + web server), systemd Restart=always. AULD tasks are display-only (no flight logging, judges watch manually). Timing parameters configurable per competition with saveable presets. Full 11-task build plan documented in memory file `project_frontend_architecture.md`. Frontend directory: `base_station/frontend/`.
- **2026-07-06 (session 7):** Full web UI scope clarified. We are a data recorder not a scorer — GliderScore does all normalisation/points maths. System loads competition from GliderScore desktop export, runs the day (timing, pilot draws, F5K altitudes), exports a CSV the organiser imports back into GliderScore desktop. This replaces the current manual flow: paper → pilot QR code entry → organiser download → desktop. Combined F3K+F5K days confirmed — disciplines alternate per round/task, pilot pool is shared. F5K altitudes (20–80m range) entered by timekeeper on handheld timer at WT expiry (pilot reads aloud from onboard altimeter). GliderScore CSV format researched — F3K fully documented (see `GLIDERSCORE.md`), F5K unknown. Touchscreen on timer noted but deferred — physical buttons remain for timing, touch to be used later for altitude entry and pilot select UX improvement. Soarscore repo (github.com/petegee/Soarscore) confirmed as requirements research doc only, useful for F3K rules reference. Audio beep fix from session 6 verified on hardware and committed (176e6c8).
- **2026-07-06 (session 6):** Two-radio AP setup configured (AR9271 wlan1 + onboard wlan0). AR9271 failed in AP mode — ath9k_htc driver crashes every ~60-90s (known instability). TP-Link TL-WN821N also unusable (RTL8192CU, broken AP mode in-kernel driver). Reverted F3K_BASE to onboard wlan0. Full round flow verified: TASK+START → countdown, STOP → results screen, FLIGHT recorded to SQLite (3 flights confirmed in db). Fixed idle screen "BASE OK" display bug. Fixed audio alerts firing on results screen (g_wt.reset() added to all abort paths) — flashed but not yet verified. MT7612U arrives ~5 days. F3K_Timer_Base_Station to be created as separate git repo.
- **2026-07-06 (session 5):** No hardware. Established `Troubleshooting/Spec_Sheets/Spec_Sheet_URLS.md` as a living research log — useful URLs to be added as problems are solved. No code changes.
- **2026-07-05 (session 4):** Four bugs found and fixed. (1) Half-open socket: after server restart, `_tcp.connected()` wasn't detecting dead socket — fixed with 90s RX timeout (`_lastRxMs`). (2) Retry strategy changed: removed 2-min waits between retries — now tries continuously for 5 minutes then stops; reboot to retry. (3) PING buffering: `_tcp.flush()` needed in `_sendLine()` or small packets are held by Nagle algorithm. (4) UI re-render: `_needsRender()` didn't check connState for IDLE — display showed "BASE..." forever even after connecting; fixed by tracking `_lastConnState`. Display fix flashed but not visually confirmed (end of session).
- **2026-07-05 (session 3):** Pi 4 set up from scratch (Trixie 64-bit). hostapd+dnsmasq+wlan0-setup.service all configured and boot-persistent. TCP server deployed as f3k-server.service. Timer JOIN→ASSIGN→PING→PILOTS verified live on hardware. CONNECT_TIMEOUT_MS increased to 60s — initial WiFi connection was timing out because DHCP takes >15s cold start. stdin CLI added to server for operator commands.
- **2026-07-05 (session 2):** Implemented `src/comms/TimerComms.h/.cpp`. Simple text-line TCP protocol. Added `STATE_PILOT_SELECT` and pilot select UI. Both envs build clean. Flashed and hardware-verified.
- **2026-07-05 (session 1):** Corrected CLAUDE.md — waveshare UI was already fully working, docs were wrong. Fixed button mapping (BOOT=R=primary, PWR=L=secondary). Updated PROJECT_PHASES.md.
- ESP-NOW rejected — WiFi AP with hardcoded credentials is the chosen approach.
- M5Stack ecosystem fully banned.

---

## Key File Locations

| Item | Path |
|---|---|
| Timer firmware | `C:\Kris\Projects\F3K_Timer_1` |
| Timer comms layer | `C:\Kris\Projects\F3K_Timer_1\src\comms\TimerComms.h/.cpp` |
| Timer main loop | `C:\Kris\Projects\F3K_Timer_1\src\main.cpp` |
| Timer hardware spec | `C:\Kris\Projects\F3K_Timer_1\CLAUDE.md` |
| Base station server | `C:\Kris\Projects\F3K_Timer_Project\base_station\server.py` |
| Base station spec | `C:\Kris\Projects\F3K_Timer_Project\PROJECT_SPEC.md` |
| Phase roadmap | `C:\Kris\Projects\F3K_Timer_Project\PROJECT_PHASES.md` |
| Hard rules | `C:\Kris\Projects\F3K_Timer_Project\GUARDRAILS.md` |
| This file | `C:\Kris\Projects\F3K_Timer_Project\SESSION_STATE.md` |
