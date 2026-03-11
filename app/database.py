import aiosqlite

DB_PATH = "/app/data/weather.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id      TEXT    NOT NULL,
    recorded_at     REAL    NOT NULL,
    temperature     REAL,
    humidity        REAL,
    rainfall_total  REAL,
    rainfall_delta  REAL,
    rainfall_rate   REAL,
    wind_dir        REAL,
    wind_avg        REAL,
    wind_gust       REAL,
    illuminance     REAL,
    uv_index        REAL,
    pressure        REAL
);
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_recorded_at ON weather_records(recorded_at);",
    "CREATE INDEX IF NOT EXISTS idx_station_id  ON weather_records(station_id);",
]

_MIGRATIONS = [
    # pressure カラム追加（既存DB対応）
    "ALTER TABLE weather_records ADD COLUMN pressure REAL;",
]


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db() -> None:
    db = await get_db()
    try:
        await db.executescript(_SCHEMA)
        for idx in _INDEXES:
            await db.execute(idx)
        # マイグレーション（既存DB向けカラム追加）
        for sql in _MIGRATIONS:
            try:
                await db.execute(sql)
            except Exception:
                pass  # カラムが既に存在する場合は無視
        await db.commit()
    finally:
        await db.close()
