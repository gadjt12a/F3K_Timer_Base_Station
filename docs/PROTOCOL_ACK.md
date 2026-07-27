# TCP Protocol — ACK Extension

Implemented in base station session 47 (2026-07-24).

## Background

`FLIGHT`, `ALTITUDE`, and `SELECT` were fire-and-forget. If the timer's TCP socket
died silently (AP glitch — no FIN/RST), lwIP discards the write but the timer has no
way to detect this at send time. The flight record is lost. Application-level ACKs close
this gap: the timer holds each message in a pending queue and only dequeues on receiving
the matching ACK; it retransmits anything still pending on reconnect after ASSIGN.

## Base station behaviour (session 47+)

After successfully processing `FLIGHT`, `ALTITUDE`, or `SELECT`, the base replies:

```
ACK <original message verbatim>\n
```

Examples:
```
<< FLIGHT pilot=3 dur=125430
>> ACK FLIGHT pilot=3 dur=125430

<< ALTITUDE pilot=3 flight=2 alt=142
>> ACK ALTITUDE pilot=3 flight=2 alt=142

<< SELECT pilot=5
>> ACK SELECT pilot=5
```

The ACK echoes the message byte-for-byte so the timer can match by exact string
comparison against its pending queue.

`PING` is NOT ACKed — the existing `PONG` reply is unchanged.
`JOIN`/`ASSIGN` handshake is unchanged.

## Idempotency / dedup (base station)

Because the timer retransmits unACKed messages on reconnect, the base may receive the
same message more than once:

- **FLIGHT**: deduplicated on `(pilot_id, group_id, duration_ms)` — same pilot + exact
  millisecond duration within the same group is treated as a duplicate regardless of when
  it arrives. The base ACKs the duplicate but does not insert a second flight row.
- **ALTITUDE**: the UPDATE is naturally idempotent (sets the same value again). The base
  ACKs and runs the UPDATE either way.
- **SELECT**: idempotent (updates `last_pilot_id`, broadcasts). ACKed either way.

## ACKs are unconditional

The base ACKs `FLIGHT`, `JUMPED`, `ALTITUDE` and `SELECT` **whenever it receives them**,
including messages it deliberately discards — a `FLIGHT pilot=0` from a timer that
reconnected and lost its pilot selection, or a duplicate replayed after reconnect.

`ACK` means *"received and decided"*, not *"stored"*. This matters because the timer
cannot distinguish "you ignored it" from "it never arrived": withholding an ACK from a
message the base drops on purpose puts the timer in a retry loop it can never escape.

Three of the four handlers originally ACKed inside an `if pilot_id > 0` branch. That was
harmless while the timer ignored ACKs, and would have become an infinite retry the moment
the timer side shipped. Locked down by `base_station/tests/test_protocol.py`.

## Timer side (implemented, fw-v16)

`TimerComms`:
1. **Queues every** `FLIGHT`/`JUMPED`/`ALTITUDE`/`SELECT` before sending — including on a
   socket that looks healthy. Sending is not proof of delivery.
2. Drops the entry only on `ACK <msg>`, matched byte-for-byte against the queue.
3. Resends the whole backlog on `ASSIGN` (i.e. after every reconnect), oldest first.
4. Retries anything un-ACKed after `ACK_RETRY_MS` (5 s) while connected.

The buffer is a plain 16-entry array rather than the previous ring: ACKs let entries leave
from the middle, which a head/tail ring cannot express. When it fills, the **newest**
message is dropped with a loud log — the older entries have already been attempted and are
closer to confirmation. That is still data loss, so it is logged, not counted silently.

Backward compatibility: a base older than session 47 never ACKs, so a fw-v16 timer would
retry every message every 5 s and eventually fill its buffer. All deployed bases are
session 47+; if that ever stops being true, the retry needs a cap.
