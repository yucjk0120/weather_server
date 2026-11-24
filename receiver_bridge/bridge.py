#!/usr/bin/env python3
"""気象受信機 (ESP32-C3) と weather_server をつなぐブリッジ。

受信機は USB シリアルに 1 行 1 件の JSON を流すだけなので、ここで次を行う。
  - 記録 : 受信機の出力をすべて時刻付きで日ごとのファイルに残す
  - 登録 : 対象センサの電文を weather_server の形式に直し、送信待ち (SQLite) に積んでから
           POST /api/weather する。登録できなければ消さずに再送する
  - 見張り: 受信が途絶えたら受信機に段階的に指示を出し (KICK → RADIO_RESET → REBOOT)、
           どの段階で戻ったかを記録する。受信機から何も届かなければハードウェアリセットする
  - 公開 : /status (状態の JSON) と /logs/ (記録) を LAN に読み取り専用で出す

シリアルの制御線 (DTR/RTS) は、ハードウェアリセットのとき以外は触らない。
ESP32-C3 は制御線の組み合わせでリセットされるので、つないだだけで状態を壊さないため。
"""
import collections
import datetime as dt
import functools
import glob
import http.server
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

import serial


def env(name, default):
    v = os.environ.get(name)
    return type(default)(v) if v not in (None, "") else default


PORT_GLOB    = env("SERIAL_PORT", "/dev/serial/by-id/usb-Espressif*")
API_URL      = env("API_URL", "http://weather-server:8000/api/weather")
STATION_ID   = env("STATION_ID", "ST-001")
TARGET_MODEL = env("TARGET_MODEL", "Cotech-367959")
DATA_DIR     = env("DATA_DIR", "/data")
HTTP_PORT    = env("HTTP_PORT", 8766)
KEEP_DAYS    = env("KEEP_DAYS", 30)
TZ           = ZoneInfo(env("TZ", "Asia/Tokyo"))

# 受信が途絶えてからの段階的な指示 (秒)。センサは 16 秒ごとに送信し、正常だった
# 9/1〜14 では 2 分を超える途切れは 2 週間に 1 回、3 分を超える途切れは一度もなかった。
KICK_AFTER        = env("KICK_AFTER_S", 120.0)
RADIO_RESET_AFTER = env("RADIO_RESET_AFTER_S", 180.0)
REBOOT_AFTER      = env("REBOOT_AFTER_S", 240.0)
REBOOT_BACKOFF_MAX = 8          # 再起動の間隔は 4 → 8 → 16 → 32 分で打ち止め
# 受信機から何も届かない (PING にも応えない) ときのハードウェアリセット
SILENT_AFTER      = env("SILENT_AFTER_S", 90.0)
HW_RESET_INTERVAL = env("HW_RESET_INTERVAL_S", 300.0)
PING_INTERVAL     = env("PING_INTERVAL_S", 15.0)
# つないだ直後は受信機の起動と最初の受信を待ってから見張りを始める
CONNECT_GRACE     = env("CONNECT_GRACE_S", 30.0)
# センサは 2 回に 1 回、同じ電文を 5ms 間隔で 2 つ送る。2 つ目は捨てる。
DUP_WINDOW        = 2.0

# rtl_433 の電文 → weather_server の項目
FIELDS = {
    "temperature":    "temperature_C",
    "humidity":       "humidity",
    "rainfall_total": "rain_mm",
    "wind_dir":       "wind_dir_deg",
    "wind_avg":       "wind_avg_m_s",
    "wind_gust":      "wind_max_m_s",
    "illuminance":    "light_lux",
    "uv_index":       "uvi",
}


def now_text():
    return dt.datetime.now(TZ).isoformat(timespec="milliseconds")


def say(msg):
    print(f"{now_text()} {msg}", flush=True)


# ---------------------------------------------------------------------------
# 記録
# ---------------------------------------------------------------------------
class DailyLog:
    def __init__(self, directory):
        self.directory = directory
        self.lock = threading.Lock()
        self.day = None
        self.fp = None
        os.makedirs(directory, exist_ok=True)

    def write(self, text):
        with self.lock:
            day = dt.datetime.now(TZ).date().isoformat()
            if day != self.day:
                if self.fp:
                    self.fp.close()
                self.day = day
                self.fp = open(os.path.join(self.directory, f"receiver-{day}.log"),
                               "a", encoding="utf-8", buffering=1)
                limit = time.time() - KEEP_DAYS * 86400
                for path in glob.glob(os.path.join(self.directory, "receiver-*.log")):
                    if os.path.getmtime(path) < limit:
                        os.remove(path)
            self.fp.write(f"{now_text()} {text}\n")

    def event(self, kind, **fields):
        """ブリッジ自身の出来事も、同じファイルに JSON で残す。"""
        line = json.dumps({"t": "bridge", "ev": kind, **fields}, ensure_ascii=False)
        self.write(line)
        say(line)


# ---------------------------------------------------------------------------
# 送信待ち (受信したら先にディスクへ積み、登録できたら消す)
# ---------------------------------------------------------------------------
class Outbox:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("""CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, payload TEXT,
                attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, last_error TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS dead (
                id INTEGER PRIMARY KEY, created REAL, payload TEXT, error TEXT, moved REAL)""")

    def add(self, payload):
        with self.lock:
            self.db.execute("INSERT INTO outbox (created, payload) VALUES (?, ?)",
                            (time.time(), json.dumps(payload)))
        self.wake.set()

    def oldest(self):
        with self.lock:
            return self.db.execute(
                "SELECT id, payload, attempts, next_try FROM outbox ORDER BY id LIMIT 1").fetchone()

    def done(self, rid):
        with self.lock:
            self.db.execute("DELETE FROM outbox WHERE id = ?", (rid,))

    def retry_later(self, rid, attempts, error):
        delay = min(60.0, 2.0 ** min(attempts, 6))
        with self.lock:
            self.db.execute("UPDATE outbox SET attempts = ?, next_try = ?, last_error = ? WHERE id = ?",
                            (attempts, time.time() + delay, error, rid))

    def give_up(self, rid, payload, error):
        with self.lock:
            self.db.execute("INSERT INTO dead (id, created, payload, error, moved) "
                            "SELECT id, created, payload, ?, ? FROM outbox WHERE id = ?",
                            (error, time.time(), rid))
            self.db.execute("DELETE FROM outbox WHERE id = ?", (rid,))

    def counts(self):
        with self.lock:
            return (self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
                    self.db.execute("SELECT COUNT(*) FROM dead").fetchone()[0])


def post(payload):
    req = urllib.request.Request(API_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(300).decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(300).decode(errors="replace")
    except Exception as e:   # 接続できない・タイムアウト
        return None, str(e)


def sender_loop(outbox, state, log):
    while True:
        row = outbox.oldest()
        if not row:
            outbox.wake.wait(5)
            outbox.wake.clear()
            continue
        rid, text, attempts, next_try = row
        if next_try > time.time():
            # 古い順を守るため、先頭が待ち中なら追い越さずに待つ
            outbox.wake.wait(min(5.0, next_try - time.time()))
            outbox.wake.clear()
            continue
        payload = json.loads(text)
        code, body = post(payload)
        with state.lock:
            state.last_post = {"at": time.time(), "code": code}
        if code is not None and 200 <= code < 300:
            outbox.done(rid)
            with state.lock:
                state.posted += 1
            if '"rejected"' in body:   # サーバが異常値として登録しなかった (再送しても同じ)
                log.event("rejected", body=body)
        elif code is not None and 400 <= code < 500 and code not in (408, 429):
            outbox.give_up(rid, payload, f"{code} {body}")   # 再送しても通らない
            log.event("post_dropped", code=code, body=body)
        else:
            outbox.retry_later(rid, attempts + 1, f"{code} {body}")
            if attempts == 0 or attempts % 10 == 0:
                log.event("post_retry", code=code, error=body[:200], attempts=attempts + 1)


# ---------------------------------------------------------------------------
# 受信機とのやりとり
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.port_path = None
        self.connected_at = None
        self.last_line_at = None
        self.last_sensor_at = time.time()   # 起動直後に誤って指示を出さない
        self.sensor_times = collections.deque(maxlen=200)
        self.last_key = None
        self.last_key_at = 0.0
        self.last_diag = None
        self.last_boot = None
        self.posted = 0
        self.duplicates = 0
        self.skipped = 0
        self.last_post = None
        # 見張り
        self.level = 0
        self.reboots = 0
        self.next_action_at = 0.0
        self.last_hw_reset_at = 0.0
        self.episode = None
        self.episodes = collections.deque(maxlen=30)


class Device:
    def __init__(self, state, log, outbox):
        self.state, self.log, self.outbox = state, log, outbox
        self.port = None
        self.wlock = threading.Lock()

    def send(self, cmd):
        with self.wlock:
            if not self.port:
                return False
            try:
                self.port.write((cmd + "\n").encode())
                return True
            except (serial.SerialException, OSError):
                return False

    def hw_reset(self):
        """EN を引いてチップをリセットする (RST ボタンと同じ)。ファームが固まっていても効く。"""
        with self.wlock:
            p = self.port
            if not p:
                return False
            try:
                p.dtr = False        # (DTR,RTS)=(0,1): EN が下がりリセット
                time.sleep(0.1)
                p.rts = False        # (0,0): リセット解除。起動モードは通常
                time.sleep(0.05)
                p.dtr = True         # (1,0) → (1,1) の順に戻す。逆順だとまたリセットになる
                p.rts = True
                return True
            except (serial.SerialException, OSError):
                return False

    def run(self):
        while True:
            found = sorted(glob.glob(PORT_GLOB)) if any(c in PORT_GLOB for c in "*?[") else \
                ([PORT_GLOB] if os.path.exists(PORT_GLOB) else [])
            if not found:
                time.sleep(2)
                continue
            path = found[0]
            try:
                # 制御線は pyserial の既定 (両方オン) のまま。Linux の cdc_acm は開いたときに
                # 両方を同時にオンにするので、つないだだけではリセットされない。
                with serial.Serial(path, 115200, timeout=1) as port:
                    with self.wlock:
                        self.port = port
                    with self.state.lock:
                        self.state.port_path = path
                        self.state.connected_at = time.time()
                        # 接続した時点を起点にする (切断前の古い時刻で即リセットしないため)
                        self.state.last_line_at = time.time()
                        self.state.next_action_at = max(self.state.next_action_at,
                                                        time.time() + CONNECT_GRACE)
                    self.log.event("connected", port=path)
                    self.send("STAT")
                    buf = b""
                    while True:
                        chunk = port.read(port.in_waiting or 1)
                        if not chunk:
                            continue
                        buf += chunk
                        while b"\n" in buf:
                            raw, buf = buf.split(b"\n", 1)
                            text = raw.rstrip(b"\r").decode("utf-8", errors="replace")
                            if text:
                                self.on_line(text, time.time())
            except (serial.SerialException, OSError) as e:
                self.log.event("disconnected", error=str(e))
            finally:
                with self.wlock:
                    self.port = None
                with self.state.lock:
                    self.state.port_path = None
            time.sleep(2)

    def on_line(self, text, at):
        st = self.state
        with st.lock:
            st.last_line_at = at
        msg = None
        if text.startswith("{"):
            try:
                msg = json.loads(text)
            except ValueError:
                pass
        kind = msg.get("t") if isinstance(msg, dict) else None
        if kind != "pong":   # 15 秒ごとの生存確認の応答は記録しない (1 日 6 千行近くになる)
            self.log.write(text)
        if msg is None:
            return
        if kind == "rx":
            self.on_rx(msg, at)
        elif kind == "diag":
            with st.lock:
                st.last_diag = msg
        elif kind == "boot":
            with st.lock:
                st.last_boot = {**msg, "at": at}

    def on_rx(self, msg, at):
        d = msg.get("d") or {}
        if d.get("model") != TARGET_MODEL:
            return
        rec = {}
        for dst, src in FIELDS.items():
            v = d.get(src)
            if v is None and src == "temperature_C" and d.get("temperature_F") is not None:
                v = (float(d["temperature_F"]) - 32.0) / 1.8
            if v is None:
                # 欠けた項目を 0 にすると誤った観測値になるので、その電文は使わない
                with self.state.lock:
                    self.state.skipped += 1
                self.log.event("skipped", missing=src)
                return
            rec[dst] = float(v)
        st = self.state
        key = tuple(rec[k] for k in sorted(rec))
        with st.lock:
            dup = (key == st.last_key and at - st.last_key_at < DUP_WINDOW)
            st.last_key, st.last_key_at = key, at
            if dup:
                st.duplicates += 1
                return
            st.last_sensor_at = at
            st.sensor_times.append(at)
            ep = st.episode
            if ep:   # 途絶からの回復: 直前に何をしたかを残す
                ep["recovered_at"] = at
                ep["duration_s"] = round(at - ep["started_at"], 1)
                st.episodes.append(ep)
                st.episode = None
            st.level = 0
            st.reboots = 0
            st.next_action_at = 0.0
        if ep:
            self.log.event("recovered", **ep)
        payload = {"station_id": STATION_ID, "recorded_at": at, **rec}
        if msg.get("pres") is not None:
            payload["pressure"] = float(msg["pres"])
        self.outbox.add(payload)


def watch_loop(dev, state, log):
    last_ping = 0.0
    while True:
        time.sleep(1)
        now = time.time()
        with state.lock:
            connected = state.port_path is not None
            last_line = state.last_line_at
        if not connected:
            continue
        if now - last_ping >= PING_INTERVAL:
            last_ping = now
            dev.send("PING")

        # 受信機から何も届かない = ファームが固まっているか、USB が詰まっている
        if last_line and now - last_line > SILENT_AFTER and now - state.last_hw_reset_at > HW_RESET_INTERVAL:
            state.last_hw_reset_at = now
            ok = dev.hw_reset()
            log.event("hw_reset", silent_s=round(now - last_line), ok=ok)
            continue

        # 受信の途絶に対する段階的な指示。1 回の見回りで進むのは 1 段だけで、
        # 段と段のあいだは決めた間隔を空ける (つなぎ直した直後に一気に進まないように)。
        action = None
        with state.lock:
            age = now - state.last_sensor_at
            if age < KICK_AFTER or now < state.next_action_at:
                continue
            if state.episode is None:
                state.episode = {"started_at": state.last_sensor_at, "actions": []}
            if state.level == 0:
                action, state.level = "KICK", 1
                state.next_action_at = now + (RADIO_RESET_AFTER - KICK_AFTER)
            elif state.level == 1 and age >= RADIO_RESET_AFTER:
                action, state.level = "RADIO_RESET", 2
                state.next_action_at = now + (REBOOT_AFTER - RADIO_RESET_AFTER)
            elif state.level >= 2 and age >= REBOOT_AFTER:
                # 再起動しても戻らなければ、間隔を倍々に延ばして続ける (4→8→16→32 分)
                action, state.level = "REBOOT", 3
                state.reboots += 1
                state.next_action_at = now + REBOOT_AFTER * min(2 ** state.reboots, REBOOT_BACKOFF_MAX)
            if action:
                state.episode["actions"].append({"cmd": action, "after_s": round(age, 1)})
                state.episode["last_action"] = action
        if action:
            ok = dev.send(action)
            log.event("action", cmd=action, rx_age_s=round(age), ok=ok)


# ---------------------------------------------------------------------------
# 公開 (/status と /logs/)
# ---------------------------------------------------------------------------
def status_json(state, outbox):
    now = time.time()
    with state.lock:
        pending, dead = outbox.counts()
        return {
            "now": now_text(),
            "port": state.port_path,
            "line_age_s": None if state.last_line_at is None else round(now - state.last_line_at, 1),
            "sensor_age_s": round(now - state.last_sensor_at, 1),
            "sensor_pkts_10min": sum(1 for t in state.sensor_times if now - t <= 600),
            "posted": state.posted,
            "duplicates": state.duplicates,
            "skipped": state.skipped,
            "outbox": pending,
            "dead": dead,
            "last_post": state.last_post,
            "watch": {"level": state.level, "reboots": state.reboots, "episode": state.episode},
            "episodes": list(state.episodes),
            "last_boot": state.last_boot,
            "last_diag": state.last_diag,
        }


def serve(state, outbox, log_dir):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=log_dir, **kw)

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path in ("/", "/status"):
                body = json.dumps(status_json(state, outbox), ensure_ascii=False, indent=1).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/logs"):
                self.path = self.path[len("/logs"):] or "/"
                super().do_GET()
            else:
                self.send_error(404)

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    say(f"状態: http://<このホスト>:{HTTP_PORT}/status  記録: http://<このホスト>:{HTTP_PORT}/logs/")


def main():
    log_dir = os.path.join(DATA_DIR, "logs")
    log = DailyLog(log_dir)
    outbox = Outbox(os.path.join(DATA_DIR, "outbox.db"))
    state = State()
    dev = Device(state, log, outbox)
    log.event("start", api=API_URL, port=PORT_GLOB)
    serve(state, outbox, log_dir)
    threading.Thread(target=sender_loop, args=(outbox, state, log), daemon=True).start()
    threading.Thread(target=watch_loop, args=(dev, state, log), daemon=True).start()
    dev.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
