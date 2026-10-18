@echo off
rem Check the screen read lines up (after a GSPro update or resolution change): bring GSPro to the
rem front during the countdown. Run from a logged-in desktop session, not over SSH: screen capture
rem needs an interactive desktop. Add --fix to write moved boxes to config.yaml.
cd /d "%~dp0"
if not exist logs mkdir logs
echo Bring GSPro to the front: checking the screen in 8 seconds...
.venv\Scripts\python.exe -m commentator screen-check --delay 8 %* > logs\screen-check.txt 2>&1
type logs\screen-check.txt
echo Wrote logs\screen-check.txt (boxes drawn on logs\screen-check.png)
pause
