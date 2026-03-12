import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app.database import get_db, init_db
from app.models import WeatherInput, WeatherRecord
from app.rainfall import backfill_rainfall_rate, calc_rainfall

# SSE: 新規データを通知するためのイベント
_sse_clients: list[asyncio.Queue] = []


def _notify_sse(record: dict) -> None:
    for q in _sse_clients:
        q.put_nowait(record)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Weather Station API", lifespan=lifespan)

_static_dir = Path(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)


# --- HTML ---


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/service-worker.js")
async def service_worker():
    """Serve SW from root scope so it can control '/'."""
    sw_path = Path(__file__).parent / "static" / "service-worker.js"
    return FileResponse(
        sw_path,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


# --- API ---


@app.post("/api/weather", status_code=201)
async def post_weather(data: WeatherInput):
    db = await get_db()
    try:
        delta, rate = await calc_rainfall(data.rainfall_total, data.recorded_at, db)

        await db.execute(
            """
            INSERT INTO weather_records
                (station_id, recorded_at, temperature, humidity,
                 rainfall_total, rainfall_delta, rainfall_rate,
                 wind_dir, wind_avg, wind_gust, illuminance, uv_index,
                 pressure)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.station_id,
                data.recorded_at,
                data.temperature,
                data.humidity,
                data.rainfall_total,
                delta,
                rate,
                data.wind_dir,
                data.wind_avg,
                data.wind_gust,
                data.illuminance,
                data.uv_index,
                data.pressure,
            ),
        )
        await db.commit()

        # 過去の NULL レコードをバックフィル
        await backfill_rainfall_rate(db)
        await db.commit()

        # 挿入したレコードを取得
        cursor = await db.execute(
            "SELECT * FROM weather_records ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        record = dict(row)

        # SSE用: 新レコードの rate は NULL なので
        # 直前のバックフィル済みレコードの rate を暫定セット
        if record["rainfall_rate"] is None:
            cursor = await db.execute(
                "SELECT rainfall_rate FROM weather_records "
                "WHERE rainfall_rate IS NOT NULL "
                "ORDER BY recorded_at DESC LIMIT 1"
            )
            prev = await cursor.fetchone()
            if prev and prev["rainfall_rate"] is not None:
                record["rainfall_rate"] = prev["rainfall_rate"]
    finally:
        await db.close()

    _notify_sse(record)
    return record


_BUCKET_AVG_SQL = """
    SELECT
        MIN(id)                     AS id,
        station_id,
        AVG(recorded_at)            AS recorded_at,
        AVG(temperature)            AS temperature,
        AVG(humidity)               AS humidity,
        MAX(rainfall_total)         AS rainfall_total,
        SUM(rainfall_delta)         AS rainfall_delta,
        AVG(rainfall_rate)          AS rainfall_rate,
        CASE WHEN COUNT(wind_dir) = 0 THEN NULL
             ELSE (DEGREES(ATAN2(
                 AVG(SIN(wind_dir * 0.017453292519943295)),
                 AVG(COS(wind_dir * 0.017453292519943295))
             )) + 360) % 360
        END                         AS wind_dir,
        AVG(wind_avg)               AS wind_avg,
        MAX(wind_gust)              AS wind_gust,
        AVG(illuminance)            AS illuminance,
        AVG(uv_index)               AS uv_index,
        AVG(pressure)               AS pressure
    FROM weather_records
    WHERE recorded_at >= ? AND recorded_at <= ?
    GROUP BY CAST((recorded_at - ?) / ? AS INTEGER), station_id
    ORDER BY recorded_at
"""


@app.get("/api/weather/latest")
async def get_latest(
    since: float | None = Query(default=None, description="開始Unix timestamp"),
    until: float | None = Query(default=None, description="終了Unix timestamp"),
    limit: int = Query(default=120, ge=1, le=100000),
    max_points: int = Query(default=600, ge=1, le=5000, description="最大返却件数（間引き）"),
):
    db = await get_db()
    try:
        if since is not None:
            end = until if until is not None else 9999999999.0

            cursor = await db.execute(
                "SELECT COUNT(*), MIN(recorded_at), MAX(recorded_at) "
                "FROM weather_records "
                "WHERE recorded_at >= ? AND recorded_at <= ?",
                (since, end),
            )
            total, t_min, t_max = await cursor.fetchone()

            if total == 0:
                return []

            if total <= max_points or t_max == t_min:
                # 間引き不要 — 全件返却
                cursor = await db.execute(
                    "SELECT * FROM weather_records "
                    "WHERE recorded_at >= ? AND recorded_at <= ? "
                    "ORDER BY recorded_at",
                    (since, end),
                )
            else:
                # バケット平均で間引き
                bucket_sec = (t_max - t_min) / max_points
                cursor = await db.execute(
                    _BUCKET_AVG_SQL, (since, end, since, bucket_sec)
                )
        else:
            # 従来互換: limit 件取得
            cursor = await db.execute(
                "SELECT * FROM weather_records "
                "ORDER BY recorded_at DESC LIMIT ?",
                (limit,),
            )

        rows = await cursor.fetchall()
    finally:
        await db.close()

    records = [dict(r) for r in rows]
    if since is None:
        records.reverse()
    return records


@app.get("/api/weather/extremes")
async def get_extremes(
    days: int = Query(default=3, ge=1, le=30),
    tz_offset: int = Query(default=540, description="UTC からのオフセット（分）。JST=540"),
):
    """直近 N 日間の日別極値を返す。"""
    import time

    tz_sec = tz_offset * 60
    now_local = time.time() + tz_sec
    today_start_local = int(now_local / 86400) * 86400
    since_utc = today_start_local - (days - 1) * 86400 - tz_sec

    db = await get_db()
    try:
        cursor = await db.execute(
            """
            SELECT
                CAST((recorded_at + ?) / 86400 AS INTEGER) AS day_num,
                MIN(temperature)    AS temp_min,
                MAX(temperature)    AS temp_max,
                MIN(humidity)       AS hum_min,
                MAX(humidity)       AS hum_max,
                MAX(wind_gust)      AS wind_max,
                MAX(illuminance)    AS lux_max,
                MAX(uv_index)       AS uv_max,
                MAX(rainfall_rate)  AS rain_rate_max,
                SUM(rainfall_delta) AS rain_24h,
                MIN(pressure)       AS pressure_min,
                MAX(pressure)       AS pressure_max
            FROM weather_records
            WHERE recorded_at >= ?
            GROUP BY day_num
            ORDER BY day_num DESC
            """,
            (tz_sec, since_utc),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    result = []
    for r in rows:
        d = dict(r)
        # day_num → 日付文字列 (ローカル)
        epoch = d["day_num"] * 86400  # local midnight epoch
        d["date"] = f"{epoch}"  # フロント側で変換
        result.append(d)
    return result


@app.get("/api/weather/stream")
async def stream():
    queue: asyncio.Queue = asyncio.Queue()
    _sse_clients.append(queue)

    async def event_generator():
        try:
            while True:
                record = await queue.get()
                yield {"event": "weather", "data": json.dumps(record)}
        except asyncio.CancelledError:
            pass
        finally:
            _sse_clients.remove(queue)

    return EventSourceResponse(event_generator())
