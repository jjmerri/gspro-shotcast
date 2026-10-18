@echo off
rem Update Shotcast to the latest version. Keeps config.yaml, .env, data\league.json and logs\
rem (none of them are in the download). Close the Golf Booth window first.
setlocal
rem Windows reads a .bat as it runs, so run a copy from TEMP: the update may replace this file.
if /i not "%~1"=="--from-temp" (
  copy /y "%~f0" "%TEMP%\shotcast-update.bat" >nul
  "%TEMP%\shotcast-update.bat" --from-temp "%~dp0."
  exit /b
)
cd /d "%~2"
title Shotcast update
echo Close the Golf Booth window before updating, if it's open.
pause

if not exist .git goto zip
where git >nul 2>&1 || goto zip
echo Updating with git...
git pull --ff-only || goto git_failed
goto packages

:zip
echo Downloading the latest version from GitHub...
powershell -NoProfile -ExecutionPolicy Bypass -File tools\update_from_zip.ps1 || goto failed

:packages
set "EXTRAS=capture"
if exist .venv\shotcast-extras.txt set /p EXTRAS=<.venv\shotcast-extras.txt
echo Updating packages...
.venv\Scripts\python.exe -m pip install --quiet -e ".[%EXTRAS%]" || goto failed
echo.
echo Updated. If GSPro updated too, run screen-check.bat. New settings show up in
echo config.example.yaml; copy any you want into config.yaml.
pause
exit /b 0

:git_failed
echo.
echo git couldn't update: usually a file you edited in this folder (other than config.yaml and
echo .env). "git status" lists them; "git stash" sets them aside. Then run update.bat again.
pause
exit /b 1

:failed
echo.
echo Update stopped on the error above. Nothing you set up was touched; run update.bat again.
pause
exit /b 1
