# Self-Contained Scoring Engine — Full Project Plan

*Created: 2026-07-15*  
*Purpose: Scope and task breakdown for building a self-contained scoring engine on the base station. GliderScore integration is retained as a supported workflow — this project adds a fully independent alternative, not a forced replacement.*

> **STATUS (session 38, 2026-07-15): IMPLEMENTED** on branch `scoring-engine` — Phases A1–A4
> (`frontend/scoring.py` + `base_station/tests/`), B1–B3, C1–C3, D1–D2 (`frontend/draw.py`),
> E1, F1 and G1–G2 are done, deployed and verified on the Pi.
> Remaining: **F2** (scoresheet / flight card generation) only.
> Known limitations: Poker (Task E) scores the N longest recorded flights since declared
> targets are not recorded; Ladder (D) credits the target time per achieved flight;
> re-flights (ReFlightNo) and team scoring are not modelled.

---

## Background

Our current workflow:

```
Timer → base station (automatic flight time capture)
→ organiser exports CSV from base station web UI
→ imports into GliderScore desktop
→ GliderScore calculates scores / normalisation
→ organiser uploads results to online competition table
```

**Goal:** make GliderScore **optional, not required**. All scoring, standings, and results can run on the Pi base station without any Windows desktop app. Organisers who prefer GliderScore can still use it — CSV export, direct DB sync (`gs_sync.py`), and `.mdb` import all stay in place. Organisers who want a fully self-contained field system can use the base station end-to-end without GliderScore installed at all.

---

## What GliderScore Actually Does (8 Functional Domains)

| Domain | What it covers |
|---|---|
| **1. Competition setup** | Create comp, pilot registry, round/group draw, task assignment per round |
| **2. Score recording** | Accept flight times + F5K altitudes (CSV, eScoring, manual) |
| **3. Task scoring rules** | Apply per-task rules (which flights count, targets, limits) |
| **4. Normalisation engine** | Best-in-group = 1000, others proportional; 0.1pt resolution |
| **5. Cumulative standings** | Sum across rounds, drop scores, team scoring |
| **6. F5K altitude bonus** | RefHeight + stepped bonus table (per comp, per round) |
| **7. Output / reporting** | Scoresheets, results tables, flight cards, online upload |
| **8. Timer/audio profiles** | Competition countdown, announcements, cue timing |

---

## What We Already Have

| GliderScore Domain | Base Station Coverage | Status |
|---|---|---|
| Competition setup | `/setup` — competitions, pilot registry, rounds, groups | Done |
| Group draw | Manual only — CD assigns pilots to groups | Partial (no FAI auto-rotation) |
| Task assignment | Per-round task letter on Rounds page | Done |
| Score recording | Timer → FLIGHT/ALTITUDE → SQLite | Done |
| F5K altitude entry | Firmware STATE_ALTITUDE_ENTRY + base station storage | Done |
| Manual flight entry | CD Override + Results edit | Done |
| Timer/audio profiles | GliderScore-identical audio engine, all 18 profiles | Done |
| GliderScore sync | `tools/gs_sync.py` writes directly to .mdb | Done (bridge only) |
| Raw results display | `/results` page — times and altitudes | Done |
| CSV export | 15-field GliderScore format | Done |

### Gaps — the scoring engine itself

| GliderScore Domain | What is missing |
|---|---|
| Task scoring rules | Which flights count per task (A=last, B=last 2, C=all N, D=ladder targets met, E=poker achieved, F=3 longest, etc.) |
| Group normalisation | best-in-group = 1000, others = own÷best × 1000, truncated to 0.1 |
| Cumulative standings | Sum across rounds, per-pilot running total |
| Drop score logic | Drop lowest N score(s) after round X (configurable per comp) |
| F5K altitude bonus | Apply bonus table to altitude vs RefHeight (formula confirmed, not computed yet) |
| CD altitude entry | Web UI for CD to enter/correct motor-cut altitudes after F5K WT |
| Standings/leaderboard | Live standings page sorted by cumulative score |
| Group draw algorithm | FAI-standard seeding (reverse standings → group assignment) |
| Online publishing | Public results upload (self-hosted) |
| Reports | Scoresheets, flight cards, competition PDFs |

---

## Full Task List

Ordered by dependency. Each task maps to a base station file or new module.

---

### Phase A — Scoring Engine *(core — blocks everything else)*

---

#### A1 — Task Scoring Rules Engine

**File:** `base_station/frontend/scoring.py` (new)

**Input:** list of flight records (times in ms) + task letter + task variant  
**Output:** list of "scored flights" — which flights count and what time is used (ms)

One function per task:

| Task | Rule | Cap |
|---|---|---|
| A | Last flight counts | 5:00 (variant: A(1)=10 min window, A(2)=7 min) |
| B | Last 2 flights count | 4:00 each (variant: B(1)=4:00/10 min, B(2)=3:00/7 min) |
| C | All N flights count | 3:00 each (C(1)=3, C(2)=4, C(3)=5 flights per round) |
| D | Ladder — first target 0:30, +0:15 when reached | Unlimited flights / 10 min |
| E | Poker — pilot declares target; scored flights must meet target | 5 flights (variant: E(1)=3 scores, E(2)=3 scores/15 min) |
| F | 3 longest flights count | 3:00 each, max 6 flights |
| G | 5 longest flights count | 2:00 each, unlimited |
| H | Best 4: must hit 1, 2, 3, 4 min targets (any order) | Unlimited / 10 min |
| I | Best 3 flights count | 3:20 each |
| J | Last 3 flights count | 3:00 each |
| K | Big Ladder: 5 flights in order, first 1:00 then +0:30 each | |
| L | One flight | 9:59 |
| M | Huge Ladder: 3, 5, 7 min in order | 15 min |
| N | Best single flight | 10 min window |

F5K tasks A–E use their own objectives (different from F3K despite same letters — see `GLIDERSCORE.md`).

> This is the most complex task in the project — 15+ distinct rule sets, each needing unit tests with edge cases (zeros, ties, partial targets, ladder completion).

---

#### A2 — F5K Altitude Bonus Computation

**File:** `base_station/frontend/scoring.py`

**Input:** `altitude_m`, `ref_height_m`, `bonus_table` (stepped curve of pts/m)  
**Formula** (confirmed from NZ Nats DB, BonusNo=1 "BP Table 2020-10"):

| Motor-cut altitude vs RefHeight | Bonus / Penalty |
|---|---|
| Below reference | +0.5 pts per metre below |
| At reference | 0 |
| 1–10 m above reference | −1.0 pt per metre above |
| 11+ m above reference | −1.0 pt/m for first 10m, then −3.0 pts/m for each additional metre |

Bonus only applies if flight duration ≥ `f5k_min_time_for_bonus` (30 s default).  
**Output:** `bonus_points` (positive = bonus, negative = penalty)

> Formula already confirmed. Straightforward to implement. Different competitions may use a different bonus table (BonusNo field) — the stepped curve must be configurable, not hardcoded.

---

#### A3 — Group Normalisation

**File:** `base_station/frontend/scoring.py`

**Input:** group results — dict of `pilot_id → raw_score_seconds`  
**Output:** dict of `pilot_id → normalised_score` (0–1000, 0.1 resolution)

Rules:
- Best raw score in group = 1000 pts
- Others = `floor(own / best × 10000) / 10` (truncate to 0.1, not round)
- Edge cases: all zero → all get 0 (not 1000); ties → both get 1000

---

#### A4 — Cumulative Standings with Drop Scores

**File:** `base_station/frontend/scoring.py`

**Input:** all normalised group scores across rounds + competition drop config  
**Output:** per-pilot totals with drop rounds identified

Drop score config (mirrors GliderScore `Comps` table):
- `drop1_at_round` — first drop applies after this round (99 = never)
- `drop2_at_round`, `drop3_at_round` ... up to 5 drops

Tie-breaking: highest score in last contested round (standard FAI rule).

---

### Phase B — Data Model Extensions

---

#### B1 — F5K Altitude Entry UI on Run Page

**File:** `base_station/frontend/templates/run.html`, `app.py`

After WT expires for an F5K heat: show altitude entry panel in the web UI.  
- One row per pilot with numeric altitude input
- Auto-calculates bonus points in real-time (calls A2)
- CD can correct altitudes sent by timers, or enter ones that were missed
- "Confirm altitudes" locks the heat and triggers group scoring

Track altitude source: `timer` (from ALTITUDE protocol message) vs `cd_entry` (entered via UI) — store in `flights` table as a new `altitude_source` column.

---

#### B2 — Scoring Config in Competition Table

**File:** `base_station/frontend/db.py`

Add to competitions (or a new `competition_config` table):

| Column | Default | Notes |
|---|---|---|
| `drop1_at_round` | 99 | No drop unless set |
| `drop2_at_round` | 99 | |
| `group_score_decimals` | 1 | Decimal places for display |
| `f5k_ref_height` | 60 | metres, default NLH |
| `f5k_bonus_table` | 1 | BonusNo (1 = BP Table 2020-10) |
| `f5k_min_time_for_bonus` | 30 | seconds |

These are already imported from the GliderScore .mdb via `gs_import.py` for GS-locked comps. For standalone comps, the Setup page needs fields for them.

---

#### B3 — Computed Score Storage (optional)

Recommendation: **compute scores on-demand** from raw flight data rather than storing normalised scores. This keeps the DB as a single source of truth and avoids stale computed values.

Cache in-memory per request. Only consider persisting if calculation time becomes a problem (unlikely — competitions have at most ~100 pilots × ~15 rounds).

---

### Phase C — Leaderboard & Standings UI

---

#### C1 — Standings / Leaderboard Page

**File:** `base_station/frontend/templates/leaderboard.html` (new), `app.py`

Live standings for a competition:
- Columns: Rank | Pilot | Rd 1 | Rd 2 | ... | Drop | Total
- Dropped rounds shown in gray / strikethrough
- Auto-refresh via WebSocket (same pattern as Run page)
- Filter by discipline for mixed-day F3K+F5K comps
- Accessible at `/leaderboard/{comp_id}`

---

#### C2 — Group Results View Enhancement

**File:** `base_station/frontend/templates/results.html`

Add computed columns to the existing results table:
- Raw score (sum of scored flight times in seconds)
- Normalised score (0–1000)
- Rank within group
- F5K: altitude column, bonus points, FPT (flight points = time + bonus)

"Calculate scores" button triggers A1–A3 for the selected group and updates the view.

---

#### C3 — Run Page Live Score Preview

**File:** `base_station/frontend/templates/run.html`

After marking a heat Done: show each pilot's raw score below their flights in the flight log. Useful for CD to spot-check before calling the next group.

---

### Phase D — Group Draw

---

#### D1 — FAI Group Rotation Algorithm

**File:** `base_station/frontend/draw.py` (new)

**Round 1:** Random draw (or alphabetical — configurable).  
**Round 2+:** Reverse-standings seeding → top pilots in last group, work backwards.

Standard FAI F3K draw:
1. Sort pilots by cumulative score descending (after A4)
2. Assign to groups in snake/reverse-snake pattern so leaders are separated
3. Group count and size come from competition config

**Input:** standings after round N, `groups_per_round`  
**Output:** `dict[pilot_id → group_no]` for round N+1

---

#### D2 — Draw UI

**File:** `base_station/frontend/templates/rounds.html`

- "Auto-draw next round" button (runs D1, populates groups)
- Manual override still available (existing dropdown/drag UI)
- Show seed numbers alongside pilot names when draw is based on standings

---

### Phase E — CD Workflow for F5K (see also B1)

---

#### E1 — Altitude Audit Trail

**File:** `base_station/frontend/db.py`, `app.py`

- `altitude_source` column on `flights`: `'timer'` | `'cd_entry'` | `null`
- Show source indicator in results and audit log
- Needed for dispute resolution at competitions

---

### Phase F — Publishing & Reports

---

#### F1 — Public Results Endpoint

**File:** `base_station/frontend/app.py`

`GET /api/results/{comp_id}/public` — unauthenticated JSON standings.  
Designed to be proxied by `glidetime.pawson.co.nz` (Tailscale + NGINX already wired).  
Optional: static HTML results viewer at `/leaderboard/{comp_id}` that works without JS for spectators.

---

#### F2 — Scoresheet / Flight Card Generation

**File:** new utility — `base_station/frontend/reports.py`

Per-round group scoresheets for timekeepers (pilot names, blank time fields, task description).  
Per-pilot flight cards.  
Generate as HTML → print from browser, or PDF via `weasyprint` on the Pi.  

> Low priority. Paper scoresheets are a fallback; digital entry via the timer is the primary flow.

---

### Phase G — Removing the GliderScore Import Dependency

---

#### G1 — Standalone Competition Setup

Currently `gs_import.py` reads pilot list + round structure from GliderScore `.mdb`.  
For fully standalone operation:
- Pilot registry already exists in Setup UI — no .mdb needed
- Rounds/groups already configurable in Rounds UI
- Gap: no bulk pilot import

#### G2 — Bulk Pilot Import from CSV

**File:** `app.py`, `setup.html`

Import pilot list from a CSV roster (name, FAI number, club, country).  
CSV format: `FirstName, LastName, FAINumber, Club, Country` (standard competition entry form output).  
Maps to existing `pilots` table; skips duplicates by FAI number.

---

## Multi-Discipline Expandability

### Design principle

The scoring engine must be built as an **open framework**, not a hardcoded F3K/F5K implementation. The architecture should allow a new discipline to be added by:

1. Registering a new discipline code (e.g. `F3J`, `F3B`, `F5J`)
2. Implementing a scoring module for that discipline (task rules + normalisation variant)
3. Wiring it to the existing competition/round/group data model — no schema changes required

This means Phase A should be structured as a **strategy pattern**:

```python
# Pseudo-structure — each discipline is a plugin
DISCIPLINE_SCORERS = {
    "F3K": F3KScorer,
    "F5K": F5KScorer,
    "F3J": F3JScorer,   # future
    "F5J": F5JScorer,   # future
    "F3B": F3BScorer,   # future
}

def score_group(discipline: str, task: str, variant: int, flights) -> GroupResult:
    return DISCIPLINE_SCORERS[discipline].score(task, variant, flights)
```

### Future disciplines — what we already know

All details below are extracted from `GliderScoreData.mdb` and documented in `Knowledge_Base/GLIDERSCORE_FULL_REFERENCE.md`. We have the complete picture for each discipline — no further research needed to start implementing.

#### F3J (GSCompClass = `F3J`, TaskNo = 1)

Winch/towline thermal soaring. Duration + landing.

- **Timer profile:** `F3J/F5JTimer-5m10m` or `F3J/F5JTimer-7m15m` (two window sizes)
- **Phases:** PT → TT (45s test fly) → NF (60s) → WT (10 or 15 min) → LT (30s)
- **Score:** Flight time (up to 600s) + landing score (0–100 pts from landing table)
- **Data fields:** Data2 = FlightTime, Data4 = FlightTime2 (dual timekeeper, else 0), Data7 = Landing
- **Landing table:** GliderScore uses `F3JLandingPts` (distance in cm → points); 0 cm = 100 pts
- **Normalisation:** same best-in-group = 1000 formula

#### F5J (GSCompClass = `F5J`, TaskNo = 1)

Electric soaring. Duration + height penalty.

- **Timer profile:** `F5JTimer-7m15m` or `F5JTimer-5m10m`
- **Score:** Flight time + height penalty (motor-cut height from altimeter; lower is better)
- **Data fields:** Data2 = FlightTime, Data6 = Height (motor cut altitude, metres), Data7 = Landing
- **Height penalty:** applied by GliderScore internally from the `Height` field in the CSV — similar concept to F5K but goes in the CSV unlike F5K

#### F3B (GSCompClass = `F3B`, TaskNo = 1/2/3)

Three separate tasks in one competition (TaskNo distinguishes them):

| TaskNo | Task | Description |
|---|---|---|
| 1 | Distance | Count laps in 4 min (each lap = ~150m); Data1 = Laps |
| 2 | Speed | Fly 4 laps as fast as possible; Data2 = FlightTime |
| 3 | Duration | 7 min duration + landing; Data2 = FlightTime, Data7 = Landing |

- **Timer profile:** `F3B-Distance` (2m20s/4m), `F3B-Speed` (5m/1m), `F3B-Duration` (3m/7m/30s)

#### F3F / Speed (GSCompClass = `Spd`, TaskNo = 3)

- Speed task: fly 10 laps on a slope; Data2 = FlightTime
- Not a priority — no local F3F competitions currently

#### ALES / DurALES (GSCompClass = `DurALES`, TaskNo = 1)

Altitude-limited electric soaring. Similar to F5J but with a hard altitude ceiling.

### What to build first for expandability

When implementing Phase A, do NOT hardcode F3K assumptions into the normalisation or DB layers. Specifically:

1. **`scoring.py`** — put all task rules behind a discipline dispatcher from day one, even if only F3K and F5K are implemented
2. **`db.py`** — the `discipline` column on rounds already handles F3K/F5K; extend it to accept `F3J`/`F5J`/`F3B` without migration
3. **`app.py`** — CSV export already reads `discipline` per round; no change needed
4. **Task letter namespace** — F3K tasks A–N and F5K tasks A–E share letters with different meanings; future disciplines also reuse letters. Always qualify as `(discipline, task_letter)` pair, never just the letter alone

---

## Localisation (Future)

GliderScore supports multiple UI languages — the full language list is in `GliderScoreDataStructure.mdb` and includes at minimum English, German, French, Dutch, Czech, and others used across European glider competition circuits.

Our base station is currently English-only. Language support is **not a current priority** but should be kept in mind as a future feature:

- All user-facing strings in Jinja2 templates should eventually go through a translation layer (e.g. Flask-Babel / Jinja2 `gettext`)
- Audio announcements are already language-neutral — the `.wav` files from GliderScore are English but the GliderScore audio library has multilingual variants in some installations
- The GliderScore audio library JSON (`gliderscore_audio_library.json`) is English — if multilingual audio is needed, additional `.wav` sets would be required
- For now: keep all strings in one place (no scattered hardcoded text in Python) so extraction for translation is straightforward when the time comes

---

## GliderScore Integration — What Stays, What Becomes Optional

The key principle: **GliderScore is a supported workflow, not a dependency.** Everything we build is additive — existing GliderScore paths remain fully functional.

| Feature | GliderScore path | Base station path | Status |
|---|---|---|---|
| Timer/audio profiles | GliderScore Digital Timer / Big Timer | Our audio engine (GliderScore-identical) | Both work today |
| Score recording | eScoring / CSV import | Timer protocol → SQLite | Both work today |
| `.mdb` import (pilots/draw) | Export from GliderScore desktop | — | Works today; stays |
| CSV export | — | 15-field GliderScore CSV (`/export`) | Works today; stays |
| Direct DB sync | — | `gs_sync.py` / F3KSync.exe | Works today; stays |
| F3K/F5K task scoring | GliderScore calculates | Our scoring engine | Phase A1 (new) |
| Normalisation engine | GliderScore calculates | Our engine | Phase A3/A4 (new) |
| Standings leaderboard | GliderScore desktop | Our leaderboard page | Phase C1 (new) |
| Group draw | GliderScore draw tool | Our FAI rotation | Phase D1 (new) |
| Online upload | gliderscore.com | Self-hosted public endpoint | Phase F1 (new) |
| Windows desktop | Required today | Optional after Phase A–C | Goal |
| Multiple disciplines (F3J, F3B, F5J) | Supported by GliderScore | Planned (expandable framework) | Defer |

---

## Effort Summary & Critical Path

| Phase | Tasks | Effort estimate | Blocked by |
|---|---|---|---|
| **A — Scoring engine** | A1–A4 | High — ~3–4 sessions | nothing |
| **B — Data model** | B1–B3 | Low — ~1 session | A2 (bonus calc) |
| **C — Leaderboard UI** | C1–C3 | Medium — ~2 sessions | A1–A4, B |
| **D — Group draw** | D1–D2 | Medium — ~1–2 sessions | A4 (standings) |
| **E — F5K altitude UI** | E1 | Low — ~0.5 session | B1 |
| **F — Publishing** | F1–F2 | Low–Medium — ~1 session | C1 |
| **G — Import independence** | G1–G2 | Low — ~0.5 session | nothing |

**Critical path: A1 → A3 → A4 → C1 (leaderboard)**

Everything else is parallel once A1 (task rules engine) exists.

---

## Recommended Starting Point

**A1 is the linchpin.** A `scoring.py` module with:

```python
def score_group(task: str, variant: int, flights_ms: list[int]) -> list[int]:
    """Return list of scored flight times in ms (zero for unscored flights)."""
    ...
```

It is pure Python, fully testable without a UI, and unblocks all downstream work.  
The F3K/F5K task catalogue is fully documented in `GLIDERSCORE.md` and `Knowledge_Base/GLIDERSCORE_FULL_REFERENCE.md` — no further research needed.

---

## Key Reference Files

| Item | Path |
|---|---|
| GliderScore integration reference | `GLIDERSCORE.md` |
| GliderScore full DB reference | `Knowledge_Base/GLIDERSCORE_FULL_REFERENCE.md` |
| Current base station app | `base_station/frontend/app.py` |
| Current DB schema | `base_station/frontend/db.py` |
| GliderScore import | `base_station/frontend/gs_import.py` |
| GliderScore direct sync tool | `tools/gs_sync.py` |
| This file | `SCORING_ENGINE_PROJECT.md` |
