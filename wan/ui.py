"""
"WAN 원격" 탭 — 이 파일은 옛 이름(wan.ui)을 그대로 쓸 수 있게 모아 주는 입구
=============================================================================
화면 구성: ① 현장 PC 목록(중계 PC 를 거치는 현장 + 주소로 접속했던 현장) ② 이 PC 를 원격 대상으로 쓰기(주소 · 접근 코드)
③ 접어 둔 "중계 PC 로 사용" ④ 사용량 한 줄 · 로그.  연결 정보를 넣는 곳은 "연결 추가" 창 하나다.
판단(검사 · 표시 글자 · 스레드 시작)은 tkinter 를 쓰지 않는 컨트롤러에, 화면은 얇은 위젯 층에 있다.

    ui_base        상수 · 순수 함수 · 화면 갱신 함수 모음(NullUI)
    ui_controller  WanTabController 의 뼈대 (아래 세 조각을 합친 클래스)
      ui_relay       이 PC 에서 안내 서버 운영 · 접속 코드 새로고침 · 사내 Git
      ui_field       접속 설정 파일 · 연결 추가 · 이 PC 를 원격 대상으로 쓰기
      ui_targets     현장 PC 목록 · 접속 · 이름 바꾸기/삭제 · 설정 보내기 · 사용량
    ui_tab         WanTab(위젯) · ConnectDialog · build_tab

ctx (aurora_client 가 넘겨 줌) — 항목 설명은 ui_controller.py 맨 위
스레드 규칙: 오래 걸리는 일(연결 · 설정 보내기 · 재시작)은 작업 스레드에서, 화면 갱신은 schedule() 로 화면 스레드에서
tkinter 는 build_tab() 안에서 불러오므로 이 파일은 tkinter 가 없는 환경에서도 import 된다.
"""

import os      # noqa: F401  (시험이 wan.ui.os 를 바꿔 쓴다)
import socket  # noqa: F401

from .ui_base import (CODE_MAX, COLUMN_WIDTHS, COLUMNS, DIRECT_ID, FAST_MS, FILE_CHOICES, KEEP, LOG_LINES,  # noqa: F401
                      NAME_MAX, NullUI, PROBE_TIMEOUT, PUBLIC_RETRY, SLOW_MS, USAGE_HINT, classify_target,
                      is_direct_id, level_for_state, relative_time, validate_code, validate_port, validate_url)
from .ui_controller import WanTabController  # noqa: F401
from .ui_tab import ConnectDialog, WanTab, build_tab  # noqa: F401
