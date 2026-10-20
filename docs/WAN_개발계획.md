# WAN 원격 개발 계획 · 현황

기존 LAN 방식은 그대로 두고, 인터넷을 건너는 **WAN 원격**을 같은 AuroraClient 안에 더했다.
단계별 지시는 [`개발_프롬프트_Sonnet.md`](../개발_프롬프트_Sonnet.md), 사용법은 [`WAN_사용설명서.md`](WAN_사용설명서.md).
이 문서는 **지시서와 달라진 결정 · LAN/WAN 격리 구현 · 검증 현황 · 남은 일**을 기록한다.

## 1. 지시서와 달라진 결정

| 항목 | 지시서 원안 | 결정 |
|---|---|---|
| 배치 | 현장용·사용자용·안내 서버 **별도 프로그램** 3개, 원본 복사 | **기존 AuroraClient에 WAN 기능을 더함**. 안내 서버만 새 프로그램(`AuroraRelay.exe`) |
| 화면 | 반출 탭을 바탕으로 사용자용을 만듦 | 반입/반출 탭은 LAN용 그대로, **"WAN 원격" 탭을 새로 만듦** |
| 재사용 | — | `RemoteViewer`(파생 클래스로), `_TransferPane`(요청 함수만 교체)만 가져옴 |
| 빼는 기능 | Aurora 실행·종료, 배포, `/discover`, `via` | 별도 프로그램이 아니므로 **WAN 경로에서 차단**(허용 목록) |
| 현장 에이전트 | 현장용 프로그램 | 같은 exe를 `--wan-agent`로 한 번 더 실행하는 **별도 프로세스** |
| 감독 | — | 죽으면 WAN만 자동 재시작(간격 2→600초, 포기하지 않음), 멈추면 강제 재시작 |

용어: LAN = 같은 공정망 안(지금 방식), WAN = 인터넷 너머의 현장과 회사를 잇는 방식(새로 만듦).

## 2. 구조

```
[내 PC: WAN 원격 탭] ── 직접(aiortc) 또는 비상 중계(websocket) ──▶ [현장 PC: AuroraClient --wan-agent]
          │                                                              │  127.0.0.1 전용 포트
          └──────────────── 안내 서버(AuroraRelay) ◀── 상시 연결 ────────┘   └▶ 기존 CommandHandler
```

| 파일 | 역할 |
|---|---|
| `wan/config.py` | `wan_config.json`(LAN 설정과 별개) · 현장 고유 ID · 화질 상수 한 곳 |
| `wan/protocol.py` · `mux.py` | 요청/응답을 16KB 조각으로 싣는 메시지 형식, 요청 다중화, 우선순위(입력>화면>파일), 흐름 제어(ACK 창) |
| `wan/links.py` | 통로 두 종류: `DirectLink`(aiortc 데이터 채널) · `RelayLink`(websocket). aiortc는 필요할 때만 불러옴 |
| `wan/bridge.py` | 받은 요청을 로컬 서버로 넘김. 허용 경로만 통과, 화질 상한 적용, LAN 키를 현장에서 붙임 |
| `wan/server.py` | 안내 서버: 현장 목록·설정 보관/전달·연결 정보 중개·비상 중계·새 버전 배포 |
| `wan/agent.py` | 현장 에이전트: 상시 연결·재접속·접속 코드 확인·ID 중복 처리 |
| `wan/client.py` | 사용자 쪽: 목록·설정 보내기·연결(직접→중계)·끊기면 자동 재연결 |
| `wan/supervisor.py` · `runtime.py` | 에이전트 프로세스 감독 · AuroraClient와의 단일 접점 |
| `wan/ui.py` · `viewer.py` | "WAN 원격" 탭 · `WanRemoteViewer`(항상 절약으로 시작, 사용량 표시) |
| `wan/update.py` | 안내 서버에서 새 버전 내려받기(SHA-256 검증) |

수정한 기존 파일: `aurora_client.py`(연결점만, 아래 3절), `AuroraClient.spec`(hiddenimports), `build.bat`(WAN 패키지 선택 설치), `.gitignore`.

## 3. LAN/WAN 격리 — 구현 위치

| 번질 수 있는 곳 | 구현 |
|---|---|
| aiortc를 못 불러오면 시작 단계에서 죽음 | `wan/` 안의 aiortc import는 모두 함수 안. 못 불러오면 비상 중계만 사용. `aurora_client.py`는 모듈 맨 위에서 `wan`을 import하지 않음 |
| 설정 파일 공유 · 깨짐 | WAN은 `wan_config.json`만 씀. 깨지면 `.bad`로 치우고 기본값 (LAN 설정 무관, 테스트로 확인) |
| 9001 서버 · 스레드 공유 | 에이전트가 `127.0.0.1` 임의 포트에 `CommandHandler`를 하나 더 띄움 (`_run_wan_agent`). LAN `listen_port`와 무관 |
| WAN이 LAN을 건드림 | `bridge.ALLOWED` 허용 목록: `/command` · `/discover` · `via=` · 그 밖의 경로 모두 403 |
| LAN 장애가 WAN을 막음 | WAN은 `server_ips` · `export_relay` · `/status` 루프를 쓰지 않음. LAN 로컬 서버가 죽어도 WAN은 502 응답 후 계속 |
| C 라이브러리 비정상 종료 · 메모리 폭주 | 현장 에이전트는 **별도 프로세스**(`supervisor.py`). 부모가 끝나면 같이 끝남, 멈추면 `wan_agent.alive` 로 감지해 재시작 |
| 공용 코드(`RemoteViewer`) 수정 | 수정 없음. `make_viewer_class`로 파생 클래스를 만들어 `aurora_remote.py`는 그대로 |
| WAN 탭 생성 실패 | `_build_wan_tab` try/except — 실패해도 다른 탭 정상, 안내 문구 표시 |
| 시작/종료 | `_wan_start` · `_wan_shutdown` try/except. 기본값 `enabled=False`, 안내 서버 주소가 없으면 아무것도 시작하지 않음 |

`aurora_client.py`에서 바뀐 곳: 탭 도움말/제목 1개 추가, `_build_wan_tab`·`_wan_*` 메서드, `_WanTransferPane`, `_run_wan_agent`, `main()` 앞부분의 `--wan-agent` 분기, 종료 시 `_wan_shutdown`.
**알려진 결합 하나**: 재사용한 `_TransferPane._export_start`가 반출 저장 폴더를 `client_config.json`에 저장한다(LAN 원격 창도 똑같이 하는 기존 동작).

## 4. 단계 현황

| 단계 | 내용 | 상태 |
|---|---|---|
| 0 | 홀 펀칭 시험 (`tests/holepunch_test.py`) | 스크립트 완료. **서로 다른 망의 두 PC 실측은 아직** |
| 1 | 구조 · 설정 파일 · 현장 고유 ID | 완료 (`wan/`, `wan_config.json`, 처음 실행 때 ID 생성) |
| 2 | 안내 서버 | 완료 (현장 목록, 설정 보관·전달, 키, `/health`, 서버 재시작 후에도 유지) |
| 3 | 직접 연결 + 비상 중계 | 완료 (6초 안에 직접이 안 되면 중계, 끊기면 자동 재연결, 접속 코드는 현장이 확인) |
| 4 | 데이터 절약 | 화질 3단계 · 항상 절약으로 시작 · 연결별 사용량/속도 표시 · 날짜별 누적 기록 · **화면 선요청("빠르게" 옵션, 아래)** 완료. 화질별 실사용량 측정은 Windows 실기 필요 |
| 5 | 업데이트 · 설치 · 설명서 | 완료 (안내 서버 경유 새 버전 + SHA-256, `tools/make_wan_config.py`, `WanRelay.spec`, 사용 설명서) |

### 화면 선요청("빠르게") — 구현과 대가
`/remote/frame`의 바뀐 영역은 보는 쪽이 보낸 `last` 번호 이후 **누적**으로 계산된다(직전에 보낸 화면 기준이 아님). 그래서 요청이 겹쳐도 화면이 깨지지 않게 `WanRemoteViewer._apply`가 다음 규칙을 쓴다:
- 이미 더 새 화면이 있으면(응답의 `seq` ≤ 내 번호) 늦게 온 응답은 버린다.
- 조각의 기준 번호(`base`)가 내 번호보다 오래됐어도 붙인다 — 바뀌지 않은 칸은 같은 내용이므로.
- 기준 화면이 모자라면 통째로 다시 받는다. 화면 종류·크기·화질이 바뀌면 이전 요청의 응답은 버린다.

**대가**: 겹친 요청이 같은 변화를 두 번 받아서 초당 화면 수가 2배가 되는 만큼 **데이터도 약 1.9배**가 된다(시뮬레이션: RTT 300ms, 계속 바뀌는 화면 3.0→5.7장/초, 24.8→46.3KB/s). 핫스팟 데이터 절약이 1순위이므로 **기본은 꺼짐**이고 원격 창의 "빠르게" 체크박스로 켠다. "절약" 화질은 켜도 한 장씩. 값은 `wan/config.py`의 `PREFETCH`.

## 5. 검증 현황

`python -m pytest` → 전체 통과 (Python 3.12 + 진짜 Tk(Xvfb) 289개, tkinter 없는 환경 264개 + 화면 시험 3개 파일 건너뜀). 테스트 파일별:

| 파일 | 내용 |
|---|---|
| `test_wan_basic` | 설정(깨진 파일·알 수 없는 키·LAN 설정 불간섭) · 사용량 · 메시지 조각 |
| `test_wan_mux` | 응답 모양 · 차단 · 대용량 · 이어받기 · 업로드 · 우선순위 · 흐름 제어 · 끊김/시간 초과 · 사용량 일치 |
| `test_wan_e2e` | 실제 안내 서버 + 현장 에이전트 + 사용자 + **실제 aiortc/websockets**: 접속 코드 · 잠금 · 설정 전달 · 직접/중계 · 막힌 망 · 재연결 · 서버 재시작 · 키 · ID 중복 · LAN 로컬 서버 장애 |
| `test_wan_real_handler` | **실제 `aurora_client.CommandHandler`**를 WAN 통로로 (파일 목록/다운로드/이어받기/업로드/경로 제한/차단) |
| `test_wan_supervisor` · `update` · `runtime` · `tools` | 프로세스 감독 · 새 버전 다운로드/검증 · 런타임 · 설정 파일 도구 |
| `test_wan_ui` · `ui_strict` | UI 로직 + 엄격한 가짜 tkinter(pack/grid 혼용·오타 옵션 검출) |
| `test_wan_realtk` | **진짜 tkinter(Xvfb)** 로 실제 `AuroraClientApp` 전체를 띄워 WAN 탭 구성·창 안 배치·목록 표시 확인 |
| `test_wan_viewer_real` | **진짜 `RemoteViewer`** + 가짜 현장 화면(`sim_host.py`, 지연·지터): 모든 화질에서 화면이 현장과 같아지는지, 응답 뒤바뀜, 화질 전환, 크기 변경, 선요청 효과 |
| `test_wan_full_chain` | **끝에서 끝까지**: 실제 앱의 `_wan_open_remote` → 진짜 `RemoteViewer` + 진짜 `CommandHandler` + 안내 서버 + 현장 에이전트 + aiortc. 화면 동기화 · 입력 전달 · 반입/반출 패널 요청 · 닫기 · 중복 열기 |
| `tools/wan_frozen_check/` | PyInstaller 번들 점검: `AuroraClient.spec`의 제외 목록·hiddenimports 로 묶은 실행 파일 안에서 직접·중계 연결이 되는지 (Linux 확인 완료, **Windows 빌드 PC에서 1회 실행 권장**) |

측정(이 환경, 루프백): 60MB 전송 중 입력 왕복 — 직접 연결 중앙 80ms / 최대 393ms, 비상 중계 중앙 10ms / 최대 15ms (전송 중이 아닐 때 약 3~4ms).

Linux 에서 Windows 전용 부분을 `tests/winshim.py` 로 흡수해 **실제 `aurora_client.py` · `aurora_remote.py` 코드**를 시험한다. tkinter 가 설치된 환경에서는 `xvfb-run -a python -m pytest` 로 전체(화면 시험 포함)를 돌린다. tkinter 가 없으면 화면 시험은 건너뛴다.

### 확인하지 못한 것 (Windows 실기 · 실제 인터넷 필요)
1. **서로 다른 망에서 실제 홀 펀칭** — 이 환경은 외부 UDP 불가(`host` 후보만 확인). `tests/holepunch_test.py` 를 두 PC에서 돌린 결과가 필요
2. Windows 에서의 화면 동작 (DPI · 제목 표시줄 제거 · 키보드 훅 · 파일 끌어놓기 등). Linux 의 Tk 로 구성과 흐름은 확인했지만 Windows 전용 호출은 흡수해서 시험
3. **Windows 에서의 PyInstaller 빌드** — Linux 에서 같은 `EXCLUDES`·`hiddenimports` 로 묶은 실행 파일이 직접 연결·중계 모두 동작함은 확인
4. 현장 PC에서 LAN 프로세스와 WAN 에이전트가 동시에 화면 캡처(DXGI)할 때의 충돌 여부
5. 화질별 실제 데이터 사용량

확인된 것: Python 3.14 용 Windows(win_amd64) 휠이 `aiortc`·`av`·`pylibsrtp`·`cryptography`·`google-crc32c`·`websockets`·`cffi` 등 의존성 전부에 존재한다(조회 결과).

### 알려진 사항
- **exe 크기**: WAN 스택(aiortc·`av`(FFmpeg) 등)이 exe에 **약 53MB**를 더한다(같은 제외 목록으로 빈 프로그램 7.1MB → 59.9MB, Linux 빌드 측정; Windows 휠 압축 해제 합계 약 85MB). 기존 `AuroraClient.exe`가 약 64MB이므로 100MB를 넘길 것으로 예상(공정망 업데이트 복사량 증가). aiortc 없이 빌드하면 비상 중계만 동작.
- **직접 연결 전송 속도**: aiortc의 SCTP가 순수 파이썬이라 이 환경에서 약 3.5MB/s(≈28Mbps)에서 CPU가 병목이다. 같은 조건에서 비상 중계는 26MB/s. 화면 원격에는 충분하지만 큰 파일은 중계가 더 빠를 수 있다.
- 통신은 `ws://`(암호화 없음) + 공용 키 + 접속 코드. 안내 서버는 접속 코드를 평문으로 보관한다. 필요하면 TLS 프록시(`wss://`).

## 6. 의도된 설계 (바꾸지 않음)

- 배포 계정 `user`/`1`, 반출 키 `aurora-export` 등 기본값
- `D:\RemoteConfig`, `D:\RemoteVersion` 고정 경로와 `build.bat`의 Python 경로
- `dist/`에 버전별 exe를 git으로 보관하는 방식, pre-commit 자동 빌드 훅
- 서버·클라이언트가 파일 하나씩으로 독립된 구조

## 7. 보류

- LAN 보안 강화: `/command`·`/status` 인증, `setpath` 검증, 업데이트 해시 확인 (보류 결정. WAN 새 버전만 SHA-256 검증)
- 공용 코드 분리, `aurora_client.py` 분할, `Claude/CLAUDE.md` 갱신
