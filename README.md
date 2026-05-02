# 気象ステーション (Weather Station)

リアルタイム気象観測データの収集・可視化を行う FastAPI + SQLite + Chart.js のダッシュボードサーバ。
PWA 対応・SSE によるリアルタイム配信・MD3 Expressive UI・ダークモード（システム連動）対応。

---

## 目次

- [スタック](#スタック)
- [必要環境](#必要環境)
- [初回セットアップ](#初回セットアップ)
- [起動・停止・更新時の再起動](#起動停止更新時の再起動)
- [ディレクトリ構成](#ディレクトリ構成)
- [必要ファイル一覧](#必要ファイル一覧)
- [内部構成](#内部構成)
- [API エンドポイント](#api-エンドポイント)
- [DB スキーマ](#db-スキーマ)
- [PWA / Service Worker](#pwa--service-worker)
- [テストデータ投入](#テストデータ投入)
- [トラブルシューティング](#トラブルシューティング)

---

## スタック

| レイヤ | 技術 |
|------|------|
| バックエンド | Python 3.12, FastAPI 0.115, Uvicorn |
| DB | SQLite (aiosqlite) |
| リアルタイム配信 | SSE (sse-starlette) |
| テンプレート | Jinja2 |
| フロントエンド | Vanilla JS, Chart.js 4, MD3 Expressive (CSS のみ) |
| 配信 | Docker / Docker Compose |
| PWA | Web App Manifest + Service Worker |

---

## 必要環境

- Docker / Docker Compose（推奨）
- もしくは Python 3.12 + pip（直接実行する場合）

ホスト側に必要なポート: **8765**（コンテナ内部 8000 をマップ）

---

## 初回セットアップ

```bash
# リポジトリ取得
git clone <REPO_URL> weather_server
cd weather_server

# データ用ディレクトリ作成（永続化ボリューム）
mkdir -p data

# 起動（ビルド + 立ち上げ、バックグラウンド）
docker compose up -d --build
```

ブラウザで http://localhost:8765/ を開く。

初回起動時に SQLite DB (`data/weather.db`) が自動生成される（テーブル + インデックス + マイグレーション）。

---

## 起動・停止・更新時の再起動

### 通常起動 / 停止

```bash
docker compose up -d        # 起動（バックグラウンド）
docker compose stop         # 停止（コンテナは残す）
docker compose down         # 停止 + コンテナ削除（データは保持）
docker compose logs -f      # ログを追う
```

### 更新時の再起動（用途別）

| 変更したファイル | 必要な操作 | コマンド |
|---------------|----------|---------|
| `app/templates/*.html` | **再起動不要** | （即時反映：ボリュームマウント） |
| `app/static/*` （manifest, SW, アイコン等） | **再起動不要** | （即時反映：ボリュームマウント） |
| `app/*.py` | コンテナ再起動 | `docker compose restart` |
| `requirements.txt` | リビルド | `docker compose up -d --build` |
| `Dockerfile` / `docker-compose.yml` | リビルド | `docker compose up -d --build` |

**ボリュームマウント対象**（変更が即時反映）:
- `./data` → `/app/data` (DB)
- `./app/templates` → `/app/app/templates` (HTML)
- `./app/static` → `/app/app/static` (PWA 資産)

**コンテナビルドに含まれる**（リビルドが必要）:
- `app/main.py`, `app/database.py`, `app/models.py`, `app/rainfall.py`
- `requirements.txt` の依存パッケージ

### Service Worker キャッシュの破棄

クライアント側で古いキャッシュが残った場合、`app/static/service-worker.js` の `CACHE_NAME` を `weather-station-v1` → `v2` に変更してリビルド。各クライアントが次回読み込み時に旧キャッシュを破棄する。

---

## ディレクトリ構成

```
weather_server/
├── Dockerfile                  # Python 3.12-slim ベースのイメージ定義
├── docker-compose.yml          # ポート/ボリューム/再起動ポリシー
├── requirements.txt            # Python 依存
├── .dockerignore               # ビルドコンテキスト除外
├── .gitignore                  # Git 除外（DB 等）
├── README.md                   # 本ファイル
│
├── app/                        # アプリ本体
│   ├── main.py                 # FastAPI エンドポイント定義
│   ├── database.py             # SQLite 接続・スキーマ・マイグレーション
│   ├── models.py               # Pydantic モデル
│   ├── rainfall.py             # 雨量差分・降水レート算出
│   ├── templates/
│   │   └── index.html          # ダッシュボード (HTML+CSS+JS 単一ファイル)
│   └── static/                 # PWA 資産（即時反映）
│       ├── manifest.json       # Web App Manifest
│       ├── service-worker.js   # PWA キャッシュ戦略
│       └── icons/              # アイコン各種
│           ├── icon-192.png
│           ├── icon-512.png
│           ├── icon-maskable-192.png
│           ├── icon-maskable-512.png
│           ├── apple-touch-icon.png
│           ├── favicon-32.png
│           ├── favicon-48.png
│           └── favicon.ico
│
├── data/                       # SQLite DB（永続化ボリューム）
│   └── weather.db              # 自動生成
│
├── scripts/
│   ├── migrate.py              # DB マイグレーションランナー
│   └── insert_test_data.py     # テストデータ投入スクリプト
│
└── docs/
    └── api-spec-for-device.md  # デバイス開発向け API 仕様書
```

---

## 必要ファイル一覧

### コンテナビルドに必要

| ファイル | 役割 |
|---------|------|
| `Dockerfile` | Python 3.12-slim + 依存インストール + アプリコピー |
| `docker-compose.yml` | サービス定義・ポートマップ・ボリューム |
| `requirements.txt` | Python 依存パッケージ |
| `app/main.py` | FastAPI ルート定義 |
| `app/database.py` | SQLite 初期化 |
| `app/models.py` | Pydantic スキーマ |
| `app/rainfall.py` | 雨量計算ロジック |

### ランタイム（ボリュームマウントで提供）

| ファイル | 役割 |
|---------|------|
| `app/templates/index.html` | ダッシュボード本体 |
| `app/static/manifest.json` | PWA マニフェスト |
| `app/static/service-worker.js` | SW（オフラインキャッシュ） |
| `app/static/icons/*.png` | PWA / favicon アイコン |
| `data/weather.db` | SQLite DB（自動生成） |

### 自動生成されるもの

- `data/weather.db` — 初回起動時にスキーマ作成
- `app/__pycache__/` — Python バイトコード

---

## 内部構成

### バックエンド（FastAPI）

```
[Device] --POST /api/weather--> [FastAPI]
                                   ├── DB INSERT (aiosqlite)
                                   ├── rainfall.calc → delta 算出
                                   ├── backfill_rainfall_rate → 過去レコードの rate 補完
                                   └── _notify_sse → 接続中の全クライアントへ broadcast

[Browser] --GET /api/weather/stream--> [FastAPI: EventSourceResponse]
              <-- weather event (JSON) <-- 各クライアントごとの asyncio.Queue
```

主要ロジック:
- **雨量差分** (`rainfall.calc_rainfall`): 直前レコードとの累計差分を算出。電源再投入で累計リセットされた場合（差分<0）は 0 として扱う。
- **降水レート (新)** — 0.1mm 仮想イベントによる時間方向の按分（[詳細](#降水レート計算-rainfall_rate)）
- **間引き** (`/api/weather/latest`): `max_points` を超える場合、時間バケット平均で間引いて返却。風向はベクトル平均（円形統計）。

### フロントエンド（単一HTMLファイル）

`app/templates/index.html` は単一ファイルで CSS / JS / SVG を全て内包：

| セクション | 内容 |
|----------|------|
| MD3 トークン | ライト/ダークの色変数（`@media (prefers-color-scheme: dark)` で切替） |
| ダッシュボード | 気温・体感温度・湿度・雨量・風向風速・気圧・照度・UV を SVG ゲージで表示 |
| 範囲チップ | 6時間〜1ヶ月 / 0時基準スナップ |
| ナビゲーション | 過去方向ページング |
| Chart.js | 8グラフ（気温＋露点・湿度・雨量・風速・風向・照度・UV・気圧） |
| カスタムプラグイン | `zebraPlugin`（時間帯ゼブラ）、`nightPlugin`（夜間帯シェード・USNO 簡易計算） |
| データ表オーバーレイ | 各グラフタイトル右の表アイコンで原データを表表示 |
| 極値テーブル | 直近3日の最高/最低/最大値を `/api/weather/extremes` から取得 |
| SSE 受信 | `EventSource('/api/weather/stream')` で自動接続・自動再接続 |

### Docker / 起動構成

```
docker-compose.yml
└── service: weather-server
    ├── build: . (Dockerfile)
    ├── ports: 8765 (host) → 8000 (container)
    ├── volumes:
    │   ├── ./data:/app/data
    │   ├── ./app/templates:/app/app/templates
    │   └── ./app/static:/app/app/static
    └── restart: unless-stopped
```

コンテナ起動時に `uvicorn app.main:app --host 0.0.0.0 --port 8000` が実行される。

---

## API エンドポイント

### 観測データ受信

| メソッド | パス | 用途 |
|--------|------|------|
| `POST` | `/api/weather` | 観測データ1件投入（デバイスから） |

詳細: [`docs/api-spec-for-device.md`](docs/api-spec-for-device.md)

### 観測データ取得

| メソッド | パス | パラメータ | 用途 |
|--------|------|----------|------|
| `GET` | `/` | — | ダッシュボード HTML |
| `GET` | `/api/weather/latest` | `since`, `until`, `limit`, `max_points` | 期間指定取得（間引き対応） |
| `GET` | `/api/weather/extremes` | `days`, `tz_offset` | 日別極値（直近 N 日） |
| `GET` | `/api/weather/stream` | — | SSE: 新規データを `event: weather` で配信 |

### 静的・PWA

| パス | 内容 |
|------|------|
| `/static/manifest.json` | Web App Manifest |
| `/service-worker.js` | SW（ルートスコープで `Service-Worker-Allowed: /` を返す専用エンドポイント経由） |
| `/static/icons/*` | アイコン各種 |

### SSE 形式

```
event: weather
data: {"id":123,"station_id":"ST-001","recorded_at":1709700000.0,"temperature":22.5,...}
```

購読例:

```js
const es = new EventSource('/api/weather/stream');
es.addEventListener('weather', (e) => {
  const r = JSON.parse(e.data);
  console.log(r.temperature, r.humidity);
});
```

---

## DB スキーマ

```sql
CREATE TABLE weather_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id      TEXT    NOT NULL,
    recorded_at     REAL    NOT NULL,   -- Unix timestamp (秒)
    temperature     REAL,                -- °C
    humidity        REAL,                -- %
    rainfall_total  REAL,                -- 累計値 (mm)
    rainfall_delta  REAL,                -- 前回との差分 (mm) ※サーバ算出
    rainfall_rate   REAL,                -- 降水レート (mm/h) ※サーバ算出
    wind_dir        REAL,                -- 度 (0-360)
    wind_avg        REAL,                -- 平均風速 (m/s)
    wind_gust       REAL,                -- 瞬間最大風速 (m/s)
    illuminance     REAL,                -- 照度 (lux)
    uv_index        REAL,                -- UV指数
    pressure        REAL                 -- 気圧 (hPa)
);
CREATE INDEX idx_recorded_at ON weather_records(recorded_at);
CREATE INDEX idx_station_id  ON weather_records(station_id);
```

`init_db()` は冪等（`IF NOT EXISTS`）。マイグレーションは `_MIGRATIONS` リストに追加（`ALTER TABLE` 等、エラーは握りつぶす）。

---

## 降水レート計算 rainfall_rate

転倒ます雨量計（1 tip = 0.3 mm が典型）の生データから、自然な「瞬間雨量」(mm/h) を算出するロジック。`app/rainfall.py` で実装。

### 異常値の取り扱い（POST 時）

POST 受信時、以下の条件に該当するレコードは**DB に登録せず**、**`200 OK` + `{"status": "rejected", "reason": ...}`** で応答する（デバイス側のエラー扱い・リトライを誘発しないため）：

- **センサー物理範囲外**（Pydantic で検証）：
  - 気温 `[-60, 70]` °C / 湿度 `[0, 100]` % / 気圧 `[800, 1100]` hPa
  - 風向 `[0, 360]` ° / 風速 `[0, 200]` m/s / 照度 `[0, 200000]` lux / UV `[0, 20]`
- **降水量の見かけ強度**: `(new_total - prev_total) / gap × 3600 > 100 mm/h`
  - 装置リセット時のカウンター復元（瞬時に +200mm 等）を検出して除外

正常受け付け時のレスポンスは従来通り `201 Created` + 挿入したレコード本体。

### 降水レート計算

各物理 tip (`rainfall_delta > 0`) を **0.1 mm 単位の仮想イベント** に分割し、過去の時間方向に按分する。

| 状況 | 処理 |
| ---- | ---- |
| 30 分以内に前 tip がある場合 | tip 量を `round(delta / 0.1)` 個の仮想イベントに分け、`(T_prev, T]` 区間の各サブ区間中央に均等配置 |
| 30 分以内に前 tip がない（孤立 tip） | `(T - 30分, T]` に均等配置（10 分間隔で 10分窓のレート表示が連続）|

通信途絶（gap中）で複数 tip 分が一度に届いたケースでも、`rainfall_delta` は実値として保持され、グラフは Chart.js が点間を補間してギャップ時間に按分表示する。

瞬間雨量は **過去 10 分窓（気象庁「10分間降水量」標準）** で集計：

```text
rate(t) = (過去10分内の仮想0.1mmイベント数) × 0.1 × 6   [mm/h]
```

### 定数（`app/rainfall.py`）

| 定数 | 値 | 説明 |
| ---- | ---- | ---- |
| `TIP_RESOLUTION` | 0.1 mm | 仮想イベントの粒度 |
| `LOOKBACK_SEC` | 1800 秒 (30分) | 前 tip 探索の上限 |
| `ISOLATED_SPREAD_SEC` | 1800 秒 (30分) | 孤立 tip の按分スパン |
| `RATE_WINDOW_SEC` | 600 秒 (10分) | 瞬間雨量の積算窓 |
| `MAX_APPARENT_RATE` | 100.0 mm/h | 異常 delta の見かけ強度閾値 |

---

## マイグレーション (DB 変換)

ロジック変更（雨量計算式・新カラム追加など）に伴って既存 DB を変換する場合、
[`scripts/migrate.py`](scripts/migrate.py) を使う。

```bash
# 適用済み・未適用の確認
python3 scripts/migrate.py --list

# 適用予定の表示（書き込みなし）
python3 scripts/migrate.py --dry-run

# 実行（自動バックアップ付き）
python3 scripts/migrate.py
```

仕組み:

- 各マイグレーションは ID で識別され、`_schema_migrations` テーブルに記録
- 適用済みのものは2回目以降スキップ（冪等）
- 実行前に `data/weather.bak.migrate.<timestamp>.db` 形式で自動バックアップ
- 標準ライブラリの `sqlite3` のみ使用するためホスト側で直接実行可能（コンテナに入る必要なし）

新しいマイグレーションを追加する場合、[`scripts/migrate.py`](scripts/migrate.py) に
`@migration("YYYY-MM-DD_NNN_short_name", "説明")` デコレータ付きの関数を追記するだけ。

本番デプロイ手順:

```bash
git pull                         # 新コード取得
python3 scripts/migrate.py       # DB 変換（自動バックアップ）
docker compose up -d --build     # アプリ再構築・再起動
```

---

## PWA / Service Worker

### 動作

- ホーム画面追加で `standalone` モード起動（URLバー非表示）
- App Shell（HTML/CSS/JS/Chart.js CDN）は **Cache First**
- API は **Network First**（オフライン時のみキャッシュフォールバック）
- SSE ストリームは **キャッシュしない**

### キャッシュ更新

`app/static/service-worker.js` の `CACHE_NAME` を変更（例: `weather-station-v1` → `v2`）するとクライアントの次回読み込み時に旧キャッシュを破棄する。

### スコープ

SW を `/static/service-worker.js` から配信すると `/static/` 以下しか制御できないため、`main.py` で `/service-worker.js` 専用エンドポイントを設け `Service-Worker-Allowed: /` ヘッダー付きで配信している。

---

## テストデータ投入

`scripts/insert_test_data.py` で日変化を模したダミーデータを投入できる：

```bash
# 必要パッケージ（ホスト側）
pip install requests

# 60件（30秒間隔で約30分ぶん）投入
python scripts/insert_test_data.py --url http://localhost:8765 --count 60
```

---

## トラブルシューティング

| 症状 | 確認事項 |
|------|---------|
| 8765 にアクセスできない | `docker compose ps` でコンテナ状態確認、ポート競合の場合 `docker-compose.yml` のホスト側ポートを変更 |
| データが表示されない | `data/weather.db` の存在確認、`scripts/insert_test_data.py` でテストデータ投入 |
| SSE が繋がらない | リバースプロキシ（nginx 等）使用時はバッファリングを無効化（`X-Accel-Buffering: no`）、`proxy_buffering off;` |
| グラフが古いまま | ハードリロード（Cmd+Shift+R）、または SW の `CACHE_NAME` を更新 |
| `pressure` カラムがない | `_MIGRATIONS` の `ALTER TABLE ADD COLUMN pressure` が走るので再起動で自動対応 |
| ロジック変更後 rate/delta が古い値のまま | `python3 scripts/migrate.py` で DB を新ロジックに変換 |
| ダークモードが切り替わらない | OS のシステム設定（外観）に依存。アプリ内トグルは未実装 |

ログ確認:
```bash
docker compose logs -f weather-server
```

DB 直接確認:
```bash
docker compose exec weather-server sqlite3 /app/data/weather.db \
  "SELECT COUNT(*), MIN(recorded_at), MAX(recorded_at) FROM weather_records;"
```
