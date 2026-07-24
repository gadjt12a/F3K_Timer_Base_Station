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

## Timer side (to be implemented)

The timer should:
1. Enqueue `FLIGHT`, `ALTITUDE`, `SELECT` in a pending ring buffer on send (do not dequeue
   at send time).
2. On receiving `ACK <msg>`, dequeue the matching entry.
3. On reconnect (after ASSIGN is received), retransmit everything still in the pending
   queue in original order.

Backward compatibility: timers on firmware ≤ fw-v12 that don't send ACKs will simply log
`ACK ...` lines as unknown messages — harmless. ACKs from the base are always sent; the
timer can ignore them until the retry logic is implemented.
