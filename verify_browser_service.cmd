@echo off
setlocal
cd /d "%~dp0"
set "STUDIO_BROWSER_RESULT_FILE=%~dp0docs\2.1.3_真实服务浏览器E2E结果.json"
python verify_browser_service.py
if errorlevel 1 exit /b %errorlevel%
echo Browser E2E result: %STUDIO_BROWSER_RESULT_FILE%
