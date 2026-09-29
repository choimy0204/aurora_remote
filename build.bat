@echo off
chcp 65001 > nul
setlocal
set "PY=C:\Users\sbico\AppData\Local\Python\bin\python.exe"
set "ROOT=%~dp0"

rem 병렬 빌드용 하위 호출: build.bat __build <spec이름> <작업폴더>
if "%~1"=="__build" goto :build

echo Stopping running processes...
taskkill /f /im AuroraServer.exe >nul 2>&1
taskkill /f /im AuroraClient.exe >nul 2>&1

rem 패키지가 없을 때만 설치 (매번 pip 확인하던 시간 절약)
"%PY%" -c "import requests, psutil, pystray, PIL, PyInstaller" >nul 2>&1 || (
    echo Installing required packages...
    "%PY%" -m pip install requests psutil pystray pillow pyinstaller --quiet
)

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
rem 사용 매뉴얼을 배포 폴더에 함께 넣어 항상 최신으로 유지
if "%FAIL%"=="0" copy /y "%ROOT%Aurora_사용매뉴얼.md" "%ROOT%dist\" >nul
if "%FAIL%"=="0" echo Done! Check: %ROOT%dist\
if /i not "%~1"=="nopause" pause
exit /b %FAIL%

:build
"%PY%" -m PyInstaller "%ROOT%%2.spec" --distpath "%ROOT%dist" --workpath "%ROOT%%3" --noconfirm --log-level WARN > "%ROOT%%3.log" 2>&1
rem 성공·실패 모두 종료 코드를 기록 (이전에는 실패 시 대기 루프가 끝나지 않았음)
> "%ROOT%%3.done" echo %errorlevel%
exit /b 0
