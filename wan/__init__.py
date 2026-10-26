"""
Aurora WAN 원격 — 인터넷을 건너 현장 PC 를 원격으로 보고 조작하는 기능
=====================================================================
기존 LAN 원격(반입/반출 탭)과는 코드·설정·포트·프로세스가 완전히 분리되어 있다.
이 패키지는 aurora_client / aurora_server 를 import 하지 않는다 (LAN 쪽 장애가 번지지 않게).

    config      WAN 설정 파일 (client_config.json 과 별개) · 현장 고유 ID · 화질 상수
    usage       날짜별 데이터 사용량 기록
    protocol    요청/응답을 16KB 조각으로 나눠 싣는 메시지 형식
    mux         한 통로에 여러 요청을 동시에 실어 나르는 계층 (Channel · Bridge)
    links       통로 세 종류 — 직접 연결(aiortc 데이터 채널) · 비상 중계(안내 서버 websocket) · 주소 접속(websocket 한 줄)
    server      안내 서버 (화면 없음)
    agent       현장 PC 의 WAN 에이전트 (안내 서버 상시 연결 · 주소 접속 받기 · 접근 코드 확인 · 요청 받기)
    supervisor  에이전트를 별도 프로세스로 띄우고 감시
    client      사용자 PC 쪽 (현장 목록 · 연결 · 설정 보내기)
    direct      안내 서버 없이 주소 + 접근 코드로 현장 PC 에 바로 접속
    connect_code / connfile   접속 정보 글자(AW1-·AD1-) 와 접속 설정 파일(aurora_wan.conn)
    update      안내 서버에서 새 버전 내려받기
    ui          "WAN 원격" 탭
"""

WAN_VERSION = "1.1"
