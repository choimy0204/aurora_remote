"""
안내 서버 프로세스 진입점 — AuroraClient 가 '이 PC 에서 안내 서버 운영' 을 켰을 때 별도 프로세스로 띄운다
(python -m wan.relay_run 으로도 실행 가능).  LAN 프로그램과 분리되어 있어 여기가 죽어도 LAN 은 영향이 없다.

- 공용 키는 명령줄(작업 관리자에 보임)이 아니라 환경 변수 AURORA_RELAY_KEY 로 받는다.
- 부모가 사라지면 같이 끝나고, 5초마다 alive 파일을 갱신한다 (감독자가 멈춤을 알아챔).
- 포트가 이미 쓰이고 있으면 EXIT_PORT_BUSY 로 끝난다 (화면에 이유를 보여 주기 위해).
"""

import argparse
import asyncio
import logging
import os
import threading
import time

from .agent import FileLog, _watch_parent
from . import connect_code, discover
from .config import DISCOVER_PORT, RELAY_EXIT_PORT_BUSY as EXIT_PORT_BUSY, WanConfig, default_dir
from .netutil import lan_addresses
from .server import RelayServer, run_server


def _alive_loop(path: str):
    while True:
        try:
            with open(path, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
        time.sleep(5)


class _LogHandler(logging.Handler):
    """서버의 logging 출력을 wan_relay.log 로 보냄"""
    def __init__(self, sink):
        super().__init__(logging.INFO)
        self.sink = sink

    def emit(self, record):
        self.sink(record.getMessage())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Aurora WAN 안내 서버 (AuroraClient 가 띄우는 프로세스)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--data", default=os.path.join(default_dir(), "relay_data"))
    ap.add_argument("--update-dir", default="")
    ap.add_argument("--parent-pid", type=int, default=0)
    ap.add_argument("--discover-port", type=int, default=DISCOVER_PORT)
    args, _ = ap.parse_known_args(argv)          # --wan-relay 같은 진입 표시는 무시
    base = default_dir()
    log = FileLog(os.path.join(base, "wan_relay.log"))
    logging.getLogger().addHandler(_LogHandler(log))
    logging.getLogger().setLevel(logging.INFO)
    if args.parent_pid:
        _watch_parent(args.parent_pid)
    threading.Thread(target=_alive_loop, args=(os.path.join(base, "wan_relay.alive"),), daemon=True).start()
    key = os.environ.get("AURORA_RELAY_KEY", "")

    def code() -> str:
        ext = (WanConfig(base).get("relay_external") or "").strip()
        urls = ([connect_code.make_url(ext, args.port)] if ext else []) + [f"ws://{ip}:{args.port}" for ip in lan_addresses()[:3]]
        return connect_code.encode(urls, key)
    threading.Thread(target=discover.serve, args=(code, threading.Event(), args.discover_port, log), daemon=True).start()
    server = RelayServer(args.data, key, args.update_dir or None)
    try:
        asyncio.run(run_server(server, args.host, args.port))
    except OSError as e:
        log(f"안내 서버를 시작하지 못했습니다 (포트 {args.port}): {e}")
        return EXIT_PORT_BUSY
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
