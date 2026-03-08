import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app.database import get_db, init_db
from app.models import WeatherInput, WeatherRecord
from app.rainfall import calc_rainfall

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

templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)


# --- HTML ---


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


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
                 wind_dir, wind_avg, wind_gust, illuminance, uv_index)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        await db.commit()

        # 挿入したレコードを取得
        cursor = await db.execute(
            "SELECT * FROM weather_records ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        record = dict(row)
    finally:
        await db.close()

    _notify_sse(record)
    return record


@app.get("/api/weather/latest")
async def get_latest(
    since: float | None = Query(default=None, description="開始Unix timestamp"),
    limit: int = Query(default=120, ge=1, le=100000),
    max_points: int = Query(default=600, ge=1, le=5000, description="最大返却件数（間引き）"),
):
    db = await get_db()
    try:
        if since is not None:
            # 時間範囲指定: since 以降の全データを取得し間引く
            cursor = await db.execute(
                "SELECT COUNT(*) FROM weather_records WHERE recorded_at >= ?",
                (since,),
            )
            (total,) = await cursor.fetchone()

            step = max(1, total // max_points)
            cursor = await db.execute(
                """
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (ORDER BY recorded_at) AS rn
                    FROM weather_records
                    WHERE recorded_at >= ?
                ) WHERE rn % ? = 1 OR rn = (
                    SELECT COUNT(*) FROM weather_records WHERE recorded_at >= ?
                )
                ORDER BY recorded_at
                """,
                (since, step, since),
            )
        else:
            # 従来互換: limit 件取得
            cursor = await db.execute(
                """
                SELECT * FROM weather_records
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (limit,),
            )

        rows = await cursor.fetchall()
    finally:
        await db.close()

    records = [dict(r) for r in rows]
    # rn 列を除去 & 古い順ソート
    for rec in records:
        rec.pop("rn", None)
    if since is None:
        records.reverse()
    return records


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
