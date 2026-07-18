@echo off
chcp 65001 >nul
title web-scrcpy multi-launcher
cd /d "%~dp0"
python start_multi.py %*
echo.
pause