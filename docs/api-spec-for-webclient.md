# 気象ステーション API 仕様書（Web クライアント向け）

サーバーが Web クライアント（ブラウザ・モニタリングツール等）に提供するエンドポイントの仕様。
データ送信側のデバイス向け仕様は [`api-spec-for-device.md`](api-spec-for-device.md) 参照。

---

## 目次

- [接続情報](#接続情報)
- [共通](#共通)
- [GET `/`](#get-)
- [GET `/api/weather/latest`](#get-apiweatherlatest)
- [GET `/api/weather/extremes`](#get-apiweatherextremes)
- [GET `/api/weather/stream`（SSE）](#get-apiweatherstreamsse)
- [GET `/static/...`、`/service-worker.js`（PWA 関連）](#pwa-関連)
- [エラーレスポンス](#エラーレスポンス)
- [データフィールド辞書](#データフィールド辞書)

---

## 接続情報

| 項目 | 値 |
| ---- | ---- |
| プロトコル | HTTP（Tailscale VPN 内） |
| ポート | `8765` |
| ベースURL例 | `http://100.x.x.x:8765` |
| 認証 | なし（VPN 越しの利用前提） |

---

## 共通

### Content-Type

すべての JSON レスポンスは `application/json; charset=utf-8`。

### CORS

設定なし（同一オリジン前提）。別オリジンから呼ぶ場合は別途プロキシが必要。

### 時刻フォーマット

すべての時刻は **Unix timestamp（秒、UTC、float）**。例: `1709700000.0`。

クライアント側で表示時に `new Date(recorded_at * 1000)` でローカル時刻に変換する。

### `null` 値

センサー未搭載・観測欠損のフィールドは `null`。フロント側で `--` 等にフォールバック表示することを想定。

---

## GET `/`

ダッシュボード HTML を返す。

| 項目 | 値 |
| ---- | ---- |
| Content-Type | `text/html; charset=utf-8` |
| キャッシュ | Service Worker が **Cache First** で保持 |

---

## GET `/api/weather/latest`

期間指定または件数指定で観測レコードを返す。データ点数が `max_points` を超える場合は **時間バケット平均で間引き** される。

### クエリパラメータ

| 名前 | 型 | デフォルト | 範囲 | 説明 |
| ---- | ---- | ---- | ---- | ---- |
| `since` | float \| null | `null` | — | 取得開始 Unix timestamp（秒）。指定すると期間モード |
| `until` | float \| null | `null` | — | 取得終了 Unix timestamp（秒）。`since` 指定時のみ有効 |
| `limit` | int | `120` | `[1, 100000]` | 件数モード時の最大件数（`since` 未指定時のみ有効） |
| `max_points` | int | `600` | `[1, 5000]` | 期間モード時の最大返却件数。超過分は間引き対象 |

### 動作モード

#### 件数モード（`since` 未指定）

最新から `limit` 件を返す。`recorded_at` 昇順。フロントの「現在値」初期化用。

#### 期間モード（`since` 指定）

`recorded_at` が `[since, until]` の範囲のレコードを返す（`recorded_at` 昇順）。

- 期間内の総件数 ≤ `max_points` なら**全件返却**
- 超える場合は **時間バケット平均** で間引き：
  - バケット幅 = `(t_max - t_min) / max_points` 秒
  - 各バケット内のレコードを集計：
    - `temperature`, `humidity`, `wind_avg`, `illuminance`, `uv_index`, `pressure`: 算術平均
    - `rainfall_total`: バケット内最大値
    - `rainfall_delta`: バケット内の合計
    - `rainfall_rate`: 算術平均
    - `wind_dir`: **円形ベクトル平均**（sin/cos の平均から再計算）
    - `wind_gust`: バケット内最大値
    - `id`: バケット内 `MIN(id)`
    - `recorded_at`: バケット内 `AVG(recorded_at)`

### レスポンス

`200 OK` で `WeatherRecord` の配列。空期間は `[]`。

```json
[
  {
    "id": 12345,
    "station_id": "ST-001",
    "recorded_at": 1709700000.0,
    "temperature": 22.5,
    "humidity": 65.3,
    "rainfall_total": 102.4,
    "rainfall_delta": 0.0,
    "rainfall_rate": 1.2,
    "wind_dir": 180.0,
    "wind_avg": 3.2,
    "wind_gust": 5.8,
    "illuminance": 25000.0,
    "uv_index": 4.2,
    "pressure": 1013.25
  }
]
```

### 例

```bash
# 直近 24時間 を最大 600 点で取得
since=$(($(date +%s) - 86400))
curl "http://100.x.x.x:8765/api/weather/latest?since=${since}&max_points=600"

# 直近 60 件を取得
curl "http://100.x.x.x:8765/api/weather/latest?limit=60"
```

```js
const since = Math.floor(Date.now() / 1000) - 24 * 3600;
const url = `/api/weather/latest?since=${since}&max_points=600`;
const records = await fetch(url).then(r => r.json());
```

---

## GET `/api/weather/extremes`

直近 N 日間の **日別極値** を返す。極値テーブル UI 用。

### クエリパラメータ

| 名前 | 型 | デフォルト | 範囲 | 説明 |
| ---- | ---- | ---- | ---- | ---- |
| `days` | int | `3` | `[1, 30]` | 直近 N 日間 |
| `tz_offset` | int | `540` | — | UTC からのオフセット（分単位）。JST = 540 |

### レスポンス

`200 OK` で日付（新→古）順の配列。

```json
[
  {
    "day_num": 19790,
    "date": "1709640000",
    "temp_min": 12.1, "temp_max": 24.8,
    "hum_min": 45.2, "hum_max": 92.7,
    "wind_max": 8.4,
    "lux_max": 32500.0,
    "uv_max": 6.1,
    "rain_rate_max": 12.3,
    "rain_24h": 5.4,
    "pressure_min": 1008.2, "pressure_max": 1015.7
  }
]
```

| フィールド | 型 | 説明 |
| ---- | ---- | ---- |
| `day_num` | int | ローカル日付の通日番号（`floor((recorded_at + tz_sec) / 86400)`） |
| `date` | string | `day_num × 86400` の Unix timestamp（秒、文字列）。フロント側で `new Date(parseInt(date) * 1000 - tz_offset*60000)` でローカル日付に変換 |
| `temp_min` / `temp_max` | float\|null | 当日の最低・最高気温 |
| `hum_min` / `hum_max` | float\|null | 当日の最低・最高湿度 |
| `wind_max` | float\|null | 当日の瞬間最大風速 |
| `lux_max` | float\|null | 当日の最大照度 |
| `uv_max` | float\|null | 当日の最大 UV 指数 |
| `rain_rate_max` | float\|null | 当日の最大降水レート |
| `rain_24h` | float\|null | 当日の累計降水量（`SUM(rainfall_delta)`） |
| `pressure_min` / `pressure_max` | float\|null | 当日の最低・最高気圧 |

---

## GET `/api/weather/stream`（SSE）

リアルタイムで新規観測データを配信する **Server-Sent Events** エンドポイント。
新規 POST `/api/weather` がサーバーに到達するたびに、購読中の全クライアントに即座にイベントが送信される。

### プロトコル

W3C SSE 仕様に準拠（[EventSource API](https://developer.mozilla.org/en-US/docs/Web/API/EventSource)）。
HTTP/1.1 の長期接続（`Connection: keep-alive` + `Content-Type: text/event-stream`）を使用。

### リクエスト

| 項目 | 値 |
| ---- | ---- |
| メソッド | `GET` |
| クエリパラメータ | なし |
| Accept ヘッダ | `text/event-stream`（EventSource が自動付与） |

### レスポンスヘッダ

| ヘッダ | 値 |
| ---- | ---- |
| `Content-Type` | `text/event-stream; charset=utf-8` |
| `Cache-Control` | `no-cache` |
| `Connection` | `keep-alive` |
| `Transfer-Encoding` | `chunked` |

### イベント形式

各イベントは以下の3行（最後に空行）で構成：

```
event: weather
data: <JSON>

```

- イベント名は **常に `weather`** で固定
- `id` フィールドはサーバーから送出**しない**（ID トラッキングなし）
- `retry` フィールドも送出**しない**（クライアント側のデフォルト 3000ms 等に依存）

### `data` ペイロード

`POST /api/weather` 完了直後の挿入レコード（`WeatherRecord` 1 件）。
**`/api/weather/latest` と同じスキーマ**で、フィールド構成は完全一致。

```
event: weather
data: {"id":12346,"station_id":"ST-001","recorded_at":1709700030.0,"temperature":22.6,"humidity":65.0,"rainfall_total":102.4,"rainfall_delta":0.0,"rainfall_rate":1.2,"wind_dir":175.0,"wind_avg":3.1,"wind_gust":5.5,"illuminance":24800.0,"uv_index":4.1,"pressure":1013.30}

```

### `rainfall_rate` の扱い（重要）

挿入直後のレコードは `rainfall_rate` がまだ計算途中の場合があります（10分窓集計のため）。
SSE では送信前に**直近の確定済みレコードの `rainfall_rate` を暫定的にコピー**して配信します。

正確な値が必要な場合は、後続の SSE で再計算された値が来るのを待つか、`/api/weather/latest` で取得し直してください。

### 配信頻度

- デバイスからの POST 頻度に依存（典型的には **30秒間隔**）
- サーバー側のバッファリング・スロットリングなし — POST 即時配信
- 異常値で拒否（`200 + status:rejected`）された POST は **配信されない**

### 接続管理（Heartbeat / Keep-alive）

サーバーは明示的な heartbeat（コメント行）を**送出しない**。
プロキシ等での接続切断対策が必要な場合は、リバースプロキシで `proxy_buffering off;` を設定する。
EventSource はサーバー側切断を検知して自動再接続するため、通常運用では問題なし。

### 再接続

`EventSource` の標準動作：

- ネットワーク・サーバー側切断時、デフォルト **3 秒後**に自動再接続
- 再接続時は `/api/weather/stream` を再度 GET
- **再接続中に発生した観測データは取りこぼす**（バッファリング・履歴配信なし）
- 取りこぼし対策が必要な場合: 再接続時に `/api/weather/latest?since=<最後のrecorded_at>` で取得

### サンプル実装

#### 標準 `EventSource`（自動再接続あり）

```js
const es = new EventSource('/api/weather/stream');

es.addEventListener('weather', (e) => {
  const record = JSON.parse(e.data);
  console.log(record.recorded_at, record.temperature);
});

es.onerror = (err) => {
  // ブラウザが自動再接続するため、通常は表示更新のみ
  console.warn('SSE disconnected, will retry...');
};

es.onopen = () => {
  console.log('SSE connected');
};

// 明示的に切断
// es.close();
```

#### 取りこぼし対策付き

```js
let lastSeen = 0;
let es = null;

function connect() {
  es = new EventSource('/api/weather/stream');
  es.addEventListener('weather', (e) => {
    const r = JSON.parse(e.data);
    lastSeen = r.recorded_at;
    handleRecord(r);
  });
  es.onerror = async () => {
    es.close();
    // 切断中の取りこぼしを取得
    if (lastSeen) {
      const recs = await fetch(`/api/weather/latest?since=${lastSeen}`)
        .then(r => r.json());
      recs.forEach(handleRecord);
    }
    setTimeout(connect, 3000);
  };
}
connect();
```

#### Python（`requests` で購読）

```python
import json
import requests

with requests.get('http://100.x.x.x:8765/api/weather/stream',
                  stream=True, headers={'Accept': 'text/event-stream'}) as r:
    event = None
    for line in r.iter_lines(decode_unicode=True):
        if line.startswith('event:'):
            event = line[6:].strip()
        elif line.startswith('data:'):
            data = json.loads(line[5:].strip())
            print(event, data['temperature'], data['humidity'])
        elif line == '':
            event = None
```

#### `curl`（動作確認用）

```bash
curl -N http://100.x.x.x:8765/api/weather/stream
```

`-N` オプションで出力バッファを無効化。

### 並行接続数

サーバー内部で接続ごとに `asyncio.Queue` を保持。同時接続数の明示的な上限なし
（実用上はリバースプロキシ・OS のファイルディスクリプタ上限に従う）。

---

## PWA 関連

| パス | 内容 | キャッシュ戦略 |
| ---- | ---- | ---- |
| `/static/manifest.json` | Web App Manifest（PWA メタデータ） | Cache First |
| `/static/icons/icon-192.png` | アイコン 192×192 | Cache First |
| `/static/icons/icon-512.png` | アイコン 512×512 | Cache First |
| `/static/icons/apple-touch-icon.png` | iOS 用 180×180 | Cache First |
| `/static/icons/favicon-32.png` | Favicon 32×32 | Cache First |
| `/static/icons/favicon.ico` | Favicon ICO | Cache First |
| `/service-worker.js` | Service Worker 本体 | ブラウザ管理 |

`/service-worker.js` のレスポンスヘッダ:

| ヘッダ | 値 |
| ---- | ---- |
| `Content-Type` | `application/javascript` |
| `Service-Worker-Allowed` | `/` |

`Service-Worker-Allowed: /` により、`/static/` 配下に置かれた SW スクリプトでもルートスコープを制御可能。

---

## エラーレスポンス

| ステータス | 発生条件 | 形式 |
| ---- | ---- | ---- |
| `404 Not Found` | 存在しないパス | FastAPI 標準 |
| `422 Unprocessable Entity` | クエリパラメータの型不整合（`limit=abc` 等） | `{"detail":[...]}` |

### 例: 422

```json
{
  "detail": [
    {
      "type": "int_parsing",
      "loc": ["query", "limit"],
      "msg": "Input should be a valid integer",
      "input": "abc"
    }
  ]
}
```

---

## データフィールド辞書

### `WeatherRecord`（`/api/weather/latest` および SSE 配信の各レコード）

| フィールド | 型 | 単位 | 説明 |
| ---- | ---- | ---- | ---- |
| `id` | int | — | レコードの一意 ID（DB の AUTOINCREMENT） |
| `station_id` | string | — | 観測ステーション識別子（例 `"ST-001"`） |
| `recorded_at` | float | 秒 | 観測時刻（Unix timestamp、UTC、小数可） |
| `temperature` | float \| null | °C | 気温 |
| `humidity` | float \| null | % | 相対湿度 |
| `rainfall_total` | float \| null | mm | 雨量計の累計値（生データ） |
| `rainfall_delta` | float \| null | mm | 前レコードからの差分（サーバー算出。負・異常は `0`） |
| `rainfall_rate` | float \| null | mm/h | 瞬間降水強度（10分窓ベース、サーバー算出） |
| `wind_dir` | float \| null | ° | 風向（0=北、90=東、180=南、270=西） |
| `wind_avg` | float \| null | m/s | 平均風速 |
| `wind_gust` | float \| null | m/s | 瞬間最大風速 |
| `illuminance` | float \| null | lux | 照度 |
| `uv_index` | float \| null | — | UV 指数 |
| `pressure` | float \| null | hPa | 海面気圧 |

### 値が `null` になる条件

- センサー未搭載
- センサー欠損（通信エラー等）
- マイグレーションで物理範囲外として除外（[`migrate.py`](../scripts/migrate.py) の `2026-05-02_002_null_out_of_range_sensor_values`）
- `rainfall_delta` / `rainfall_rate`: 履歴の最初のレコード、または装置リセット直後

### 物理範囲（範囲外は POST 時に拒否、既存データはマイグレーションで NULL 化）

| フィールド | 範囲 |
| ---- | ---- |
| `temperature` | `[-60, 70]` °C |
| `humidity` | `[0, 100]` % |
| `pressure` | `[800, 1100]` hPa |
| `wind_dir` | `[0, 360]` ° |
| `wind_avg` / `wind_gust` | `[0, 200]` m/s |
| `illuminance` | `[0, 200000]` lux |
| `uv_index` | `[0, 20]` |

詳細は [README の「異常値の取り扱い」](../README.md#異常値の取り扱いpost-時) を参照。
