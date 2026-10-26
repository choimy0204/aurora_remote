WAN 번들 점검 (Windows 빌드 PC 에서 1회)
=========================================
1. build.bat 과 같은 Python 으로 실행 (aiortc, websockets 가 설치된 환경):
     python -m pip install aiortc websockets pyinstaller
     pyinstaller tools\wan_frozen_check\wan_frozen_check.spec --distpath tools\wan_frozen_check\dist --workpath tools\wan_frozen_check\build --noconfirm
2. 실행:
     tools\wan_frozen_check\dist\wan_frozen_check.exe
3. 기대 출력:
     aiortc 사용 가능: True
     연결 방식: direct | {'hostname': 'FROZEN-FIELD', ...}
     연결 방식: relay  | {'hostname': 'FROZEN-FIELD', ...}
     주소 접속: direct | {'hostname': 'FROZEN-FIELD', ...}
     FROZEN SELFTEST OK
   (기본 AuroraClient 빌드는 aiortc 를 빼서 직접 연결 없이 중계 · 주소 접속 · 암호화만 쓴다 — 그 구성에서는 첫 줄이 aiortc 사용 가능: False 이고 연결 방식이 relay.)
   direct 가 아니라 실패하면 aiortc/av 번들 문제 — AuroraClient.exe 에서도 직접 연결은 안 되고 비상 중계만 동작한다.
   (Linux 에서 같은 spec 으로 확인한 결과: 정상 동작, 실행 파일 크기 59.9MB, 같은 제외 목록의 빈 프로그램은 7.1MB)
