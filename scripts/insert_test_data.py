"""テスト用ダミーデータをAPIに投入するスクリプト。

Usage:
    python scripts/insert_test_data.py [--url http://localhost:8765] [--count 60]
"""

import argparse
import math
import random
import time

import requests

BASE_URL = "http://localhost:8765"


def generate_record(t: float, idx: int, cumulative_rain: float) -> dict:
    hour = (t % 86400) / 3600  # 0-24
    # 気温: 日変化 (10-25°C)
    temp = 17.5 + 7.5 * math.sin((hour - 6) / 24 * 2 * math.pi)
    temp += random.uniform(-0.5, 0.5)
    # 湿度: 気温の逆相関 (40-90%)
    hum = 65 - 25 * math.sin((hour - 6) / 24 * 2 * math.pi)
    hum += random.uniform(-2, 2)
    # 雨量累計: ランダムに加算
    if random.random() < 0.1:
        cumulative_rain += random.uniform(0.1, 0.5)
    # 風
    wind_avg = abs(2.0 + random.gauss(0, 1.5))
    wind_gust = wind_avg + abs(random.gauss(0, 1.0))
    wind_dir = random.uniform(0, 360)
    # 照度: 日中のみ
    if 6 < hour < 18:
        lux = 30000 * math.sin((hour - 6) / 12 * math.pi) + random.uniform(-500, 500)
    else:
        lux = random.uniform(0, 10)
    # UV: 日中のみ
    uv = max(0, 6 * math.sin((hour - 6) / 12 * math.pi) + random.uniform(-0.3, 0.3)) if 6 < hour < 18 else 0

    return {
        "station_id": "ST-001",
        "recorded_at": t,
        "temperature": round(temp, 1),
        "humidity": round(max(0, min(100, hum)), 1),
        "rainfall_total": round(cumulative_rain, 1),
        "wind_dir": round(wind_dir, 1),
        "wind_avg": round(wind_avg, 1),
        "wind_gust": round(wind_gust, 1),
        "illuminance": round(max(0, lux), 1),
        "uv_index": round(max(0, uv), 1),
    }, cumulative_rain


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=BASE_URL)
    parser.add_argument("--count", type=int, default=60, help="投入レコード数")
    args = parser.parse_args()

    endpoint = f"{args.url}/api/weather"
    now = time.time()
    start = now - args.count * 30  # 30秒間隔で遡る
    rain = 100.0

    print(f"Posting {args.count} records to {endpoint}")
    for i in range(args.count):
        t = start + i * 30
        record, rain = generate_record(t, i, rain)
        resp = requests.post(endpoint, json=record)
        if resp.status_code == 201:
            print(f"  [{i+1}/{args.count}] OK  temp={record['temperature']}°C")
        else:
            print(f"  [{i+1}/{args.count}] ERROR {resp.status_code}: {resp.text}")

    print("Done.")


if __name__ == "__main__":
    main()
