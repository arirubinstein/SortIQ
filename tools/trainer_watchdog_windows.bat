@echo off
rem SortIQ trainer watchdog (Windows) - run once, from anywhere.
rem
rem Installs a scheduled task that checks every 2 minutes and silently
rem relaunches the trainer server if it has stopped. Pairs with
rem trainer_autostart_windows.bat (start at login); the watchdog covers
rem the rest of the day - a crashed trainer revives within 2 minutes,
rem and each revival is logged to watchdog.log in the repo folder.
rem
rem   tools\trainer_watchdog_windows.bat          install + first check now
rem   tools\trainer_watchdog_windows.bat remove   uninstall
setlocal
cd /d "%~dp0.."
set "TASK=SortIQ Trainer Watchdog"

if /i "%~1"=="remove" (
    schtasks /delete /tn "%TASK%" /f >nul 2>&1 && echo Watchdog removed. || echo No watchdog installed.
    exit /b 0
)

if not exist ".venv\Scripts\pythonw.exe" (
    echo No .venv found next to this script's parent folder.
    echo Finish the one-time setup in docs\TRAINER_SETUP.md first.
    pause
    exit /b 1
)

schtasks /create /tn "%TASK%" /sc minute /mo 2 /f /tr "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%CD%\tools\trainer_watchdog.ps1\"" >nul
if errorlevel 1 (
    echo Could not create the scheduled task.
    pause
    exit /b 1
)

rem run one check right now so a currently-dead trainer revives immediately
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%CD%\tools\trainer_watchdog.ps1"

echo.
echo Installed. Every 2 minutes, a stopped trainer is silently restarted
echo (only while you are logged in). Revivals are noted in watchdog.log.
echo To undo:  tools\trainer_watchdog_windows.bat remove
pause
