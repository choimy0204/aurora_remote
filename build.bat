@echo off
chcp 65001 > nul

echo Stopping running processes...
taskkill /f /im AuroraServer.exe 2>nul
taskkill /f /im AuroraClient.exe 2>nul
timeout /t 1 /nobreak > nul

echo Installing required packages...
"C:\Users\sbico\AppData\Local\Python\bin\python.exe" -m pip install requests psutil pystray pillow --quiet

del "%~dp0build_server.done" 2>nul
del "%~dp0build_client.done" 2>nul

echo Building in parallel...
start "AuroraServer Build" cmd /c ""C:\Users\sbico\AppData\Local\Python\bin\python.exe" -m PyInstaller "%~dp0AuroraServer.spec" --distpath "%~dp0dist" --workpath "%~dp0build_server" --noconfirm && echo done > "%~dp0build_server.done""
start "AuroraClient Build" cmd /c ""C:\Users\sbico\AppData\Local\Python\bin\python.exe" -m PyInstaller "%~dp0AuroraClient.spec" --distpath "%~dp0dist" --workpath "%~dp0build_client" --noconfirm && echo done > "%~dp0build_client.done""

:wait_loop
timeout /t 2 /nobreak > nul
if not exist "%~dp0build_server.done" goto wait_loop
if not exist "%~dp0build_client.done" goto wait_loop

del "%~dp0build_server.done" 2>nul
del "%~dp0build_client.done" 2>nul

echo Done! Check: %~dp0dist\
pause
