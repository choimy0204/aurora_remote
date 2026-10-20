"""
Aurora WAN 안내 서버 실행 파일 진입점 — WanRelay.spec 으로 AuroraRelay.exe 를 만들 때 쓴다.
실행 옵션은 python -m wan.server 와 같다 (docs/WAN_사용설명서.md 참고).
"""
from wan.server import main

if __name__ == "__main__":
    main()
