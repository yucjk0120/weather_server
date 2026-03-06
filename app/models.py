from pydantic import BaseModel


class WeatherInput(BaseModel):
    """POST /api/weather リクエストボディ。rainfall_delta/rate はサーバ側計算。"""

    station_id: str
    recorded_at: float
    temperature: float | None = None
    humidity: float | None = None
    rainfall_total: float | None = None
    wind_dir: float | None = None
    wind_avg: float | None = None
    wind_gust: float | None = None
    illuminance: float | None = None
    uv_index: float | None = None


class WeatherRecord(BaseModel):
    """DB格納済みレコード（全フィールド）。"""

    id: int
    station_id: str
    recorded_at: float
    temperature: float | None = None
    humidity: float | None = None
    rainfall_total: float | None = None
    rainfall_delta: float | None = None
    rainfall_rate: float | None = None
    wind_dir: float | None = None
    wind_avg: float | None = None
    wind_gust: float | None = None
    illuminance: float | None = None
    uv_index: float | None = None
