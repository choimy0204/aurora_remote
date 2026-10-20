@echo off
chcp 65001 > nul
setlocal
set "PY=C:\Users\sbico\AppData\Local\Python\bin\python.exe"
set "ROOT=%~dp0"

rem 병렬 빌드용 하위 호출: build.bat __build <spec이름> <작업폴더>
if "%~1"=="__build" goto :build

rem 패키지가 없을 때만 설치 (매번 pip 확인하던 시간 절약)
"%PY%" -c "import requests, psutil, pystray, PIL, PyInstaller" >nul 2>&1 || (
    echo Installing required packages...
    "%PY%" -m pip install requests psutil pystray pillow pyinstaller --quiet
)

rem WAN remote packages (optional) - bundled only when installed; the build works without them
"%PY%" -c "import aiortc, websockets" >nul 2>&1 || (
    echo Installing WAN packages - optional...
    "%PY%" -m pip install aiortc websockets --quiet
)

rem dist 에서 실행 중인 exe 만 종료 + 버전이 바뀌었으면 dist\latest → dist\old\v이전버전 으로 보관
"%PY%" "%ROOT%tools\release.py" prepare || exit /b 1

del "%ROOT%build_server.done" "%ROOT%build_client.done" 2>nul

rem 서버는 백그라운드, 클라이언트는 현재 창에서 동시에 빌드
rem (변경 없는 쪽은 PyInstaller 캐시 확인만 하고 약 1초 만에 끝남)
echo Building in parallel...
start "" /b cmd /c ""%~f0" __build AuroraServer build_server"
call :build __build AuroraClient build_client

:wait_server
if exist "%ROOT%build_server.done" goto :report
ping -n 1 -w 300 127.0.0.1 >nul
goto :wait_server

:report
set /p RC_S=<"%ROOT%build_server.done"
set /p RC_C=<"%ROOT%build_client.done"
del "%ROOT%build_server.done" "%ROOT%build_client.done" 2>nul
set FAIL=0
if not "%RC_S: =%"=="0" ( echo [FAIL] AuroraServer - log: & type "%ROOT%build_server.log" & set FAIL=1 )
if not "%RC_C: =%"=="0" ( echo [FAIL] AuroraClient - log: & type "%ROOT%build_client.log" & set FAIL=1 )
rem 사용 매뉴얼 + version.json(자동 업데이트 기준)을 최신 폴더에 함께 넣음
if "%FAIL%"=="0" copy /y "%ROOT%Aurora_사용매뉴얼.md" "%ROOT%dist\latest\" >nul
rem deploy 용 AuroraDeploy.exe (Python 없는 PC 에서 실행) - deploy_remote.py 가 바뀌었거나 exe 가 없을 때만
if "%FAIL%"=="0" "%PY%" -c "import os,sys;e=r'%ROOT%dist\latest\AuroraDeploy.exe';s=r'%ROOT%tools\deploy_remote.py';sys.exit(0 if os.path.exists(e) and os.path.getmtime(e)>os.path.getmtime(s) else 1)" || (
    echo Building AuroraDeploy...
    "%PY%" -m PyInstaller "%ROOT%tools\deploy_remote.py" --onefile --console --name AuroraDeploy --distpath "%ROOT%dist\latest" --workpath "%ROOT%build_deploy" --specpath "%ROOT%build_deploy" --noconfirm --log-level WARN > "%ROOT%build_deploy.log" 2>&1 || ( echo [FAIL] AuroraDeploy - log: & type "%ROOT%build_deploy.log" & set FAIL=1 )
)
if "%FAIL%"=="0" "%PY%" "%ROOT%tools\release.py" manifest
if "%FAIL%"=="0" echo Done! Check: %ROOT%dist\latest\  (이 폴더 내용을 D:\RemoteVersion 에 넣으면 자동 업데이트)
if /i not "%~1"=="nopause" pause
exit /b %FAIL%

:build
"%PY%" -m PyInstaller "%ROOT%%2.spec" --distpath "%ROOT%dist\latest" --workpath "%ROOT%%3" --noconfirm --log-level WARN > "%ROOT%%3.log" 2>&1
rem 성공·실패 모두 종료 코드를 기록 (이전에는 실패 시 대기 루프가 끝나지 않았음)
> "%ROOT%%3.done" echo %errorlevel%
exit /b 0
