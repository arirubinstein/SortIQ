@echo off
rem SortIQ trainer autostart (Windows) - run once, from anywhere.
rem
rem Installs a Startup shortcut so the trainer server starts silently
rem (no console window) every time you log in, and starts it now.
rem After this, the machine's Train page always finds the trainer at
rem http://localhost:5000 - no terminal needed ever again.
rem
rem   tools\trainer_autostart_windows.bat          install + start now
rem   tools\trainer_autostart_windows.bat remove   uninstall autostart
setlocal
cd /d "%~dp0.."
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\SortIQ Trainer.lnk"

if /i "%~1"=="remove" (
    if exist "%LNK%" del "%LNK%" && echo Autostart removed.
    if not exist "%LNK%" echo No autostart installed.
    exit /b 0
)

if not exist ".venv\Scripts\pythonw.exe" (
    echo No .venv found next to this script's parent folder.
    echo Finish the one-time setup in docs\TRAINER_SETUP.md first.
    pause
    exit /b 1
)

powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%LNK%'); $s.TargetPath = '%CD%\.venv\Scripts\pythonw.exe'; $s.Arguments = 'webui\server.py --port 5000'; $s.WorkingDirectory = '%CD%'; $s.Description = 'SortIQ trainer server (silent)'; $s.Save()"
if errorlevel 1 (
    echo Could not create the Startup shortcut.
    pause
    exit /b 1
)

rem start it now (silent); harmless if one is already running - the new
rem process exits because port 5000 is taken
start "" "%CD%\.venv\Scripts\pythonw.exe" webui\server.py --port 5000

echo.
echo Installed. The trainer now starts automatically when you log in,
echo and is starting right now at http://localhost:5000
echo (If Windows Firewall asks, allow access on private networks.)
echo To undo:  tools\trainer_autostart_windows.bat remove
pause
