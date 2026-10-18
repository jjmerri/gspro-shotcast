@echo off
rem Install Shotcast in this folder: finds (or installs) Python, makes .venv, installs the packages,
rem then tools\install_setup.py creates config.yaml and .env and asks for keys. Safe to re-run.
setlocal
cd /d "%~dp0"
title Shotcast install
echo.
echo   GSPro Shotcast installer
echo   ========================
echo.

set "PY="
py -3.13 -c "pass" >nul 2>&1 && set "PY=py -3.13"
if not defined PY py -3.12 -c "pass" >nul 2>&1 && set "PY=py -3.12"
if not defined PY if exist .venv\Scripts\python.exe goto have_python
if defined PY goto have_python

echo Python 3.12 or 3.13 isn't installed (Shotcast needs one of them).
where winget >nul 2>&1 || goto no_python
choice /c YN /m "Install Python 3.13 now with winget"
if errorlevel 2 goto no_python
winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements
echo.
echo Python is installed. Close this window and double-click install.bat again.
pause
exit /b 0

:no_python
echo Install Python 3.13 from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo then double-click install.bat again.
pause
exit /b 1

:have_python
echo What do you want?
echo   1. Shot tracking and stats only (no API keys, nothing leaves this PC)
echo   2. Tracking plus the AI booth (needs OpenAI and Anthropic API keys)
choice /c 12 /n /m "Choose 1 or 2: "
if errorlevel 2 (set "MODE=booth" & set "EXTRAS=capture,cloud") else (set "MODE=tracking" & set "EXTRAS=capture")
echo.

if exist .venv\Scripts\python.exe goto have_venv
echo Making the Python environment (.venv)...
%PY% -m venv .venv || goto failed
:have_venv

echo Installing packages (a few minutes the first time)...
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip || goto failed
.venv\Scripts\python.exe -m pip install --quiet -e ".[%EXTRAS%]" || goto failed
rem update.bat reinstalls the same extras.
> .venv\shotcast-extras.txt echo %EXTRAS%

.venv\Scripts\python.exe tools\install_setup.py --mode %MODE% || goto failed
echo.
pause
exit /b 0

:failed
echo.
echo Install stopped on the error above. Fix it (often the internet connection) and run install.bat again.
pause
exit /b 1
