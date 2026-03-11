import aiosqlite


async def calc_rainfall(
    new_total: float | None,
    new_time: float,
    db: aiosqlite.Connection,
) -> tuple[float, None]:
    """雨量の差分を計算して返す。rate は backfill で後から算出する。"""
    if new_total is None:
        return (0.0, None)

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
        return (0.0, None)

    prev_time, prev_total = prev["recorded_at"], prev["rainfall_total"]

    if prev_total is None:
        return (0.0, None)

    delta_mm = new_total - prev_total

    # 電源再投入で累計リセットされた場合
    if delta_mm < 0:
        return (0.0, None)

    return (delta_mm, None)


async def _find_neighbor(
    db: aiosqlite.Connection,
    target_time: float,
    direction: str,
    window: float = 30.0,
) -> tuple[float, float] | None:
    """対象時刻の前後レコードを探す。
    まず ±window 秒以内を探し、なければ直近レコードを返す。
    Returns (recorded_at, rainfall_total) or None.
    """
    if direction == "before":
        # 30秒ウィンドウ内
        cursor = await db.execute(
            "SELECT recorded_at, rainfall_total FROM weather_records "
            "WHERE recorded_at >= ? AND recorded_at < ? AND rainfall_total IS NOT NULL "
            "ORDER BY recorded_at DESC LIMIT 1",
            (target_time - window, target_time),
        )
        row = await cursor.fetchone()
        if row:
            return (row["recorded_at"], row["rainfall_total"])
        # フォールバック: 直前レコード
        cursor = await db.execute(
            "SELECT recorded_at, rainfall_total FROM weather_records "
            "WHERE recorded_at < ? AND rainfall_total IS NOT NULL "
            "ORDER BY recorded_at DESC LIMIT 1",
            (target_time,),
        )
        row = await cursor.fetchone()
        if row:
            return (row["recorded_at"], row["rainfall_total"])
    else:
        # 30秒ウィンドウ内
        cursor = await db.execute(
            "SELECT recorded_at, rainfall_total FROM weather_records "
            "WHERE recorded_at > ? AND recorded_at <= ? AND rainfall_total IS NOT NULL "
            "ORDER BY recorded_at ASC LIMIT 1",
            (target_time, target_time + window),
        )
        row = await cursor.fetchone()
        if row:
            return (row["recorded_at"], row["rainfall_total"])
        # フォールバック: 直後レコード
        cursor = await db.execute(
            "SELECT recorded_at, rainfall_total FROM weather_records "
            "WHERE recorded_at > ? AND rainfall_total IS NOT NULL "
            "ORDER BY recorded_at ASC LIMIT 1",
            (target_time,),
        )
        row = await cursor.fetchone()
        if row:
            return (row["recorded_at"], row["rainfall_total"])

    return None


async def backfill_rainfall_rate(db: aiosqlite.Connection) -> None:
    """rainfall_rate が NULL かつ最新レコードの1分以上前のレコードを一括算出・更新。"""
    # 対象レコード抽出
    cursor = await db.execute(
        """
        SELECT id, recorded_at, rainfall_total
        FROM weather_records
        WHERE rainfall_rate IS NULL
          AND recorded_at < (SELECT MAX(recorded_at) - 60 FROM weather_records)
        ORDER BY recorded_at
        """
    )
    targets = await cursor.fetchall()

    if not targets:
        return

    for row in targets:
        rid = row["id"]
        t = row["recorded_at"]
        total = row["rainfall_total"]

        # rainfall_total がない場合は rate=0
        if total is None:
            await db.execute(
                "UPDATE weather_records SET rainfall_rate = 0.0 WHERE id = ?",
                (rid,),
            )
            continue

        before = await _find_neighbor(db, t, "before")
        after = await _find_neighbor(db, t, "after")

        if before is None or after is None:
            await db.execute(
                "UPDATE weather_records SET rainfall_rate = 0.0 WHERE id = ?",
                (rid,),
            )
            continue

        delta_total = after[1] - before[1]
        delta_time = after[0] - before[0]

        if delta_total < 0 or delta_time <= 0:
            rate = 0.0
        else:
            rate = delta_total / delta_time * 3600  # mm/h

        await db.execute(
            "UPDATE weather_records SET rainfall_rate = ? WHERE id = ?",
            (rate, rid),
        )
