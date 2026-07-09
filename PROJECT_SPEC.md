# F3K Timer System — Project Spec
*Base station + timer network — reference doc for Claude Code*
*Last updated: 5 July 2026*

---

## 1. SYSTEM OVERVIEW

Three-phase system for managing F3K (and future F5K/F5J) discus-launch glider competitions:

1. **Handheld Timer** (existing — Waveshare ESP32-S3-Touch-AMOLED-1.75C) — caller's flight/working-time device. See `F3K-Timer-Project-Context.md` for full timer spec, hardware decisions, and gotchas. That document remains authoritative for the timer itself.
2. **Base Station** (this doc) — Raspberry Pi 4-based hub. Runs the working-time board, manages all timers, collects flight data, handles pilot/task management, exports results.
3. **Front-end Management** (future) — web page + mobile app for event setup, task selection, and live standings, served/accessed via the base station.

This spec covers **Phase 2 (Base Station)**. Phase 1 (timer) is already built and documented separately. Phase 3 (front-end) is out of scope until Phase 2 is stable.

---

## 2. HARDWARE — LOCKED DECISIONS

**Compute**
- **Raspberry Pi 4** (existing units on hand — do not spec Pi 5/CM5 unless Pi 4 is proven insufficient in testing). Cost-effective, off-the-shelf, no carrier board R&D.
- Standard Raspberry Pi OS (64-bit).

**Local device display** (on-device setup/status, used when not on phone/web)
- **HDMI-connected capacitive touchscreen** (not GPIO/DSI). This keeps the GPIO header free for the LED matrix driver hardware if required — deliberate choice to avoid a hardware conflict.
- Off-the-shelf panel, no minimum size locked yet — pick based on what's comfortably readable outdoors.

**Working Time Display (LED wall)**
- Existing hardware: ~3x 250–300mm addressable LED panels, likely **HUB75** interface (16-pin IDC ribbon is the tell).
- **OPEN ITEM — confirm before building the driver code:**
  - If panels have their own receiver/sender controller box (Colorlight/Linsn/Novastar-style) → Pi feeds it over Ethernet/DVI, no GPIO involved, no HAT needed.
  - If panels are raw HUB75 with no controller box → use an **Adafruit RGB Matrix Bonnet/HAT** (Pi 4 compatible, off-the-shelf library support via `rpi-rgb-led-matrix`).
- Do not commit driver code until this is confirmed.

**Audio**
- Output via **3.5mm jack, USB, or Bluetooth** — all three supported natively on Pi 4, no extra hardware needed. Lets any external speaker the club already uses become the output device.

**Internet connectivity**
- **Not a hard dependency.** System is offline-first (see Section 4).
- Opportunistic connectivity via phone hotspot tethering (primary) or USB 4G/5G dongle (secondary, plug-and-play via ModemManager). No SIM/LTE HAT — that's a future option only if a permanently-connected base station becomes a real requirement.

---

## 3. NETWORK ARCHITECTURE

Three logical networks, deliberately kept separate:

| Network | Purpose | Radio | Notes |
|---|---|---|---|
| **Timer network** | Closed AP for timers only | Dedicated **USB WiFi adapter w/ external antenna** | Hardcoded SSID/password on timer firmware. Timer gets assigned an ID on join (by MAC or lightweight handshake). Max ~10 timers, one base station — no need to over-engineer for scale. Must be reliable every time — this is the reason for a dedicated radio rather than sharing. |
| **Interface network** | Laptop/phone connects for event setup, task selection, live review | Onboard Pi 4 WiFi | Standard AP (`hostapd`), separate SSID from timer network. |
| **Internet (opportunistic)** | GliderScore/X-Score awareness, time sync, future front-end sync | Phone hotspot or USB 4G/5G dongle | Not required for core operation. See Section 4. |

**Why a dedicated radio for timers:** shared radios risk contention/interference exactly when reliability matters most (mid-comp). Isolating the timer AP onto its own adapter removes that risk entirely for minimal cost.

---

## 4. DATA / OFFLINE-FIRST MODEL

- **GliderScore has no API** (developer is self-taught, in his late 80s/early 90s; a club member is in early, informal talks about a future API — treat as non-existent for design purposes, don't build against it).
- **X-Score is newer but still maturing** and missing features — don't hard-couple to it either.
- **Design principle: base station is fully self-sufficient offline.**
  - All pilot, task, and flight-time data stored locally (SQLite is sufficient at this scale).
  - **Export function** produces a file for manual import into GliderScore (or X-Score). *Confirm the exact expected import format with the club member/GliderScore docs before building the exporter — build to the real target, not a guess.*
  - Internet connectivity (Section 3) is used only for optional extras (e.g. checking rankings, pulling reference data) — never a blocking dependency for running a comp.
  - Keep the export/data layer generic enough that swapping target format (GliderScore → X-Score or a future API) is a config change, not a rebuild.

---

## 5. BASE STATION RESPONSIBILITIES (from original outline)

- Runs working time board (LED wall) and mirrors state to all connected timers
- Pilot management, pilot-to-timer binding
- AP for timers (isolated, external antenna)
- AP/access for interface network (laptop/phone)
- Downloadable/exportable results file (GliderScore/X-Score import format)
- Speaker output + sound files
- OTA updates — pushed to timers from base station (timer-side OTA client is a Phase 1 gap, see Section 6)
- Bidirectional communication with timers (collect flight times, push task/state changes)
- Task management, distributed to timers
- Touchscreen management on the base station itself
- Phone app / web management (Phase 3, but base station must expose the API/interface these will consume)

---

## 6. KNOWN GAPS TO SOLVE (carried from planning discussion)

- **Timer-side WiFi is not yet in the timer firmware** — current timer codebase has no WiFi client, no join/ID handshake, no OTA client. This needs to be added to the Phase 1 codebase as part of enabling Phase 2.
- **Bidirectional timer↔base protocol** not yet designed (message format, join handshake, task push, flight-time collection, OTA delivery channel).
- **LED wall interface** unconfirmed (Section 2) — blocks driver code.
- **GliderScore/X-Score export format** unconfirmed — blocks exporter implementation.
- **Base station repo/project structure** not yet created (separate repo from timer, or monorepo — decide before first commit).

---

## 7. OUT OF SCOPE FOR NOW

- Phase 3 front-end (web/mobile management app) — not started, don't build ahead of the base station API being stable.
- Direct GliderScore/X-Score API integration — doesn't exist yet, don't design around it.
- SIM/LTE HAT — only revisit if hotspot tethering proves genuinely insufficient in the field.
- Multi-base-station scenarios — explicitly out of scope per stated usage pattern (one base station, ≤10 timers).
