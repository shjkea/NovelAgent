@echo off
title NovelAgent Console
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\start_web.ps1"
pause
