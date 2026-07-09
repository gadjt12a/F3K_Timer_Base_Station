# F3K Timer System — Project Phases
*Roadmap to keep work sequenced and prevent scope creep*
*Last updated: 5 July 2026*

Work strictly in phase order. Do not start a phase's tasks until the prior phase's exit criteria are met. If a task from a later phase seems urgent, log it in the relevant phase section below rather than pulling it forward.

---

## PHASE 1 — Handheld Timer (mostly complete)

**Status:** Fully working on real Waveshare hardware. WiFi/OTA/networking being added retroactively to support Phase 2.

**Remaining Phase 1 work:**
- [x] Build waveshare radial UI render path in `UI.cpp`
- [x] Wire `[env:waveshare]` into `platformio.ini`
- [x] Re-verify state machine, audio schedule, Buttons class against real Waveshare hardware
- [x] Confirm round-clip behaviour on real hardware

**New Phase 1 additions (required for Phase 2 to function):**
- [ ] Add WiFi client capability to timer firmware (join base station's timer AP using hardcoded SSID/password)
- [ ] Implement join handshake so base station can assign the timer an ID
- [ ] Implement OTA client (receive firmware updates pushed from base station)
- [ ] Implement bidirectional message handling (receive task/state pushes, send flight-time data back)

**Exit criteria:** A single timer can join the base station's AP, receive an ID, and exchange a basic message both ways, on real hardware — before building out full base station logic against it.

*Note: RPi4 now on hand. LED wall and GliderScore export format still unconfirmed — do not write driver or exporter code until confirmed.*

---

## PHASE 2 — Base Station

**Status:** Architecture decided (see `PROJECT_SPEC.md`). No code written yet.

**2a — Foundation**
- [ ] Confirm LED wall interface (controller box vs raw HUB75) — blocks 2c
- [ ] Confirm GliderScore/X-Score export file format — blocks 2e
- [ ] Set up Pi 4 base image, repo structure, dev environment
- [ ] Bring up `hostapd` for timer network (dedicated USB adapter) and interface network (onboard WiFi), as two isolated networks

**2b — Timer communication**
- [ ] Define bidirectional protocol (join/ID assignment, task push, flight-time collection, OTA delivery)
- [ ] Implement base-station side of protocol
- [ ] Test against a single Phase-1-updated timer, then scale to multiple

**2c — Working time board**
- [ ] Implement LED wall driver (path depends on 2a confirmation)
- [ ] Mirror working-time/flight-time state to the wall

**2d — Pilot/task management**
- [ ] Local data store (SQLite) — pilots, tasks, flights, timer bindings
- [ ] Pilot-to-timer binding logic
- [ ] Task management + distribution to timers

**2e — Data export**
- [ ] Build exporter to confirmed GliderScore/X-Score format
- [ ] Manual export/import tested end-to-end with a real GliderScore import

**2f — Local device UI**
- [ ] HDMI touchscreen UI for on-device setup/status (no phone/web dependency)

**2g — Audio**
- [ ] Sound file playback via 3.5mm/USB/Bluetooth output

**Exit criteria:** A full mock event (multiple timers, pilots, tasks, flights) can be run entirely on the base station with no internet connection, and results successfully export/import into GliderScore manually.

---

## PHASE 3 — Front-end Management (future, not started)

**Do not begin until Phase 2 exit criteria are met.**

- [ ] Define base station API/interface for external clients (web + mobile will consume the same one)
- [ ] HTML page: event setup, task selection, live standings
- [ ] Mobile app: same functionality, field-friendly
- [ ] Event type selection (F3K / F5K / possibly F5J)
- [ ] Multi-event mode
- [ ] Pilot card management
- [ ] Time import/export via UI (wraps Phase 2e exporter)

---

## PARKING LOT (explicitly deferred, do not action without re-opening)

- SIM/LTE HAT for permanent connectivity
- Direct GliderScore/X-Score API integration
- Multi-base-station support
- ESP-NOW mesh (superseded by WiFi-AP-with-hardcoded-credentials decision — do not resurrect without a documented reason)
