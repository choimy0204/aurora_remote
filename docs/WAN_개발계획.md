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
| 0 | 홀 펀칭 시험 (`tests/holepunch_test.py`) | 스크립트 완료. **NAT 실험실(네트워크 네임스페이스)에서 서로 다른 NAT 두 개를 사이에 두고 성공 확인** (아래 5절). 실제 인터넷(공유기·통신사 장비)에서의 실측은 아직 |
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
| `test_wan_natlab` · `tools/nat_lab/` | **NAT 실험실**: 네트워크 네임스페이스 + iptables 로 "NAT A 뒤의 현장 ↔ 인터넷(안내 서버·STUN) ↔ NAT B 뒤의 사용자"를 만들어 실제 WAN 스택과 0단계 스크립트를 실행 (root 권한 Linux 에서만, 아니면 건너뜀) |
| `tools/wine_lab/` | **Wine 위의 Windows 용 Python(+tkinter)** 으로 같은 시험을 가짜(winshim) 없이 실행 — 실제 `winreg` · `ctypes.windll` · user32 · Tk · Windows 용 aiortc/psutil/pillow 휠 |
| `test_wan_host_win` | **진짜 `RemoteHost`**(Windows API): 화면 캡처(실제 창의 색 확인, 바뀐 조각의 기준 번호, 변화 없음 응답)와 `SendInput`(마우스 좌표 정확도, 키 입력이 입력칸에 도착, 중복 요청 번호 무시). `AURORA_HOST_TESTS=1` / `AURORA_HOST_INPUT=1` 로 켬 (입력 주입은 실제 마우스를 움직이므로 기본 꺼짐) |
| `test_wan_full_chain_win` | 가짜 화면 대신 진짜 `RemoteHost` 를 WAN 경로 끝에 둔 **끝에서 끝까지**: 보는 쪽에서 누른 키가 WAN 직접 연결을 거쳐 현장의 입력칸에 도착, 마우스 이동, 실제 화면이 뷰어에 도착 |

측정(이 환경, 루프백): 60MB 전송 중 입력 왕복 — 직접 연결 중앙 80ms / 최대 393ms, 비상 중계 중앙 10ms / 최대 15ms (전송 중이 아닐 때 약 3~4ms).

Linux 에서 Windows 전용 부분을 `tests/winshim.py` 로 흡수해 **실제 `aurora_client.py` · `aurora_remote.py` 코드**를 시험한다. tkinter 가 설치된 환경에서는 `xvfb-run -a python -m pytest` 로 전체(화면 시험 포함)를 돌린다. tkinter 가 없으면 화면 시험은 건너뛴다.

### NAT 실험실 결과 (이 환경, 에뮬레이션)
서로 다른 NAT 뒤의 두 망을 리눅스 네임스페이스로 만들고(WAN 쪽 INPUT 차단 등 실제 공유기처럼 설정), 안내 서버·STUN은 "인터넷" 쪽에 두었다.

| 시나리오 (NAT A / NAT B) | 결과 |
|---|---|
| cone / cone | **직접 연결**, 0.8초, 후보 `host ↔ srflx(상대 NAT 공인 주소)` = 홀 펀칭, 왕복 5ms, 30MB 다운로드 약 20MB/s |
| cone / symmetric | 직접 연결 실패 → **8초 뒤 비상 중계로 전환**, 전송 정상 |
| symmetric / symmetric | 홀 펀칭 불가 → **비상 중계로 전환**, 전송 정상 |
| UDP 차단 (STUN 외) | **비상 중계로 전환**, 전송 정상 |
| 공유기 재부팅(NAT 매핑 전부 소실) | 4초 만에 직접 연결 상태로 **자동 복구** |
| 0단계 스크립트 (cone / cone) | srflx 로 연결 성공, 왕복 1.4ms, 109/114Mbps |

이 실험으로 찾아 고친 것: ① 직접 연결이 연결 검사(ICE) 단계에서 실패해도 "응답이 없습니다"로 표시되던 오류 메시지(NAT·방화벽 문제를 가렸음), ② 현장 에이전트·사용자 쪽에 직접 연결 진단 로그(후보 개수·단계별 시간) 추가. 실험실 자체의 교훈: WAN 쪽에서 들어온 미요청 패킷을 INPUT 에서 막지 않는 NAT 는 연결 추적 충돌로 홀 펀칭이 깨진다(실제 공유기는 대부분 막음).
**이 결과는 에뮬레이션이다.** 실제 통신사 CGNAT·기업 방화벽·공유기마다 동작이 다르므로 실제 인터넷에서의 확인은 여전히 필요하다.

### Windows 구현(Wine) 검증
`tools/wine_lab/setup.sh` 가 Wine 위에 Windows 용 Python 3.12 + tkinter + 프로젝트 의존성(Windows 휠)을 만든다. 전체 테스트를 가짜 없이 실제 Windows API 구현으로 실행한 결과 **288개 통과, 실패 0** (건너뜀 6: NAT 실험실 5개는 Linux 전용, SIGTERM 시험 1개는 POSIX 전용). 여기에 Windows 전용 시험이 추가로 있다:
- 실제 앱 구성, 진짜 `RemoteViewer`, 끝에서 끝까지(앱→뷰어→CommandHandler→안내 서버→현장→aiortc)
- **진짜 `RemoteHost`**: Wine 에는 DXGI 화면 복제가 없어 **GDI 호환 캡처 경로**가 쓰였고(이 경로가 `meta["gdi"]`로 올바르게 표시됨), 실제 창의 색이 캡처되고 바뀐 화면은 기준 번호를 가진 조각으로 오며, 마우스 포인터가 요청한 좌표에 놓이고, `SendInput` 으로 보낸 키가 입력칸에 도착한다. 보는 쪽에서 누른 키가 WAN 직접 연결을 거쳐 현장 입력칸에 "abc" 로 들어가는 것까지 확인.
**실제 빌드된 `AuroraClient.exe` (Wine 에서 `AuroraClient.spec` 으로 PyInstaller 빌드, 약 47 MB, 경고 없음)**: 현장 모드 설정(`D:\RemoteConfig\wan_config.json`)만 두고 실행하면 `--wan-agent` 자식이 떠서 중계 서버에 등록되고(ID·버전 표시), 사용자 쪽 `WanClient` 가 접속 코드로 **직접 연결**을 맺어 `/info`, `/fs/roots`, `/remote/frame`(exe 안의 실제 RemoteHost, JPEG 수신)이 응답했다. `/command` 는 403 으로 막히고, 틀린 접속 코드는 거부되며(`WanDenied`), 종료 후 exe 프로세스가 남지 않았다. (검증 스크립트가 exe 출력을 파일로 돌리면 Wine 의 Windows 프로그램이 시작되지 않는다 — 파이프로 받아야 한다. 제품 문제가 아니라 시험 도구 문제였다.) 참고: Wine 에서 기존 LAN 코드의 `psutil.process_iter` 가 한 번 `OSError 87` 을 내는 것이 보이는데, 상태 보고 스레드 안에서만 나는 Wine 한정 증상이며 이번 변경과 무관하다.
시험 도구의 Wine 관련 주의(Tcl 의 chdir 직후 Tk 생성 실패, 닫힌 뷰어의 PhotoImage 해제 순서, UTF-8 로케일, 자식 프로세스 시작이 느려 생기는 시험 경쟁 조건)는 `tests/winshim.py` 와 해당 시험에 반영했다.
**Wine 은 Windows 가 아니다.** DXGI 캡처(Windows 8+ 의 화면 복제)와 보안 데스크톱(잠금 화면·UAC)·실제 DPI 배율·실제 키보드 훅은 대상이 아니다.

### 확인하지 못한 것
1. **실제 인터넷에서의 홀 펀칭** (위는 에뮬레이션)
2. **진짜 Windows 에서의 동작** — Wine 은 Windows API 구현이지만 Windows 자체는 아니다. 특히 **DXGI 화면 캡처**(Wine 에서는 GDI 경로만 시험됨)·보안 데스크톱·실제 DPI 배율·키보드 훅
3. **Windows 에서의 PyInstaller 빌드** — Linux 에서 같은 `EXCLUDES`·`hiddenimports` 로 묶은 실행 파일이 직접·중계 연결 모두 동작함은 확인
4. 현장 PC에서 LAN 프로세스와 WAN 에이전트가 동시에 화면 캡처(DXGI)할 때의 충돌 여부
5. 화질별 실제 데이터 사용량

확인된 것: Python 3.14 용 Windows(win_amd64) 휠이 `aiortc`·`av`·`pylibsrtp`·`cryptography`·`google-crc32c`·`websockets`·`cffi` 등 의존성 전부에 존재한다(조회 결과).

### 알려진 사항
- **exe 크기**: WAN 스택(aiortc·`av`(FFmpeg) 등)이 exe에 **약 53MB**를 더한다(같은 제외 목록으로 빈 프로그램 7.1MB → 59.9MB, Linux 빌드 측정; Windows 휠 압축 해제 합계 약 85MB). 기존 `AuroraClient.exe`가 약 64MB이므로 100MB를 넘길 것으로 예상(공정망 업데이트 복사량 증가). aiortc 없이 빌드하면 비상 중계만 동작.
- **직접 연결 전송 속도**: aiortc의 SCTP가 순수 파이썬이라 이 환경에서 약 3.5MB/s(≈28Mbps)에서 CPU가 병목이다. 같은 조건에서 비상 중계는 26MB/s. 화면 원격에는 충분하지만 큰 파일은 중계가 더 빠를 수 있다.
- 통신은 `ws://`(암호화 없음) + 공용 키 + 접속 코드. 안내 서버는 접속 코드를 평문으로 보관한다. 필요하면 TLS 프록시(`wss://`).

### 주소로 바로 접속 · 설정 파일 · 화면 정리 (v1.1)

- **주소 접속(중계 PC 없이)**: 현장 에이전트가 `direct_port`(기본 9102)에서 websocket 을 받는다(`agent.run_direct`). 사용자는 `wan.direct.DirectClient` 로 `주소:포트` + 접근 코드를 보내고, 접근 코드는 중계 방식과 **같은 `check_code`**(5회 실패 → 60초 잠금, 공용 키 대체)로 현장 PC 가 직접 확인한다. 통로는 `WsLink`(세션 표시 없는 websocket 한 줄)이고 그 위에 기존 `Mux`·`Bridge`(허용 목록)가 그대로 올라가므로 원격 창·반입/반출은 방식과 무관하게 동일. 사용자 쪽은 안내 서버 클라이언트(`WanClient`)와 **별도 스레드·루프**(`DirectClient`)라 서로 영향이 없다.
- **포트 변경·접근 코드 변경 즉시 반영**: 화면(다른 프로세스)이 `wan_config.json` 을 고치면 에이전트가 파일 시각(`reload_if_changed`)으로 알아채 포트를 다시 열고 새 코드를 쓴다. 포트가 다른 프로그램에 쓰이면 `busy` 로 표시하고 30초마다 재시도(에이전트·중계 연결은 영향 없음). 상태는 `wan_agent.alive`(JSON)로 화면에 전달.
- **공인 주소 확인**: STUN(UDP 한 번)으로 인터넷에서 보이는 IP 를 알아내 주소 옆에 표시(`netutil.public_address`). 못 알아내도 기능에는 영향 없음.
- **접속 정보 글자**: `AW1-`(중계 PC) 외에 `AD1-`(현장 PC 하나: 주소들 + 접근 코드). '연결 추가' 창 한 칸에 `AW1-`·`AD1-`·`ws://…`·`IP[:포트]` 중 무엇을 넣어도 `ui.classify_target` 이 구분한다.
- **설정 파일**: `aurora_wan.conn`(접속 정보 한 줄)을 exe 옆에 두면 실행 때 한 번 자동 적용(`runtime.apply_conn_file`, 내용 지문을 `conn_hash` 로 저장해 같은 파일은 다시 덮어쓰지 않음).
- **화면**: 안내 서버 주소·키 칸, 안내 서버 운영 상시 카드, 설명 문구, 사용량 카드, 목록의 ID·버전·설정 열을 없애고 ① 현장 PC 목록(+연결 추가) ② 이 PC 원격 대상(주소·접근 코드) ③ 접힌 '중계 PC 로 사용' ④ 사용량 한 줄 · 로그로 정리.
- 시험: `tests/test_wan_direct.py`(실제 에이전트 + websockets), `test_wan_direct_ui.py`(컨트롤러 + 실제 에이전트), `test_wan_conn.py`(설정 파일·STUN·형식), 실제 Tk 시험(`test_wan_realtk.py`)에 새 화면 시험 추가. Windows 실기기 확인은 아직 못 했다(방화벽 자동 열기·공인 주소·UAC 흐름).

## 6. 의도된 설계 (바꾸지 않음)

- 배포 계정 `user`/`1`, 반출 키 `aurora-export` 등 기본값
- `D:\RemoteConfig`, `D:\RemoteVersion` 고정 경로와 `build.bat`의 Python 경로
- `dist/`에 버전별 exe를 git으로 보관하는 방식, pre-commit 자동 빌드 훅
- 서버·클라이언트가 파일 하나씩으로 독립된 구조

## 7. 보류

- LAN 보안 강화: `/command`·`/status` 인증, `setpath` 검증, 업데이트 해시 확인 (보류 결정. WAN 새 버전만 SHA-256 검증)
- 공용 코드 분리, `aurora_client.py` 분할, `Claude/CLAUDE.md` 갱신
