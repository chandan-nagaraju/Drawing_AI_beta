@echo off
REM Interactive Semantic Extraction Viewer — start API + UI
cd /d "%~dp0"
start "Drawing AI API" cmd /k "cd /d %~dp0backend && ..\.venv\Scripts\python.exe -m uvicorn api_server:app --reload --port 8000"
timeout /t 2 /nobreak >nul
cd frontend
start "Drawing AI Viewer" cmd /k "npm run dev"
echo.
echo API:    http://127.0.0.1:8000
echo Viewer: http://127.0.0.1:5173
echo Upload XFG00144.pdf and drag over dimension clusters.
