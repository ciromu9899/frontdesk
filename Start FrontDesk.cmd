@echo off
title FrontDesk Setup
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 quickstart.py --guided
) else (
  python quickstart.py --guided
)
if not %errorlevel%==0 pause
