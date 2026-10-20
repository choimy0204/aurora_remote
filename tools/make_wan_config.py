"""
WAN 설정 파일(wan_config.json) 만들기
=====================================
안내 서버 주소와 공용 키가 들어 있는 wan_config.json 을 만든다.

- 현장 PC  : 이 파일을 D:\\RemoteConfig 에 넣으면 AuroraClient 가 WAN 에이전트를 자동으로 시작한다.
- 사용자 PC: 같은 파일을 넣으면 'WAN 원격' 탭이 안내 서버에 연결된다 (--no-enable 권장).
접속 코드·화질·파일 허용 설정은 여기서 정하지 않는다 (사용자 PC 의 '설정 보내기'로 나중에 보낸다).

사용 예:
    python tools/make_wan_config.py --relay ws://192.168.0.10:9100 --key 공용키 --name "CCW 1" --out wan_config.json
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from wan.config import DEFAULTS  # noqa: E402  (저장소 루트를 sys.path 에 넣은 뒤에 가져온다)

SCHEMES = ("ws://", "wss://")


def check_relay(url: str) -> str | None:
    """안내 서버 주소 검사 — 문제가 있으면 이유를, 괜찮으면 None 을 돌려준다"""
    if not url:
        return "--relay 주소가 비어 있습니다."
    if not url.startswith(SCHEMES):
        return "안내 서버 주소는 ws:// 또는 wss:// 로 시작해야 합니다 (받은 값: %s)" % url
    host = url.split("://", 1)[1].split("/", 1)[0]
    if not host or host.startswith(":"):
        return "안내 서버 주소에 IP 또는 이름이 없습니다 (예: ws://192.168.0.10:9100)"
    return None


def build_config(relay: str, key: str, name: str, enabled: bool) -> dict:
    """기본값을 바탕으로 WanConfig 가 읽을 수 있는 설정을 만든다"""
    cfg = dict(DEFAULTS)
    cfg.update({
        "enabled":   bool(enabled),
        "relay_url": relay,
        "relay_key": key,
        "name":      name[:40],
        "agent_id":  "",          # 비워 둔다 — 에이전트가 처음 실행될 때 고유 ID 를 만든다
    })
    return cfg


def write_config(path: str, cfg: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="WAN 설정 파일(wan_config.json) 만들기")
    ap.add_argument("--relay", required=True, help="안내 서버 주소 (예: ws://192.168.0.10:9100)")
    ap.add_argument("--key", default="", help="안내 서버 공용 키 (안내 서버를 --key 로 실행했을 때 같은 값)")
    ap.add_argument("--name", default="", help="처음 표시할 이름 (예: CCW 1)")
    ap.add_argument("--out", default="wan_config.json", help="만들 파일 경로 (기본: 현재 폴더의 wan_config.json)")
    ap.add_argument("--no-enable", action="store_true",
                    help="enabled 를 끈 채로 만든다 (이 PC 를 WAN 원격 대상으로 쓰지 않을 때, 예: 사용자 PC)")
    ap.add_argument("--force", action="store_true", help="이미 파일이 있으면 덮어쓴다")
    args = ap.parse_args(argv)

    relay = args.relay.strip()
    problem = check_relay(relay)
    if problem:
        print("오류: " + problem, file=sys.stderr)
        return 2

    out = os.path.abspath(args.out)
    if os.path.exists(out) and not args.force:
        print("오류: 이미 파일이 있습니다: %s" % out, file=sys.stderr)
        print("      새로 만들려면 --force 를 붙이세요 (기존 파일 내용은 사라집니다).", file=sys.stderr)
        return 3

    key = args.key.strip()
    enabled = not args.no_enable
    write_config(out, build_config(relay, key, args.name.strip(), enabled))

    print("완료: 설정 파일을 만들었습니다.")
    print("  위치       : %s" % out)
    print("  안내 서버  : %s" % relay)
    print("  켜짐       : %s" % ("예 (현장 PC 로 쓰임)" if enabled else "아니오 (사용자 PC 용)"))
    print()
    print("다음에 할 일")
    if enabled:
        print("  1. 이 파일을 현장 PC 의 D:\\RemoteConfig 폴더에 복사합니다.")
        print("  2. 현장 PC 에서 AuroraClient.exe 를 실행하면 WAN 에이전트가 자동으로 시작됩니다.")
        print("  3. 접속 코드는 사용자 PC 의 'WAN 원격' 탭 > '설정 보내기' 로 정합니다.")
        print("     (정하기 전에는 접속 코드가 없어서 모든 접속이 거부됩니다.)")
    else:
        print("  1. 이 파일을 사용자 PC 의 D:\\RemoteConfig 폴더에 복사합니다.")
        print("  2. AuroraClient.exe 를 실행하고 'WAN 원격' 탭을 엽니다.")
    if not key:
        print()
        print("주의: 공용 키가 비어 있습니다. 안내 서버를 --key 로 실행했다면 같은 키를 넣어야 연결됩니다.")
    if not enabled:
        print()
        print("주의: --no-enable 이므로 이 PC 는 WAN 원격 대상으로 켜지지 않습니다.")
        print("      현장 PC 로 쓰려면 enabled 를 true 로 고치거나 --no-enable 없이 --force 로 다시 만드세요.")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
