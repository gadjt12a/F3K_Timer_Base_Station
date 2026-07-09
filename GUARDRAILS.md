# F3K Timer System — Guardrails
*Hard rules for any Claude Code session working on this project. Read this before making architecture, hardware, or library decisions. If a request conflicts with something here, flag it rather than quietly following it.*
*Last updated: 5 July 2026*

---

## HOW TO USE THIS DOC

- This is the guardrail layer. `PROJECT_SPEC.md` and `PROJECT_PHASES.md` are the working detail; the existing timer `CLAUDE.md` remains authoritative for Phase 1 hardware/firmware specifics.
- Add to this doc whenever a decision is made that should never be silently reversed. Don't let it balloon into a full spec — keep entries short and rule-like.
- If unsure whether something is a locked decision or still open, check `PROJECT_SPEC.md` Section 6 (open items) first.

---

## HARD RULES — DO NOT VIOLATE WITHOUT EXPLICIT SIGN-OFF

1. **No R&D-heavy custom hardware.** Off-the-shelf components only, across timer and base station, unless explicitly told otherwise. If a task seems to require custom PCB/carrier board work, stop and flag it — don't just start designing one.
2. **Raspberry Pi 4 is the base station compute platform.** Do not propose Pi 5/CM5 or other SBCs unless Pi 4 has been proven insufficient through actual testing, not assumption.
3. **M5Stack ecosystem is fully banned** (M5Unified, M5GFX, M5PM1, M5IOE1, etc.) — leftover from an abandoned hardware path, must never appear in either timer or base station code.
4. **`delay()` is banned in audio/display code** on the timer. Use `millis()` deltas, FreeRTOS tasks, or DMA callbacks.
5. **Offline-first, always.** The base station must be able to run a complete competition with zero internet connectivity. Internet (hotspot/dongle) is opportunistic only — never a blocking dependency for core functionality (timing, task management, data collection, local export).
6. **No direct GliderScore/X-Score API integration.** Neither platform has one worth building against right now (GliderScore has none; X-Score is immature). Export-to-file for manual import is the only supported integration path until this doc is explicitly updated.
7. **Network isolation is deliberate, not incidental.** Timer network, interface network, and internet uplink are three separate logical networks. Do not collapse them for convenience — the timer network in particular must stay isolated and reliable (closed AP, hardcoded credentials, dedicated radio with external antenna).
8. **One base station, ≤10 timers.** Do not design for multi-base-station scenarios or large-scale timer counts — it's explicitly out of scope and adds complexity for a case that doesn't occur.
9. **GPIO header conflicts must be designed around, not ignored.** If a component needs the GPIO header (e.g. LED matrix bonnet), the local touchscreen must be HDMI-connected, not GPIO/DSI. Check for conflicts before adding any new GPIO-header hardware.
10. **Sim and hardware UI paths stay separate (Phase 1 timer).** The Wokwi rectangular UI and the Waveshare radial UI are from-scratch, separate codebases. Never adapt one into the other.

---

## LIBRARY / TOOL ALLOW-DENY (per environment)

**Timer — hardware path:** Arduino_GFX, XPowersLib, SensorLib. **Never:** any M5Stack library.
**Timer — sim path:** Adafruit ILI9341 only.
**Base station:** Standard Raspberry Pi OS + Python/Node stack, `hostapd` for AP management, `rpi-rgb-led-matrix` if driving HUB75 directly (only if LED wall has no controller box — confirm first), ModemManager for USB 4G/5G dongle if/when used.

---

## OPEN ITEMS THAT BLOCK DOWNSTREAM WORK

Do not write code against these until confirmed — check `PROJECT_SPEC.md` Section 6 for current status:
- LED wall interface (controller box vs raw HUB75)
- GliderScore/X-Score export file format
- Bidirectional timer↔base protocol design

---

## THINGS THAT HAVE ALREADY BEEN TRIED AND REJECTED — DON'T RE-LITIGATE

- Custom ESP32-C3 PCB (timer) — shelved for M5Stack, which was then also abandoned
- M5Stack Stopwatch (timer) — unit became unavailable, migrated to Waveshare
- ESP-NOW mesh for timer↔base comms — superseded by WiFi AP with hardcoded credentials; only revisit with a documented reason
- Direct API integration with GliderScore — no API exists; don't propose building against one
- Pandora F3K Master (buy vs. build evaluation) — rejected due to one-way comms, closed firmware, poor Gen 1→2 support history

---

## WHEN IN DOUBT

Ask before assuming. Kris actively corrects wrong assumptions and expects rationale for design changes, not just output — especially on hardware/ergonomic decisions. Flag scope creep rather than quietly expanding into Phase 3 work while Phase 2 is incomplete.
