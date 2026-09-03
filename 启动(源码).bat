@echo off
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
cd /d "%~dp0"
start "" "%PYW%" main.py
