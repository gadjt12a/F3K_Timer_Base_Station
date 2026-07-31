# F3K / F5K Timer System — Rules and Decisions

**A review document.** Everything below is a rule the system actually enforces today.
Each one was a decision made during development, and each one could be wrong.

**Who this is for:** someone who has run competitions and flown them, and who can say
"that is not how it works" — which is worth far more than another code review.

**How to read it.** Every rule has a confidence marker:

| marker | meaning |
|---|---|
| ✅ | Taken from GliderScore 6.78 or FAI rules and verified against real data. Change only with a good reason. |
| ⚠️ | **A judgement call.** Defensible, but made by someone learning the sport. **These are the ones to attack.** |
| ❓ | **Known gap or simplification.** We know it is incomplete and shipped it anyway. |

**How to challenge one.** Quote the rule number (e.g. R-14). Every rule says where it
lives in the code, so a decision can be changed without hunting for it.

**Terminology:** *working time* / *working window* = the flight window. *Round* = one
task flown by everyone. *Group* / *heat* = the pilots flying that window together.
*Base station* = the Raspberry Pi running the competition. *Timer* = the handheld unit
the timekeeper holds.

---

## 1. What the system is, and what it is not

**R-01 ✅ The system times and records; GliderScore remains the scorer of record.**
Results export as a GliderScore-compatible CSV and the CD scores there. The built-in
scoring engine exists so the CD can see live standings during the day.

> `base_station/frontend/app.py` (export), `GLIDERSCORE.md`

**R-02 ⚠️ The built-in scoring engine is a second opinion, not an authority.**
It implements the task rules independently. If it and GliderScore disagree, **GliderScore
is right and we have a bug**. It has not yet been validated against a full scored
competition — that is an open task.

> `base_station/frontend/scoring.py`

**R-03 ✅ Everything works with no internet.** The field system is three isolated
networks with no uplink. Nothing depends on a cloud service or a phone signal.

---

## 2. The round: prep, working window, landing

**R-04 ⚠️ A round runs prep → working window → landing window.** Prep and landing
lengths are per-competition settings. The defaults are **prep 2:00, landing 0:30**, with
a 30 s gap between heats and between rounds, a 45 s "focus time" and a 15 s final count.

*Worth challenging:* 2 minutes of prep is shorter than the 3 minutes F3K commonly uses,
and it is what every new competition starts with unless the CD changes it. If 3:00 is the
sensible default, that is a one-line change.

> `base_station/frontend/db.py` — competition defaults; `state_machine.py` — sequence

**R-05 ⚠️ The base station starts the round; the timekeeper cannot.** While a timer is
connected to the base, its start buttons are **locked**. Every timer in the heat starts
on the same broadcast, so the group is on one clock.

*Why:* independent starts drift, and the pilot flying nearest the CD gets a different
window from the one at the far end of the line.

*Worth challenging:* a timekeeper who wants to start their own pilot cannot. Is that
right, or should there be a manual override for a timer that has lost the link?

> `F3K_Timer_1/src/main.cpp` — `STATE_IDLE`, start lockout when connected

**R-06 ⚠️ If the start signal is lost, the timer opens the window itself 250 ms after
its own prep clock hits zero.** The prep clock is re-synced from the base every second
during the last 10 seconds, so its zero is accurate to about one packet.

*Why:* a dropped packet must never cost a pilot part of their window. Waiting even a
second for a start that may never come opens the window late for that pilot only.

*Worth challenging:* 250 ms is chosen to let a healthy link win the race. A pilot on a
degraded link could start a fraction before the rest of the group.

> `config.h` — `PREP_START_GRACE_MS`

**R-07 ⚠️ The CD can skip the prep countdown forward** (e.g. jump to "1 minute to go"
when the line is ready early). Only during prep, never during the window.

> `state_machine.py` — `skip_prep_to`

**R-08 ⚠️ The landing window is a countdown after the working window closes, and the
timer shows it.** At zero the timer moves to the results screen. The timekeeper can skip
ahead with the right-hand button.

❓ **The system does not enforce anything about landings.** It does not know where a
model landed, and it applies no landing bonus or penalty. If your rules score landings,
that happens in GliderScore.

---

## 3. Launching and jumped starts

**R-09 ⚠️ A launch before the window opens is recorded, flown, and then invalidated.**
The right-hand button unlocks **2 seconds** before the window opens. If the timekeeper
starts a flight in that window, the flight runs on screen marked `JUMPED` in red, and
when it stops it is **automatically scratched** — it never counts, and never reaches the
results.

*Why:* the alternative is refusing to start the clock, which leaves the timekeeper
fighting the device while the model is already in the air.

**R-10 ⚠️ A jumped start is reported to the CD as a note, and is never stored as a
flight.** The CD sees it on the run screen. It is not in the database and cannot appear
in results.

*Worth challenging:* should a jumped start be a **scoring penalty** rather than just a
discarded flight? Right now the pilot simply loses that launch. Under some
interpretations a jumped start should cost more than the launch itself.

> `main.cpp` — `STATE_PREP`, `_recordFlight`; protocol message `JUMPED`

**R-11 ⚠️ Why 2 seconds?** Wide enough to catch a genuine early launch, narrow enough
that it is not a general "start early" button. The unlock deliberately stays open at
zero, because the local clock can reach zero up to a second before the start signal
arrives, and that gap is exactly when a pilot jumps.

---

## 4. Flights and scratching

**R-12 ✅ A flight is timed from launch to landing by the timekeeper's button presses,**
recorded to hundredths, and sent to the base station immediately.

**R-13 ✅ Up to 10 flights per pilot per round are recorded.** More launches than the
task allows are recorded but not scored — see R-20.

**R-14 ⚠️ Scratching discards the most recent flight that has not already been
scratched.** The timekeeper presses left, then confirms within 2 seconds. There is no
way to scratch an *earlier* flight from the timer.

*Worth challenging:* is "most recent" always what a caller means? A caller who realises
two flights later that one was invalid cannot fix it on the timer — the CD must do it
on the base station.

> `main.cpp` — `STATE_SCRATCH_CONFIRM`; `FlightLog::scratchLast`

**R-15 ⚠️ A scratched flight is kept and marked, never deleted.** It shows struck
through in red on the results screen. It is excluded from scoring and excluded from the
GliderScore export.

*Why kept:* an audit trail if a pilot disputes the call, and a deleted row would be
silently re-created by the end-of-round safety resend (R-27).

**R-16 ⚠️ A scratch is a timekeeper decision and needs no CD confirmation.** The timer
already requires a deliberate confirm press.

*Worth challenging:* at a serious competition, should a scratch require the CD to
countersign? Currently the caller's word is final and the CD only sees the result.

---

## 5. Task scoring

**R-17 ✅ Flight times are truncated, not rounded**, to one decimal place by default
(configurable per competition). 59.98 s scores 59.9, not 60.0. This matches GliderScore.

**R-18 ✅ The task rules implemented.** "Cap" = the longest time that flight can score.

### F3K

| Task | Rule as implemented | Cap |
|---|---|---|
| **A** | Last flight counts | 5:00 |
| **B** | Last 2 flights count | 4:00 (variant B(2): 3:00) |
| **C** | First 3 flights count (all-up) | 3:00 each (variants: 4 or 5 flights) |
| **D** | Ladder: first flight ≥ 30 s scores 30, then target rises by 15 s each time | — |
| **D(1)** | First 2 flights count | 5:00 |
| **E** | Poker — best 5 flights, uncapped ⚠️ see R-19 (variants E(1)/E(2): best 3) | none |
| **F** | Best 3 of at most 6 launches | 3:00 |
| **G** | Best 5 flights | 2:00 |
| **H** | 4 flights matched to targets 1:00 / 2:00 / 3:00 / 4:00 | per target |
| **I** | Best 3 flights | 3:20 |
| **J** | Last 3 flights | 3:00 |
| **K** | 5 flights in order, capped 1:00 / 1:30 / 2:00 / 2:30 / 3:00 | per slot |
| **L** | First flight only | 9:59 ⚠️ |
| **M** | 3 flights in order, capped 3:00 / 5:00 / 7:00 | per slot |
| **N** | Best single flight | 10:00 |
| **U10 / U15** | Every flight counts | none |

### F5K

| Task | Rule as implemented | Launches |
|---|---|---|
| **A** | 4 flights matched to targets 1:00 / 2:00 / 3:00 / 4:00 | 4 |
| **B** | Last flight counts, cap 5:00 | 3 |
| **C** | First 3 flights, cap 4:00 | — |
| **D** | 3 flights matched to targets 3:00 / 3:00 / 4:00 | 3 |
| **E** | Poker — best 3 ⚠️ see R-19 | 3 |

> `scoring.py` — `F3K_RULES`, `F5K_RULES`

**R-19 ❓ Poker (task E) is not really implemented.** A declared target is central to
Poker, and **the system never records a declaration**. It simply takes the achieved
times and scores the best *n*. A pilot who declares 3:00 and flies 2:30 should score
nothing for that flight; here they score 2:30.

**This is the single biggest known gap.** It needs either a way for the timekeeper to
enter a declaration on the timer before the launch, or an explicit decision that Poker
is scored by the CD in GliderScore and the timer only records times.

**R-20 ⚠️ Where a task limits launches, extra flights are ignored in the order flown** —
the first *n* are kept, later ones dropped. They are still recorded and visible.

*Worth challenging:* is first-*n* right, or should a pilot who over-launches keep their
*best* allowed flights? First-*n* is the stricter reading.

**R-21 ⚠️ "Matched to targets" tasks (H, F5K A, F5K D) pair the longest flight with the
largest target,** second-longest with second-largest, and so on, each scoring the lesser
of flight and target. This maximises the pilot's score.

*Worth challenging:* it assumes the pilot is always given the most favourable
assignment. If your rules require the pilot to nominate which flight targets which slot,
this is wrong.

**R-22 ⚠️ An unknown task scores every flight rather than failing.** If a round
references a task the system does not know (e.g. a custom task deleted mid-competition),
it counts all flights instead of crashing the results page.

*Why:* a scoring page that fails mid-competition is worse than one that is generous and
visibly wrong.

---

## 6. F5K height bonus

**R-23 ✅ Bonus points by motor-cut height,** relative to a reference height (default
**60 m**, settable per competition and per round):

| Height vs reference | Points |
|---|---|
| Below | **+0.5 per metre**, no limit |
| 1–10 m above | **−1 per metre** |
| 11 m+ above | −1/m for the first 10 m, then **−3 per metre** beyond |

Example: 15 m above reference = −10 (first 10 m) − 15 (5 m at −3) = **−25 points**.

Verified against GliderScore's bonus table and the NZ Nationals 2026 database.

**R-24 ⚠️ The bonus only applies to a flight that counts and is at least 30 seconds**
(the minimum is configurable). A flight that does not score gets no bonus and no penalty.

*Worth challenging:* a pilot who launches very high and lands immediately currently
escapes the height penalty entirely, because the flight is too short to qualify. Is that
intended, or should the penalty apply regardless?

**R-25 ⚠️ Heights are entered by the timekeeper on the timer after the round,** one
flight at a time, and can be overridden by the CD. Every height records whether it came
from the timer or the CD.

❓ **Heights are not exported to GliderScore.** The CSV carries flight times only;
GliderScore applies the bonus itself from heights the CD enters there. Confirmed against
a scored F5K competition, but it does mean heights are typed twice.

---

## 7. When things go wrong

**R-26 ✅ Every flight, scratch, height and pilot selection must be acknowledged by the
base station.** The timer holds each message until the base echoes it back, and retries
until it does — through a dropped link, a base station restart, or a flat battery on the
Wi-Fi.

**R-27 ✅ At the end of every round the timer re-reports the whole round from its own
memory.** Anything the base missed is recovered; anything it already has is recognised
and ignored. Recovered flights are flagged loudly to the CD, because the log changed
after the round ended.

**R-28 ✅ The timer keeps the last 3 rounds in permanent memory,** written flight by
flight as they happen. A timer that loses power mid-round keeps everything already
flown, and the round can be recovered without the base station.

**R-29 ⚠️ A duplicate is defined as: same pilot, same group, same time to the
millisecond.** Two genuinely identical flights in one group are treated as one.

*Worth challenging:* two flights matching to the millisecond is vanishingly unlikely —
but it is not impossible, and if it happened the pilot would silently lose one.

---

## 8. Normalisation, drops and standings

**R-30 ✅ Within a group, the best raw score scores 1000** and everyone else scores
`their score ÷ best × 1000`, truncated to one decimal.

**R-31 ⚠️ A group where nobody scores gives everyone 0, not 1000.** If no one in the
group posts a time, no one gets the winner's points.

**R-32 ⚠️ A round with no flights recorded at all does not count** toward the
competition, and is not a drop candidate — it simply did not happen.

**R-33 ✅ Drop scores** are configured per competition: drop 1 becomes active after round
*n*, and so on for up to three drops. The lowest scores are dropped.

**R-34 ⚠️ On tied scores, the earliest round is dropped.**

**R-35 ⚠️ Ties in the standings are broken on the last round flown, then working
backwards** — FAI style.

*Worth checking:* the tie-break compares each round's score **including rounds that were
dropped**. A stricter reading might compare only counting rounds.

> `scoring.py` — `normalise`, `standings`, `active_drops`

---

## 9. The draw

**R-36 ⚠️ Round 1 is drawn at random. Later rounds are seeded by reverse standings,
snaked across groups** — so the leaders are spread out rather than all flying together.

**R-37 ⚠️ The multi-round draw optimises for every pilot flying with every other pilot
at least once.** Meeting a new pilot is weighted heavily; repeat meetings are mildly
discouraged.

**R-38 ⚠️ Back-to-back flying is avoided** — a pilot in the last group of one round and
the first group of the next gets no break. With **3 or more groups this is treated as
effectively mandatory**; with only 2 groups it is a preference, because forcing it would
freeze the group composition for the whole competition.

*Worth challenging:* is one group's gap enough rest, or should it be two?

**R-39 ⚠️ The draw warns if there are not enough non-flying pilots to timekeep** — it
assumes one timekeeper per flying pilot, drawn from the pilots not in that group.

*Worth challenging:* this assumes pilot-timekeepers. A competition with dedicated
helpers does not need the warning.

**R-40 ⚠️ Group sizes differ by at most one, and the earlier groups get the extra
pilot.**

> `base_station/frontend/draw.py`

---

## 10. Audio

**R-41 ✅ The audio cues are GliderScore's own**, extracted from its database — the same
announcements and beeps, at the same times, so a pilot hears what they are used to.

**R-42 ⚠️ Working times GliderScore has no profile for get a generated cue schedule**
rather than running in silence.

**R-43 ⚠️ The last 10 seconds of the working and landing windows are beeps, not spoken
numbers.** Spoken counts overlapped each other at one-second spacing.

**R-44 ⚠️ The spoken cue "to land" is deliberately suppressed.** It fired one second
before the landing beeps and clipped them. "Pilots must land" already makes the call 17
seconds earlier, and the timer shows the landing clock.

**R-45 ✅ Landing cues are re-anchored to the competition's actual landing window,** not
the length in GliderScore's profile.

**R-46 ✅ Audio latency was removed rather than compensated.** Cues fire on the beat
with no offset. ⚠️ **Bluetooth speakers are the exception** — they add 100–200 ms that
cannot be removed and must be dialled out per speaker.

> `base_station/frontend/audio.py`

---

## 11. Deliberate omissions

Things the system does **not** do, on purpose. Each is a candidate for challenge.

- **❓ No landing scoring or bonus** (R-08).
- **❓ No Poker declarations** (R-19) — the largest gap.
- **❓ No penalties of any kind.** No airframe penalties, no safety penalties, no
  late-landing penalties. There is a penalty field in the database but nothing sets it.
- **❓ No fly-off handling.** A fly-off is run as ordinary extra rounds.
- **❓ No airframe or model registration**, so no model-change rules.
- **❓ No wind or weather record**, and no round abandonment/re-fly workflow. A round is
  re-flown by deleting the flights and running it again.
- **❓ No handicapping or age/junior classes** in scoring — pilots are one field.
- **❓ Group timekeeper assignment is not tracked**; the draw only warns whether enough
  pilots exist to fill the role (R-39).

---

## 12. Where the rules live

| Area | File |
|---|---|
| Task rules, normalisation, drops, F5K bonus | `base_station/frontend/scoring.py` |
| Round flow (prep / working / landing) | `base_station/frontend/state_machine.py` |
| Group draw | `base_station/frontend/draw.py` |
| Audio cues | `base_station/frontend/audio.py` |
| Flight recording, duplicates, scratching | `base_station/server.py` |
| GliderScore export | `base_station/frontend/app.py` |
| Timer behaviour (jumped start, scratch, heights) | `F3K_Timer_1/src/main.cpp` |
| Timer timings and limits | `F3K_Timer_1/include/config.h` |
| Every defect and its reasoning | `ISSUES.md` |

Rules marked ✅ are pinned by automated tests (169 of them) — changing one without
updating its test will fail the build.

---

## 13. The five questions most worth your time

If you read nothing else:

1. **Poker (R-19).** We do not record declarations. Is scoring achieved times an
   acceptable simplification, or does it make task E meaningless?
2. **Jumped starts (R-10).** The pilot loses the launch. Should it cost more?
3. **Over-launching (R-20).** Extra flights are dropped in the order flown, not by
   quality. Is first-*n* the right reading?
4. **Target matching (R-21).** We always give the pilot the most favourable pairing of
   flights to targets. Is that how it is scored?
5. **The height-bonus gate (R-24).** A very high launch with a very short flight escapes
   the penalty. Should it?
