import aiosqlite


async def calc_rainfall(
    new_total: float | None,
    new_time: float,
    db: aiosqlite.Connection,
) -> tuple[float, float]:
    """雨量の差分と毎時雨量を計算して返す。"""
    if new_total is None:
        return (0.0, 0.0)

    cursor = await db.execute(
        """
        SELECT recorded_at, rainfall_total
        FROM weather_records
        ORDER BY recorded_at DESC
        LIMIT 1
        """
    )
    prev = await cursor.fetchone()

    if prev is None:
        return (0.0, 0.0)

    prev_time, prev_total = prev["recorded_at"], prev["rainfall_total"]

    if prev_total is None:
        return (0.0, 0.0)

    delta_mm = new_total - prev_total

    # 電源再投入で累計リセットされた場合
    if delta_mm < 0:
        return (0.0, 0.0)

    delta_sec = new_time - prev_time
    if delta_sec <= 0:
        return (0.0, 0.0)

    rate_mm_h = delta_mm / delta_sec * 3600
    return (delta_mm, rate_mm_h)
