@echo off
setlocal
cd /d "%~dp0"
python FINAL2026.py
if errorlevel 1 pause
