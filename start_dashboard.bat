@echo off
rem DL India Core. Reads settings from .env (DRIVE_FOLDER_ID) and the
rem service-account key in backend\. One server: dashboard and API.
cd /d "%~dp0backend"
start "" /min python -m uvicorn main:app --host 127.0.0.1 --port 8765
rem it enumerates the Drive folder before serving, so give it a moment
timeout /t 8 >nul
start "" http://localhost:8765
