@echo off
setlocal
set "HQIN_HOME=%~dp0.."
if "%~1"=="" (
  "%HQIN_HOME%\.venv\Scripts\python.exe" "%HQIN_HOME%\main.py" -w "%CD%"
) else (
  "%HQIN_HOME%\.venv\Scripts\python.exe" "%HQIN_HOME%\main.py" %*
)
endlocal
