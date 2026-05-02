"""Rainfall delta and instantaneous rate calculation.

Algorithm
---------
Each physical tip (rainfall_delta > 0, typically 0.3 mm per tip) is split into
virtual 0.1 mm events distributed in time:

  - If a previous tip exists within LOOKBACK_SEC (30 min): generate
    round(delta / 0.1) virtual events at evenly-spaced centroids in (T_prev, T].
  - If no previous tip within LOOKBACK_SEC (isolated tip): generate the same
    number of virtual events evenly distributed over (T - ISOLATED_SPREAD_SEC, T]
    (= the past 30 min). Spread = 30 min keeps the events <=10 min apart so
    the 10-min rate window stays continuously covered.

Anomaly handling
----------------
A "tip" with delta whose apparent rate (delta / gap × 3600) exceeds
MAX_APPARENT_RATE mm/h is treated as a counter-restoration anomaly (e.g.
device reset where the gauge counter jumps back from 0 to its true total
in 32 sec). Such records are rejected at POST time and their deltas are
zeroed during the historical recompute.

100 mm/h is the threshold: well above natural extremes (typhoon peaks
~ 100-150 mm/h are extremely rare) but far below counter-restoration
events (often 10000+ mm/h).

Instantaneous rainfall rate at time t is:

  rate(t) = (sum of virtual 0.1 mm events in (t - 10 min, t]) × 6   [mm/h]

The 10-minute window matches JMA's standard for "10分間降水量".
"""
from __future__ import annotations

import bisect

import aiosqlite

# === Tunable constants ===
TIP_RESOLUTION = 0.1          # mm — virtual event size
LOOKBACK_SEC = 1800.0         # 30 min — max lookback for previous tip
ISOLATED_SPREAD_SEC = 1800.0  # 30 min — span over which isolated tips are distributed
RATE_WINDOW_SEC = 600.0       # 10 min — instantaneous rate window
MAX_APPARENT_RATE = 100.0     # mm/h — apparent rate threshold for anomaly detection


async def is_rainfall_anomaly(
    new_total: float | None,
    new_time: float,
    db: aiosqlite.Connection,
) -> bool:
    """True if the new rainfall_total represents an anomalous jump
    (counter restoration / device reset)."""
    if new_total is None:
        return False

    cursor = await db.execute(
        "SELECT recorded_at, rainfall_total FROM weather_records "
        "ORDER BY recorded_at DESC LIMIT 1"
    )
    prev = await cursor.fetchone()
    if prev is None or prev["rainfall_total"] is None:
        return False

    delta = new_total - prev["rainfall_total"]
    if delta <= 0:
        return False  # negative is reset, handled by calc_rainfall

    gap = new_time - prev["recorded_at"]
    if gap <= 0:
        return False  # out-of-order or same timestamp

    apparent_rate = delta / gap * 3600.0
    return apparent_rate > MAX_APPARENT_RATE


async def calc_rainfall(
    new_total: float | None,
    new_time: float,
    db: aiosqlite.Connection,
) -> tuple[float, None]:
    """Compute rainfall_delta from the previous record's rainfall_total.

    Returns (delta, None). Negative deltas (counter reset) → 0.
    Anomaly check is performed by is_rainfall_anomaly() in the caller.
    """
    if new_total is None:
        return (0.0, None)

    cursor = await db.execute(
        "SELECT rainfall_total FROM weather_records "
        "ORDER BY recorded_at DESC LIMIT 1"
    )
    prev = await cursor.fetchone()

    if prev is None or prev["rainfall_total"] is None:
        return (0.0, None)

    delta = new_total - prev["rainfall_total"]
    if delta < 0:  # device counter reset
        return (0.0, None)
    return (delta, None)


def _virtual_events_for_tip(
    tip_time: float,
    tip_delta: float,
    prev_tip_time: float | None,
) -> list[float]:
    """Convert a single tip event into virtual 0.1 mm event timestamps."""
    n = round(tip_delta / TIP_RESOLUTION)
    if n <= 0:
        return []

    if prev_tip_time is None or (tip_time - prev_tip_time) > LOOKBACK_SEC:
        # Isolated tip: distribute n events evenly over (T - ISOLATED_SPREAD_SEC, T]
        step = ISOLATED_SPREAD_SEC / n
        return [tip_time - ISOLATED_SPREAD_SEC + step * (i + 0.5) for i in range(n)]

    # Distribute n events evenly in (T_prev, tip_time] at sub-interval centroids
    gap = tip_time - prev_tip_time
    step = gap / n
    return [prev_tip_time + step * (i + 0.5) for i in range(n)]


async def _load_virtual_events(
    db: aiosqlite.Connection,
    end_time: float | None = None,
) -> list[float]:
    """Load all tips up to end_time and return a sorted list of virtual event timestamps."""
    if end_time is None:
        cursor = await db.execute(
            "SELECT recorded_at, rainfall_delta FROM weather_records "
            "WHERE rainfall_delta > 0 ORDER BY recorded_at"
        )
    else:
        cursor = await db.execute(
            "SELECT recorded_at, rainfall_delta FROM weather_records "
            "WHERE rainfall_delta > 0 AND recorded_at <= ? "
            "ORDER BY recorded_at",
            (end_time,),
        )
    tips = await cursor.fetchall()

    virtuals: list[float] = []
    prev_tip_time: float | None = None
    for tip in tips:
        T = tip["recorded_at"]
        D = tip["rainfall_delta"]
        virtuals.extend(_virtual_events_for_tip(T, D, prev_tip_time))
        prev_tip_time = T

    virtuals.sort()
    return virtuals


def _rate_from_virtuals(virtuals: list[float], t: float) -> float:
    """Compute rate (mm/h) at time t given a sorted list of virtual event timestamps."""
    lo = bisect.bisect_right(virtuals, t - RATE_WINDOW_SEC)
    hi = bisect.bisect_right(virtuals, t)
    count = hi - lo
    return count * TIP_RESOLUTION * (3600.0 / RATE_WINDOW_SEC)


async def update_recent_rates(
    db: aiosqlite.Connection,
    around_time: float,
) -> None:
    """Recompute rainfall_rate for records within LOOKBACK_SEC of around_time."""
    virtuals = await _load_virtual_events(db, end_time=around_time)

    cursor = await db.execute(
        "SELECT id, recorded_at FROM weather_records "
        "WHERE recorded_at > ? AND recorded_at <= ?",
        (around_time - LOOKBACK_SEC, around_time),
    )
    records = await cursor.fetchall()

    for rec in records:
        rate = _rate_from_virtuals(virtuals, rec["recorded_at"])
        await db.execute(
            "UPDATE weather_records SET rainfall_rate = ? WHERE id = ?",
            (rate, rec["id"]),
        )


async def backfill_rainfall_rate(db: aiosqlite.Connection) -> None:
    """Compatibility wrapper called from main.py after each insert.

    Recomputes rates for the past LOOKBACK_SEC window around the latest record.
    """
    cursor = await db.execute("SELECT MAX(recorded_at) AS t FROM weather_records")
    row = await cursor.fetchone()
    if row is None or row["t"] is None:
        return
    await update_recent_rates(db, row["t"])


async def recompute_all_rates(
    db: aiosqlite.Connection,
    batch_size: int = 5000,
) -> int:
    """One-shot recompute of rainfall_rate for all records. Returns count updated."""
    virtuals = await _load_virtual_events(db)

    cursor = await db.execute(
        "SELECT id, recorded_at FROM weather_records ORDER BY id"
    )
    records = await cursor.fetchall()

    updates = [
        (_rate_from_virtuals(virtuals, rec["recorded_at"]), rec["id"])
        for rec in records
    ]

    for i in range(0, len(updates), batch_size):
        await db.executemany(
            "UPDATE weather_records SET rainfall_rate = ? WHERE id = ?",
            updates[i:i + batch_size],
        )
        await db.commit()

    return len(updates)
