"""
Bridge — 통로로 들어온 요청을 이 PC 안의 로컬 서버(127.0.0.1)로 넘기고 응답을 돌려준다
====================================================================================
기존 CommandHandler 를 그대로 재사용하되, WAN 으로는 파일·원격 화면 경로만 열어 준다.
  허용  /info  /fs/*  /remote/*
  차단  /command (Aurora 실행·종료)  /discover (공정망 대역 스캔)  via= (LAN 다단 중계)  그 밖의 모든 경로
→ WAN 으로 들어온 사람이 LAN 쪽 기능을 건드릴 수 없다.
LAN 키(X-Aurora-Key)는 여기서 붙인다 — 접속 코드를 통과한 사람은 키를 몰라도 된다.
"""

import json
from concurrent.futures import ThreadPoolExecutor

import requests

from . import protocol as P
from .config import QUALITY, SAVER_BOX

ALLOWED = {
    "/info":         {"GET"},
    "/fs/roots":     {"GET"}, "/fs/list":   {"GET"}, "/fs/walk":   {"GET"}, "/fs/file": {"GET"},
    "/fs/mkdir":     {"PUT"}, "/fs/upload": {"PUT"}, "/fs/rename": {"PUT"}, "/fs/delete": {"PUT"},
    "/remote/frame": {"GET"}, "/remote/input": {"GET"}, "/remote/end": {"GET"}, "/remote/clip": {"GET"},
}
FORWARD_HEADERS  = ("range",)    # 이어받기
RESPONSE_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")


class Policy:
    """어떤 요청을 통과시킬지 · 화질 상한을 적용할지 (설정은 원격으로 바뀔 수 있어 매번 읽음)"""

    def __init__(self, cfg):
        self.cfg = cfg

    def check(self, served) -> tuple | None:
        """막아야 하면 (상태 코드, 이유), 통과면 None"""
        methods = ALLOWED.get(served.route)
        if methods is None or served.method not in methods:
            return 403, "WAN 으로는 허용되지 않는 요청입니다"
        if "via" in served.params:
            return 403, "WAN 으로는 허용되지 않는 요청입니다 (via)"
        if served.route.startswith("/fs/") and not self.cfg.get("allow_files"):
            return 403, "이 장비는 파일 전송이 꺼져 있습니다"
        return None

    def adjust_params(self, route: str, params: dict) -> dict:
        """화면 요청에 현장이 정한 최고 화질을 적용 (사용자가 더 높게 요청해도 여기서 낮춤)"""
        params = dict(params)
        if route != "/remote/frame":
            return params
        top = self.cfg.get("max_quality")
        jpeg_max = QUALITY.get(top, QUALITY["고화질"])[0]
        try:
            params["q"] = str(min(int(params.get("q", jpeg_max)), jpeg_max))
        except ValueError:
            params["q"] = str(jpeg_max)
        if top == "절약":
            for key, limit in zip(("w", "h"), SAVER_BOX):
                try:
                    params[key] = str(min(int(params.get(key, limit)), limit))
                except ValueError:
                    params[key] = str(limit)
        return params


class _ServedBody:
    """로컬 서버로 올려 보낼 요청 본문 — 통로로 들어오는 대로 흘려 보냄 (requests 가 길이를 알도록 __len__)"""

    def __init__(self, served):
        self._s, self._left = served, served.length

    def __len__(self):
        return self._s.length

    def __iter__(self):
        while self._left > 0:
            chunk = self._s.read_body(P.MAX_DATA)
            if not chunk:
                raise P.WanError("보낸 쪽의 본문이 중간에 끊겼습니다")
            self._left -= len(chunk)
            yield chunk

    def read(self, n=-1):
        if self._left <= 0:
            return b""
        chunk = self._s.read_body(P.MAX_DATA)
        if not chunk:
            raise P.WanError("보낸 쪽의 본문이 중간에 끊겼습니다")
        self._left -= len(chunk)
        return chunk


class Bridge:
    def __init__(self, local_port: int, key_getter, policy: Policy, workers: int = 16, log=None):
        self.local_port, self.key_getter, self.policy = local_port, key_getter, policy
        self._log = log or (lambda msg: None)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wan-bridge")

    def submit(self, served):
        self._pool.submit(self._run, served)

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ── 작업 스레드 ──
    def _run(self, s):
        try:
            self._serve(s)
        except Exception as e:
            self._fail(s, e)

    def _serve(self, s):
        denied = self.policy.check(s)
        if denied:
            self._log(f"차단: {s.method} {s.route} — {denied[1]}")
            return self._reply_json(s, denied[0], {"error": denied[1]})
        headers = {"X-Aurora-Key": self.key_getter()}
        headers.update({k: v for k, v in s.headers.items() if str(k).lower() in FORWARD_HEADERS})
        body = _ServedBody(s) if s.length else None
        url = f"http://127.0.0.1:{self.local_port}{s.route}"
        with requests.request(s.method, url, params=self.policy.adjust_params(s.route, s.params),
                              headers=headers, data=body, stream=True, timeout=(3, 120)) as r:
            s.send_head(r.status_code, {h: r.headers[h] for h in RESPONSE_HEADERS if h in r.headers})
            sent = 0
            for chunk in r.iter_content(P.MAX_DATA):
                if s.cancelled.is_set():
                    return
                s.send_body(chunk)
                sent += len(chunk)
            expected = r.headers.get("Content-Length")
            if expected is not None and expected.isdigit() and sent != int(expected):
                return s.end("응답이 중간에 끊겼습니다")
        s.end()

    def _reply_json(self, s, status: int, obj: dict):
        s.send_head(status, {"Content-Type": "application/json; charset=utf-8"})
        s.send_body(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        s.end()

    def _fail(self, s, exc: Exception):
        if s.cancelled.is_set():
            return
        try:
            if s.head_sent:
                s.end(f"{type(exc).__name__}: {exc}")
            else:
                self._reply_json(s, 502, {"error": f"현장 PC 내부 오류: {exc}"})
        except P.WanError:
            pass   # 통로가 이미 끊김
