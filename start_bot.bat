@echo off
chcp 65001 >nul
title YouTube Comment Replier
cd /d "%~dp0"
call .venv\Scripts\python.exe youtube_bot.py --wizard
if %errorlevel% neq 0 pause
