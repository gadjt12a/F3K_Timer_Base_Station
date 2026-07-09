# GliderScore Integration Reference

*Last updated: 2026-07-06*

---

## What GliderScore Is

Windows-only desktop scoring application for RC glider competitions. Supports F3B, F3G, F3F, F3J, F3K, F3L, F3Q, F5B, F5J, F5K, F5L, Thermal, Electric, ALES.  
Website: https://gliderscore.com/  
Contact: gerry.carter(at)gliderscore.com

---

## Current Manual Workflow (what we are replacing)

```
Timer → paper time sheet → handed to pilot
→ pilot scans QR code → enters data into online score card (gliderscore.com)
→ organiser downloads from server into GliderScore desktop
→ GliderScore calculates scores / normalisation
→ organiser uploads results to online competition table
```

**Problems:** multiple manual transcription steps, room for errors, delays.

## Target Workflow (what we are building)

```
Timer → base station (automatic flight time capture)
F5K: timekeeper enters altitudes on timer at WT expiry → sent to base station
→ organiser exports CSV from base station web UI
→ imports into GliderScore desktop
→ GliderScore calculates scores / normalisation (unchanged)
→ organiser uploads results to online competition table (unchanged)
```

**We are a data recorder, not a scorer.** GliderScore does all normalisation/points maths.

---

## Integration Approach

- **No API.** GliderScore has no public API.
- **Export-only.** We produce a CSV file that GliderScore desktop can import via its "CSV File Interface for External Scoring Systems."
- **Import side TBD.** We need to load the competition (pilot list, round/group draw, tasks) from a GliderScore export. Format unknown — need a sample export from GliderScore desktop.

---

## eScoring Architecture (for reference)

GliderScore's own eScoring system (which we replace) works via their server:
- Desktop uploads competition parameters to gliderscore.com server
- Pilots submit scores via: QR code → browser, `eScoringInterface.exe`, or `gliderscore.com/escoreinterface.aspx`
- Desktop downloads submitted scores and recalculates
- Can run on an automatic cycle (preset interval)

This tells us GliderScore's server holds CompID-keyed data. We do NOT interact with their server — we produce a local file for desktop import only.

---

## CSV Export Format (External Scoring Systems)

### Field order (15 fields, comma-separated)

```
CompNo, TaskNo, RoundNo, GroupNo, ReFlightNo, PilotNo, Data1, Data2, Data3, Data4, Data5, Data6, Data7, Penalty, PilotName
```

| Field | Notes |
|---|---|
| CompNo | Competition identifier from GliderScore |
| TaskNo | Task identifier (e.g. task A=1, B=2... or GliderScore internal ID — TBC) |
| RoundNo | Round number |
| GroupNo | Group number within the round |
| ReFlightNo | 0 for normal flights, >0 for re-flights |
| PilotNo | GliderScore internal pilot number |
| Data1–Data7 | Competition-type-specific (see table below) |
| Penalty | 0 or penalty value |
| PilotName | Pilot name string |

### Time format

`mmss.sss` — e.g. 83.4 seconds = `123.400`, 10 minutes 0 seconds = `1000.000`

### Data field mapping by competition type

| Type | Data1 | Data2 | Data3 | Data4 | Data5 | Data6 | Data7 |
|---|---|---|---|---|---|---|---|
| **F3K** | FlightTime1 | FlightTime2 | FlightTime3 | FlightTime4 | FlightTime5 | FlightTime6 | FlightTime7 |
| Duration | 0 | FlightTime | 0 | FlightTime2* | 0 | 0 | Landing |
| F3J | 0 | FlightTime | 0 | FlightTime2* | 0 | Late penalty | Landing |
| Duration+Motor Run | 0 | FlightTime | 0 | FlightTime2* | 0 | Motor run | Landing |
| F5J | 0 | FlightTime | 0 | FlightTime2* | 0 | Height | Landing |
| Distance (Laps) | Laps | 0 | 0 | 0 | 0 | 0 | 0 |
| Speed | 0 | FlightTime | 0 | FlightTime2* | 0 | 0 | 0 |
| F5B | Laps | FlightTime | 0 | FlightTime2* | 0 | Motor run | Landing |

*FlightTime2: dual timekeeper only, otherwise 0.

### F3K example row

Pilot 7 ("Alice Smith"), round 2, group 1, task D, 3 flights: 83.4s / 45.0s / 112.1s

```
1234,4,2,1,0,7,123.400,045.000,112.100,0,0,0,0,0,Alice Smith
```

### F5K — STATUS: UNKNOWN

F5K is **not in GliderScore's published CSV field table**. Need to determine:
- How per-flight motor cut altitudes map to Data1–Data7
- Whether altitude is per-flight (multiple rows?) or a single value per score card
- F5K scoring: 1 point per second of flight time, with bonus/penalty relative to Nominal Launch Height (NLH) — NLH can be changed per round

**Action:** Enter a sample F5K score in GliderScore desktop and use the CSV export to observe the field mapping.

---

## F3K Task Catalogue (for TaskNo mapping — TBC)

GliderScore's internal TaskNo values are unknown. Need to confirm from a GliderScore export or the desktop UI.

| Task | Description | Max single flight | Working time |
|---|---|---|---|
| A | Last flight only | 300s | 7 or 10 min |
| B | Next-to-last + last | 240s (180s large field) | 10 min (7 min) |
| C | All up, last down (3–5 simultaneous) | 180s each | = sum of flights |
| D | Two flights summed | 300s each | 10 min |
| E | Poker (up to 3 self-nominated targets) | ≤ working time | 10 or 15 min |
| F | Best 3 of up to 6 flights | 180s each | 10 min |
| G | Best 5 flights | 120s each | 10 min |
| H | 1-2-3-4 min any order | 60/120/180/240s | 10 min |
| I | Three longest | 200s each | 10 min |
| J | Three last | 180s each | 10 min |
| K | Big Ladder (5 flights in order: 60/90/120/150/180s) | as listed | 10 min |
| L | One flight | 419s or 599s | 7 or 10 min |
| M | Huge Ladder fly-off (3 flights: 180/300/420s) | as listed | 15 min |
| N | Best flight | 599s | 10 min |

Scoring: raw score = sum of scored seconds. Group normalisation: best raw = 1000, others = (own/best) × 1000, rounded to 0.1. Times truncated to 0.1s (not rounded up).

---

## What Still Needs to Be Determined

| Item | Needed for | How to get it |
|---|---|---|
| GliderScore desktop export format | Importing competition into our system (pilot list, rounds, draw) | Export a sample F3K comp from GliderScore desktop |
| F5K CSV field mapping | F5K altitude data in export | Enter sample F5K data in GliderScore desktop, observe CSV export |
| GliderScore internal TaskNo values | Correct TaskNo in our CSV output | Check GliderScore desktop or exported file |
| CompNo format | Required field in every CSV row | From GliderScore desktop export |
| PilotNo format | Required field in every CSV row | From GliderScore desktop export |

**All five items are unblocked by having GliderScore desktop with a sample F3K/F5K competition loaded.**

---

## Combined F3K + F5K Competition Day

Local competitions run F3K and F5K on the same day, alternating after each task or pair of tasks. The pilot pool is the same; rounds alternate discipline. Our system must:

- Know the discipline for each round (F3K or F5K)
- For F5K rounds: collect per-flight motor cut altitudes after WT expires
- Altitudes collected by timekeeper on the handheld timer: pilot reads aloud from their onboard altimeter, timekeeper enters on timer at the "times up" screen
- Altitudes sent from timer to base station as `ALTITUDE flight=N alt=Xm` messages (protocol extension — not yet implemented)

---

## References

- https://gliderscore.com/ — main site
- https://gliderscore.com/Scoring.aspx — eScoring and CSV format documentation
- https://gliderscore.com/CompF3K.aspx — F3K competition details
- https://gliderscore.com/CompF5K.aspx — F5K competition details
- https://gliderscore.com/FAQ.aspx — FAQ including export/import notes
- https://gliderscore.com/OnLineScores.aspx — online scores / upload workflow
- https://github.com/petegee/Soarscore — requirements research doc for a broader RC scoring platform; useful for F3K rules reference and user role definitions
