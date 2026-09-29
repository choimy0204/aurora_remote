@echo off
rem Aurora remote deploy - copies AuroraRemote + RemoteConfig to each PC via \\IP\D
rem   server PCs (client_config.json server_ips) : AuroraServer + AuroraClient
rem   other PCs  (server_config.json client_ips) : AuroraClient
rem usage: deploy.bat [IP ...] [--dry] [--overwrite-config] [-y]
rem NOTE: keep this file ASCII only - non-ASCII lines break after chcp 65001
setlocal
set "PY=C:\Users\sbico\AppData\Local\Python\bin\python.exe"
set "ROOT=%~dp0"
chcp 65001 > nul
"%PY%" "%ROOT%tools\deploy_remote.py" %*
set RC=%errorlevel%
pause
exit /b %RC%
