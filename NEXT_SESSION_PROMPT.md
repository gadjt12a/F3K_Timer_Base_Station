# Session Start Prompt — Task 6 Hardware Testing

Paste the text below as your opening message in a new Claude Code session.

---

## PROMPT TO PASTE

We are continuing work on the F3K Timer base station project.
Working directory: `C:\Kris\Projects\F3K_Timer_Project`

Please read `SESSION_STATE.md` before doing anything.

Today we are **testing and validating** the work from sessions 13–14 before moving on to
new frontend tasks. Task 6 (Runner UI) is functionally complete and has been hardware-tested
at a basic level, but needs thorough end-to-end validation. Do not start new frontend tasks
until all checklist items below pass.

---

### What's implemented (sessions 13–14)

- **Runner UI** (`/run`) — two-column layout: heat queue left, live state panel right
- **WebSocket** — tick/flight/state_change events from `state_machine.py` to browser
- **PILOTS broadcast** — sent to all connected timers at start of PREP
- **COUNT broadcast** — `COUNT 10..1` during last 10s of prep → `STATE_COUNTDOWN` on timer
- **Countdown arc** — green anticlockwise arc on timer display, short beep per tick, long beep + WT start on START
- **Flight times in hundredths** — `fmtMs(ms)` → `M:SS.hh` in flight log
- **Timer reconnect catchup** — `send_catchup()` resends PILOTS (all states) and TASK+START (WORKING state) on JOIN

---

### Testing checklist

Work through these in order. Fix any failures before proceeding.

#### 1. Basic run UI
- [ ] `http://10.0.1.12:8080/run` loads without errors
- [ ] Heat queue shows all competitions / rounds / groups from the database
- [ ] Pilot names display correctly in each heat card
- [ ] Load button is enabled in IDLE, disabled when a round is running

#### 2. Load + Start
- [ ] Click Load on a heat → right panel shows comp/round/heat/task, START becomes active
- [ ] Click Start → state badge turns blue (PREP), 120s countdown begins
- [ ] Timers show pilot select screen during PREP (PILOTS received)
- [ ] Last 10s of PREP: `COUNT 10..1` triggers `STATE_COUNTDOWN` on timer — green arc visible, short beep per second
- [ ] At COUNT 1 → COUNT expires → `TASK` + `START` broadcast → timer plays long beep and WT starts
- [ ] State badge turns green (WORKING), WT countdown begins on browser

#### 3. Flight recording
- [ ] Pilot presses R to start flight, R again to stop → flight appears in browser flight log within ~1s
- [ ] Flight time displayed as `M:SS.hh` (hundredths, NOT just MM:SS)
- [ ] Multiple flights from the same pilot stack up correctly in the log
- [ ] Flights from different pilots both appear in the log with correct names

#### 4. Working time expiry
- [ ] WT reaches 0 → state transitions to LANDING, 30s landing countdown
- [ ] LANDING reaches 0 → state returns to IDLE, flight log clears
- [ ] Browser shows IDLE, Load buttons re-enable

#### 5. Abort
- [ ] Click ABORT during PREP → state returns to IDLE immediately
- [ ] Click ABORT during WORKING → state returns to IDLE, timer receives STOP
- [ ] After ABORT, Load + Start can start a fresh heat

#### 6. Timer reconnect — PREP phase
- [ ] Start a heat (PREP running)
- [ ] Power cycle or WiFi-disconnect the timer
- [ ] Timer reconnects during PREP → receives PILOTS → shows pilot select screen
- [ ] Countdown arc still appears in last 10s
- [ ] WT starts correctly at end of PREP

#### 7. Timer reconnect — WORKING phase
- [ ] Start a heat, let it reach WORKING state
- [ ] Power cycle or WiFi-disconnect the timer
- [ ] Timer reconnects during WORKING → receives PILOTS + TASK + START → WT starts from full time
- [ ] *(Known limitation: WT restarts from full duration, not mid-round timestamp)*
- [ ] Pilot can select, start flights, record times normally

#### 8. Multiple heats in sequence
- [ ] Run Heat A to completion (IDLE)
- [ ] Load Heat B, start, run to completion
- [ ] Confirm no state leakage (flight log clears, timer returns to IDLE between heats)

---

### Known issues / limitations (don't fix these yet)

- Timer reconnect during WORKING restarts WT from full duration (no timestamp sync) — acceptable for now
- WiFi AP (onboard wlan0) occasionally drops connections — MT7612U USB adapter (dedicated AP radio) is on order (~5 days), will improve stability
- No score summary screen yet (upcoming frontend tasks)

---

### If bugs are found

Fix them before moving on. Common places to look:
- `base_station/frontend/state_machine.py` — state machine logic, PILOTS/COUNT/catchup broadcasts
- `base_station/server.py` — JOIN handler, `send_catchup()` call
- `base_station/frontend/templates/run.html` — Alpine.js, `fmtMs()`, WebSocket handling
- `F3K_Timer_1/src/main.cpp` — COUNT/START/STOP handling, state transitions
- `F3K_Timer_1/src/display/UI.cpp` — `STATE_COUNTDOWN` render

After any fix: deploy to Pi, rebuild + flash timer if firmware changed, then re-run the affected checklist items.

---

### When all tests pass

Move on to the next frontend task per `SESSION_STATE.md`.
