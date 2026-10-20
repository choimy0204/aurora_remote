"""
데이터 사용량 기록 — 통로 계층에서 주고받은 바이트를 날짜별로 JSON 에 누적
(직접 연결과 비상 중계 모두 같은 곳에서 센다. 출장지 핫스팟 요금 관리용)
"""

import os
import json
import time
import threading
from datetime import date

KEEP_DAYS = 400
FLUSH_EVERY = 10.0     # 초 — 프레임마다 파일을 쓰지 않도록 모아서 저장


class UsageStore:
    def __init__(self, path: str, clock=time.monotonic, today=date.today):
        self.path = path
        self._clock, self._today = clock, today
        self._lock = threading.Lock()
        self._days: dict = {}
        self._dirty = False
        self._last_flush = clock()
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._days = {k: {"tx": int(v.get("tx", 0)), "rx": int(v.get("rx", 0))}
                          for k, v in data.items() if isinstance(v, dict)}
        except Exception:
            self._days = {}

    def add(self, tx: int = 0, rx: int = 0):
        if not (tx or rx):
            return
        key = self._today().isoformat()
        with self._lock:
            d = self._days.setdefault(key, {"tx": 0, "rx": 0})
            d["tx"] += tx
            d["rx"] += rx
            self._dirty = True
            due = self._clock() - self._last_flush >= FLUSH_EVERY
        if due:
            self.flush()

    def flush(self):
        with self._lock:
            if not self._dirty:
                return
            days = sorted(self._days)[-KEEP_DAYS:]
            self._days = {k: self._days[k] for k in days}
            snapshot = json.dumps(self._days, indent=1)
            self._dirty = False
            self._last_flush = self._clock()
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(snapshot)
            os.replace(tmp, self.path)
        except OSError:
            with self._lock:
                self._dirty = True

    def day_total(self, day: str | None = None) -> int:
        key = day or self._today().isoformat()
        with self._lock:
            d = self._days.get(key, {})
            return d.get("tx", 0) + d.get("rx", 0)

    def month_total(self, month: str | None = None) -> int:
        """month: 'YYYY-MM' (기본: 이번 달)"""
        prefix = month or self._today().isoformat()[:7]
        with self._lock:
            return sum(v["tx"] + v["rx"] for k, v in self._days.items() if k.startswith(prefix))


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
