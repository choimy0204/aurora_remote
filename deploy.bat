@echo off
chcp 65001 > nul
setlocal
set "PY=C:\Users\sbico\AppData\Local\Python\bin\python.exe"
set "ROOT=%~dp0"

rem 각 PC 의 \\IP\D 에 설치/갱신
rem   서버 PC(client_config.json 의 server_ips) → AuroraServer + AuroraClient
rem   나머지(server_config.json 의 client_ips)  → AuroraClient
rem   D:\AuroraRemote   실행 파일
rem   D:\RemoteConfig   설정 (이미 있으면 보존) + Update (자동 업데이트용)
rem
rem   deploy.bat                     전체
rem   deploy.bat 192.168.110.21      지정한 PC 만
rem   deploy.bat --dry               계획만 보기
rem   deploy.bat --overwrite-config  설정 json 도 덮어쓰기
"%PY%" "%ROOT%tools\deploy_remote.py" %*
set RC=%errorlevel%
pause
exit /b %RC%
