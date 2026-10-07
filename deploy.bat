@echo off
rem Aurora remote deploy - copies to RemoteConfig\Update + RemoteVersion on each PC via \\IP\D
rem   server PCs (client_config.json server_ips) : AuroraServer + AuroraClient
rem   other PCs  (server_config.json client_ips) : AuroraClient
rem usage: deploy.bat [IP ...] [--dry] [--overwrite-config] [-y]
rem Uses dist\latest\AuroraDeploy.exe (no Python needed); falls back to Python.
rem NOTE: keep this file ASCII only - non-ASCII lines break after chcp 65001
setlocal
set "ROOT=%~dp0"
set "EXE=%ROOT%dist\latest\AuroraDeploy.exe"
if exist "%EXE%" (
    "%EXE%" %*
    exit /b
)
set "PY=C:\Users\sbico\AppData\Local\Python\bin\python.exe"
if not exist "%PY%" set "PY=python"
chcp 65001 > nul
"%PY%" "%ROOT%tools\deploy_remote.py" %*
set RC=%errorlevel%
pause
exit /b %RC%
