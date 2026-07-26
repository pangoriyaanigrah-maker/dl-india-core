@echo off
rem DL India Core dashboard launcher: pull latest prices, serve, open browser.
cd /d "%~dp0"
git pull --ff-only --quiet
start "" /min python serve.py
rem give the server a moment before the browser asks for the page
timeout /t 1 >nul
start "" http://localhost:8765
