@echo off
rem ============================================================
rem  Aurora WAN relay (guide server) - start example
rem
rem  Copy this file to the relay PC (for example D:\AuroraRelay\run_relay.bat)
rem  next to AuroraRelay.exe, then edit the settings below.
rem
rem  1. Change CHANGE_ME to a real shared key (use a long random
rem     string). The same key must be put in wan_config.json on every
rem     field PC and engineer PC (relay_key). Keep it private.
rem  2. Open TCP port 9100 (or your --port) in the firewall of this
rem     PC. Field PCs and engineer PCs do not need any open port.
rem  3. To run it all the time, register this file in Task Scheduler:
rem       - Create Task (not "Create Basic Task")
rem       - General: "Run whether user is logged on or not"
rem       - Triggers: New > "At startup"
rem       - Actions: New > Start a program > this .bat file
rem       - Settings: "If the task fails, restart every" 1 minute
rem  4. Check it works: open https://<relay PC IP>:9100/health in a
rem     browser (the certificate is self-signed, so the browser warns;
rem     continue anyway). The page should show "ok".
rem  5. To keep a log file, add  >> D:\AuroraRelay\relay.log 2>&1
rem     at the end of the AuroraRelay.exe line below.
rem ============================================================

setlocal
set "RELAY_EXE=D:\AuroraRelay\AuroraRelay.exe"
set "RELAY_DATA=D:\AuroraRelay"
set "RELAY_UPDATE=D:\AuroraRelay\update"
set "RELAY_PORT=9100"
set "RELAY_KEY=CHANGE_ME"

if not exist "%RELAY_EXE%" (
    echo AuroraRelay.exe not found: %RELAY_EXE%
    pause
    exit /b 1
)

rem --update-dir: put AuroraClient.exe and version.json (from dist\latest) here
rem --tls: encrypt everything (wss://). The certificate is created in RELAY_DATA on the first run and its
rem        fingerprint is printed; clients remember it on first connection.
"%RELAY_EXE%" --port %RELAY_PORT% --data %RELAY_DATA% --key %RELAY_KEY% --update-dir %RELAY_UPDATE% --tls
if errorlevel 1 (
    echo AuroraRelay stopped with an error.
    pause
)
endlocal
