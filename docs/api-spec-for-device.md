# 気象ステーション API 仕様書（デバイス開発向け）

## 概要

デバイスからサーバーへ観測データを送信するためのAPI仕様です。
デバイスが使用するエンドポイントは **`POST /api/weather`** の1つだけです。

---

## 接続先

| 項目 | 値 |
|------|-----|
| プロトコル | HTTP |
| ホスト | サーバーのIPアドレス（Tailscale VPN経由） |
| ポート | `8765` |
| ベースURL例 | `http://100.x.x.x:8765` |

---

## POST /api/weather

観測データを1件送信する。

### リクエスト

```
POST /api/weather
Content-Type: application/json
```

### リクエストボディ

```json
{
  "station_id":     "ST-001",
  "recorded_at":    1709700000.0,
  "temperature":    22.5,
  "humidity":       65.3,
  "rainfall_total": 102.4,
  "wind_dir":       180.0,
  "wind_avg":       3.2,
  "wind_gust":      5.8,
  "illuminance":    25000.0,
  "uv_index":       4.2
}
```

### フィールド定義

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `station_id` | string | **必須** | ステーション識別子（例: `"ST-001"`） |
| `recorded_at` | float | **必須** | 観測時刻（Unix timestamp、秒単位、小数可） |
| `temperature` | float \| null | 任意 | 気温（°C） |
| `humidity` | float \| null | 任意 | 湿度（%） |
| `rainfall_total` | float \| null | 任意 | 雨量計の累計値（mm）※差分はサーバ側で計算 |
| `wind_dir` | float \| null | 任意 | 風向（度, 0-360） |
| `wind_avg` | float \| null | 任意 | 平均風速（m/s） |
| `wind_gust` | float \| null | 任意 | 瞬間最大風速（m/s） |
| `illuminance` | float \| null | 任意 | 照度（lux） |
| `uv_index` | float \| null | 任意 | UV指数 |

### 任意フィールドの扱い

- 搭載していないセンサーのフィールドは **省略** または **null** を送信してください
- 省略した場合、サーバ側で `null` として格納されます

```json
{
  "station_id": "ST-001",
  "recorded_at": 1709700000.0,
  "temperature": 22.5,
  "humidity": 65.3
}
```

上記のように温度・湿度のみの送信も有効です。

---

### レスポンス

**成功時: `201 Created`**

```json
{
  "id": 1,
  "station_id": "ST-001",
  "recorded_at": 1709700000.0,
  "temperature": 22.5,
  "humidity": 65.3,
  "rainfall_total": 102.4,
  "rainfall_delta": 0.2,
  "rainfall_rate": 24.0,
  "wind_dir": 180.0,
  "wind_avg": 3.2,
  "wind_gust": 5.8,
  "illuminance": 25000.0,
  "uv_index": 4.2
}
```

- `rainfall_delta`: 前回からの雨量差分（mm）— **サーバ側計算**
- `rainfall_rate`: 毎時雨量換算（mm/h）— **サーバ側計算**

**エラー時: `422 Unprocessable Entity`**

必須フィールド (`station_id`, `recorded_at`) の欠落や型不一致の場合。

```json
{
  "detail": [
    {
      "loc": ["body", "station_id"],
      "msg": "Field required",
      "type": "missing"
    }
  ]
}
```

**異常値検出時（DB登録なし）: `200 OK`**

センサー値が物理的に妥当な範囲外（例: temperature=69.5°C, humidity=150%）、
または降水量が異常な見かけ強度（>100 mm/h、装置リセット由来など）の場合、
**サーバ側で破棄**される。デバイス側はリトライ不要。

```json
{
  "status": "rejected",
  "reason": "validation failed",
  "errors": [...]
}
```

センサー値の物理範囲（範囲外で破棄）:

| フィールド | 範囲 |
| ---- | ---- |
| `temperature` | `[-60, 70]` °C |
| `humidity` | `[0, 100]` % |
| `pressure` | `[800, 1100]` hPa |
| `wind_dir` | `[0, 360]` ° |
| `wind_avg` / `wind_gust` | `[0, 200]` m/s |
| `illuminance` | `[0, 200000]` lux |
| `uv_index` | `[0, 20]` |

---

## 送信頻度

- **推奨: 約30秒間隔**（2回/分）
- 雨量計算の精度は送信間隔に依存するため、極端に長い間隔（数分以上）は避けてください

---

## rainfall_total について

| 状況 | デバイス側の対応 |
|------|-----------------|
| 通常 | 雨量計の累計値をそのまま送信 |
| 電源再投入で累計リセット | そのまま送信してOK（サーバ側で差分<0を検出し `0.0` として扱う） |
| 雨量計なし | フィールドを省略 or `null` |

---

## 実装例

### curl

```bash
curl -X POST http://100.x.x.x:8765/api/weather \
  -H 'Content-Type: application/json' \
  -d '{
    "station_id": "ST-001",
    "recorded_at": 1709700000.0,
    "temperature": 22.5,
    "humidity": 65.3
  }'
```

### MicroPython / CircuitPython（参考）

```python
import urequests
import time
import json

payload = {
    "station_id": "ST-001",
    "recorded_at": time.time(),
    "temperature": sensor.temperature,
    "humidity": sensor.humidity,
}

resp = urequests.post(
    "http://100.x.x.x:8765/api/weather",
    headers={"Content-Type": "application/json"},
    data=json.dumps(payload),
)
print(resp.status_code)  # 201 なら成功
resp.close()
```

### Arduino (ESP32 + HTTPClient)（参考）

```cpp
#include <HTTPClient.h>
#include <ArduinoJson.h>

HTTPClient http;
http.begin("http://100.x.x.x:8765/api/weather");
http.addHeader("Content-Type", "application/json");

JsonDocument doc;
doc["station_id"]  = "ST-001";
doc["recorded_at"] = (double)time(NULL);
doc["temperature"] = 22.5;
doc["humidity"]    = 65.3;

String body;
serializeJson(doc, body);

int code = http.POST(body);  // 201 なら成功
http.end();
```

---

## 補足

- 認証: 現時点では未実装（Tailscale VPN内での利用を前提）
- `recorded_at` はデバイス側の時刻を使用してください（NTP同期推奨）
