@echo off
REM Claude Office 실행 (Windows) - Python 3.8+ 필요 (python.org 또는 Microsoft Store에서 설치)
cd /d "%~dp0"
start "" "http://localhost:8765"
python server.py %*
if errorlevel 1 py -3 server.py %*
pause
