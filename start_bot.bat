@echo off
chcp 65001 >nul
title YouTube Comment Replier
cd /d "%~dp0"

rem Auto-update: fast-forward master from GitHub before launch. No git, no network, local edits or another branch: skipped.
set "GIT_SSH_COMMAND=ssh -o BatchMode=yes -o ConnectTimeout=10"
for /f "delims=" %%b in ('git branch --show-current 2^>nul') do set "BRANCH=%%b"
if "%BRANCH%"=="master" (
    git pull --ff-only github master || echo [!] Обновление пропущено, запускаю текущую версию
)

call .venv\Scripts\python.exe youtube_bot.py --wizard
if %errorlevel% neq 0 pause
