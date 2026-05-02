"""DB マイグレーションランナー。

各マイグレーションは ID で識別され、`_schema_migrations` テーブルに記録される。
適用済みは2回目以降スキップされるので、何度実行しても安全。

Usage:
    python3 scripts/migrate.py                # 未適用マイグレーションを全て実行
    python3 scripts/migrate.py --dry-run      # 適用予定の表示のみ（書き込みなし）
    python3 scripts/migrate.py --list         # 適用済み・未適用の一覧
    python3 scripts/migrate.py --no-backup    # バックアップをスキップ（非推奨）
    python3 scripts/migrate.py --db <path>    # DB パスを指定（デフォルト: data/weather.db）

新しいマイグレーションを追加する場合は、本ファイル下部に
`@migration(id, description)` デコレータ付きの関数を追加するだけ。
ID は時系列に並べる: "YYYY-MM-DD_NNN_short_name" 形式推奨。

実行環境:
    Python 3.10+ stdlib のみ（追加パッケージ不要）。
    docker コンテナ外（ホスト側）から data/weather.db に対して実行可能。
"""

from __future__ import annotations

import argparse
import bisect
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

MIGRATIONS_TABLE = "_schema_migrations"

# Registered migrations (順序は登録順 = ファイル内記載順)
_REGISTRY: list[dict] = []


def migration(mig_id: str, description: str) -> Callable:
    """マイグレーション登録デコレータ。"""
    def _wrap(func: Callable[[sqlite3.Connection], None]) -> Callable:
        _REGISTRY.append({"id": mig_id, "description": description, "fn": func})
        return func
    return _wrap


def _init_table(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
            id          TEXT PRIMARY KEY,
            applied_at  REAL NOT NULL,
            description TEXT
        )
    """)
    conn.commit()


def _applied_set(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute(f"SELECT id FROM {MIGRATIONS_TABLE}")
    return {r[0] for r in cur.fetchall()}


def _mark_applied(conn: sqlite3.Connection, mig_id: str, description: str) -> None:
    conn.execute(
        f"INSERT INTO {MIGRATIONS_TABLE} (id, applied_at, description) "
        "VALUES (?, ?, ?)",
        (mig_id, time.time(), description),
    )
    conn.commit()


def _backup_db(db_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = db_path.parent / f"{db_path.stem}.bak.migrate.{ts}{db_path.suffix}"
    shutil.copy2(db_path, bak)
    return bak


# ============================================================================
# Migrations
# ============================================================================

@migration(
    "2026-05-02_001_rainfall_anomaly_zero_and_rate_recompute",
    "Zero rainfall_delta where apparent rate > 100 mm/h "
    "(counter restoration), and recompute rainfall_rate via 0.1mm virtual "
    "event algorithm (LOOKBACK=30min, ISOLATED_SPREAD=30min, WINDOW=10min)",
)
def _m_2026_05_02_001(conn: sqlite3.Connection) -> None:
    """異常雨量delta のゼロ化 + rainfall_rate を新ロジックで全件再計算。"""
    TIP_RESOLUTION = 0.1
    LOOKBACK_SEC = 1800.0
    ISOLATED_SPREAD_SEC = 1800.0
    RATE_WINDOW_SEC = 600.0
    MAX_APPARENT_RATE = 100.0

    # --- Phase 1: 異常delta のゼロ化 ---
    cur = conn.execute("""
        SELECT id, recorded_at, rainfall_delta, prev_t FROM (
            SELECT id, recorded_at, rainfall_delta,
                   LAG(recorded_at) OVER (ORDER BY recorded_at) AS prev_t
            FROM weather_records
        )
        WHERE rainfall_delta > 0 AND prev_t IS NOT NULL
    """)
    anomaly_ids: list[int] = []
    for rid, t, delta, prev_t in cur:
        gap = t - prev_t
        if gap > 0 and (delta / gap * 3600.0) > MAX_APPARENT_RATE:
            anomaly_ids.append(rid)

    if anomaly_ids:
        conn.executemany(
            "UPDATE weather_records SET rainfall_delta = 0 WHERE id = ?",
            [(i,) for i in anomaly_ids],
        )
        conn.commit()
    print(f"    Phase 1: zeroed {len(anomaly_ids)} anomaly deltas")

    # --- Phase 2: rainfall_rate 全件再計算 ---
    def _vevents(tip_t: float, tip_d: float, prev_t: float | None) -> list[float]:
        n = round(tip_d / TIP_RESOLUTION)
        if n <= 0:
            return []
        if prev_t is None or (tip_t - prev_t) > LOOKBACK_SEC:
            step = ISOLATED_SPREAD_SEC / n
            return [tip_t - ISOLATED_SPREAD_SEC + step * (i + 0.5) for i in range(n)]
        gap = tip_t - prev_t
        step = gap / n
        return [prev_t + step * (i + 0.5) for i in range(n)]

    cur = conn.execute(
        "SELECT recorded_at, rainfall_delta FROM weather_records "
        "WHERE rainfall_delta > 0 ORDER BY recorded_at"
    )
    virtuals: list[float] = []
    prev_tip: float | None = None
    for T, D in cur:
        virtuals.extend(_vevents(T, D, prev_tip))
        prev_tip = T
    virtuals.sort()

    cur = conn.execute("SELECT id, recorded_at FROM weather_records ORDER BY id")
    updates = []
    for rid, t in cur:
        lo = bisect.bisect_right(virtuals, t - RATE_WINDOW_SEC)
        hi = bisect.bisect_right(virtuals, t)
        rate = (hi - lo) * TIP_RESOLUTION * (3600.0 / RATE_WINDOW_SEC)
        updates.append((rate, rid))

    batch = 5000
    for i in range(0, len(updates), batch):
        conn.executemany(
            "UPDATE weather_records SET rainfall_rate = ? WHERE id = ?",
            updates[i:i + batch],
        )
        conn.commit()
    print(f"    Phase 2: recomputed {len(updates)} rates "
          f"from {len(virtuals)} virtual 0.1mm events")


@migration(
    "2026-05-02_002_null_out_of_range_sensor_values",
    "NULL out sensor values (temperature/humidity/wind/pressure/etc) "
    "that fall outside their physical valid range; preserves the record itself.",
)
def _m_2026_05_02_002(conn: sqlite3.Connection) -> None:
    """物理的にあり得ないセンサー値を NULL に置換（レコードは残す）。
    新規 POST は Pydantic で同じ範囲が検証済みなので、この migration は既存
    データの掃除目的。範囲は app/models.py の WeatherInput と一致させること。
    """
    constraints: list[tuple[str, float | None, float | None]] = [
        ("temperature", -60.0, 70.0),
        ("humidity", 0.0, 100.0),
        ("rainfall_total", 0.0, None),
        ("wind_dir", 0.0, 360.0),
        ("wind_avg", 0.0, 200.0),
        ("wind_gust", 0.0, 200.0),
        ("illuminance", 0.0, 200000.0),
        ("uv_index", 0.0, 20.0),
        ("pressure", 800.0, 1100.0),
    ]

    total_nulled = 0
    for field, lo, hi in constraints:
        clauses: list[str] = []
        if lo is not None:
            clauses.append(f"{field} < {lo}")
        if hi is not None:
            clauses.append(f"{field} > {hi}")
        cond = " OR ".join(clauses)
        cur = conn.execute(
            f"UPDATE weather_records SET {field} = NULL "
            f"WHERE {field} IS NOT NULL AND ({cond})"
        )
        affected = cur.rowcount
        if affected:
            print(f"    {field}: nulled {affected} out-of-range values")
            total_nulled += affected
    conn.commit()
    if total_nulled == 0:
        print("    no out-of-range values found")


# ============================================================================
# 今後の追加例 (テンプレート):
#
# @migration(
#     "2026-06-01_001_example",
#     "What this migration does, in one sentence",
# )
# def _m_2026_06_01_001(conn: sqlite3.Connection) -> None:
#     conn.execute("ALTER TABLE weather_records ADD COLUMN new_field REAL")
#     conn.commit()
# ============================================================================


# ============================================================================
# Runner
# ============================================================================

def cmd_list(conn: sqlite3.Connection) -> int:
    _init_table(conn)
    applied = _applied_set(conn)
    print(f"{'STATUS':<8} {'ID':<60} {'APPLIED_AT'}")
    print("-" * 105)
    cur = conn.execute(
        f"SELECT id, applied_at FROM {MIGRATIONS_TABLE} ORDER BY applied_at"
    )
    for mid, ts in cur.fetchall():
        when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{'APPLIED':<8} {mid:<60} {when}")
    pending = [m for m in _REGISTRY if m["id"] not in applied]
    for m in pending:
        print(f"{'PENDING':<8} {m['id']:<60} -")
    if not _REGISTRY and not applied:
        print("(no migrations registered)")
    return 0


def cmd_run(conn: sqlite3.Connection, db_path: Path,
            dry_run: bool, do_backup: bool) -> int:
    _init_table(conn)
    applied = _applied_set(conn)
    pending = [m for m in _REGISTRY if m["id"] not in applied]

    if not pending:
        print("No pending migrations.")
        return 0

    print(f"Pending migrations ({len(pending)}):")
    for m in pending:
        print(f"  - {m['id']}")
        print(f"    {m['description']}")

    if dry_run:
        print("\n[DRY-RUN] Nothing applied.")
        return 0

    if do_backup:
        bak = _backup_db(db_path)
        print(f"\nBackup: {bak}")

    for m in pending:
        print(f"\n>>> Applying {m['id']}")
        t0 = time.time()
        try:
            m["fn"](conn)
        except Exception as exc:
            print(f"    ✗ FAILED: {exc}")
            print("       (migration not marked as applied; "
                  "fix and re-run, or restore from backup)")
            return 1
        _mark_applied(conn, m["id"], m["description"])
        print(f"    ✓ done in {time.time() - t0:.2f}s")

    print(f"\nApplied {len(pending)} migration(s) successfully.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Weather Server DB migration runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--db", default="data/weather.db",
                        help="SQLite DB path (default: data/weather.db)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show pending migrations without applying")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip auto-backup before migration")
    parser.add_argument("--list", action="store_true",
                        help="List applied / pending migrations")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        if args.list:
            return cmd_list(conn)
        return cmd_run(conn, db_path,
                       dry_run=args.dry_run,
                       do_backup=not args.no_backup)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
