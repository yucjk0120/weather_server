from pydantic import BaseModel, Field


class WeatherInput(BaseModel):
    """POST /api/weather リクエストボディ。

    各センサー値は物理的に妥当な範囲を Pydantic で検証する。
    範囲外の値が含まれる場合は 422 エラーで拒否（DBに登録しない）。
    """

    station_id: str
    recorded_at: float
    temperature: float | None = Field(default=None, ge=-60.0, le=70.0)
    humidity: float | None = Field(default=None, ge=0.0, le=100.0)
    rainfall_total: float | None = Field(default=None, ge=0.0)
    wind_dir: float | None = Field(default=None, ge=0.0, le=360.0)
    wind_avg: float | None = Field(default=None, ge=0.0, le=200.0)
    wind_gust: float | None = Field(default=None, ge=0.0, le=200.0)
    illuminance: float | None = Field(default=None, ge=0.0, le=200000.0)
    uv_index: float | None = Field(default=None, ge=0.0, le=20.0)
    pressure: float | None = Field(default=None, ge=800.0, le=1100.0)


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
    pressure: float | None = None
