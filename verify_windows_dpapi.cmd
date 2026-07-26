@echo off
setlocal
cd /d "%~dp0"
set "STUDIO_DPAPI_RESULT_FILE=%~dp0docs\2.1.3_Windows_DPAPI验证结果.json"
python verify_windows_dpapi.py
if errorlevel 1 exit /b %errorlevel%
echo DPAPI validation result: %STUDIO_DPAPI_RESULT_FILE%
