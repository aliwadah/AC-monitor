@echo off
rem Launcher for the local Solar AC control server.
rem Starts server.py (hidden) in the background and logs output to this folder.
cd /d "%~dp0"
if exist server.log del server.log 2>nul
start "Solar AC Control" /min cmd /c "python server.py > server.log 2>&1"