@echo off
chcp 65001 >nul
title stop web-scrcpy
echo 正在停止所有投屏进程...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -match 'app.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo 已全部停止。
timeout /t 2 >nul