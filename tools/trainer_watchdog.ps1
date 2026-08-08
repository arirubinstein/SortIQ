# SortIQ trainer watchdog - relaunches the trainer server if it has
# stopped. Installed as a scheduled task by trainer_watchdog_windows.bat;
# harmless to run by hand. Appends a line to watchdog.log (repo root)
# whenever it actually revives something, so silent deaths leave a trace.
$root = Split-Path -Parent $PSScriptRoot
$up = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
      Where-Object { $_.CommandLine -like '*webui\server.py*' }
if (-not $up) {
    Start-Process -FilePath (Join-Path $root '.venv\Scripts\pythonw.exe') `
        -ArgumentList 'webui\server.py', '--port', '5000' `
        -WorkingDirectory $root
    Add-Content -Path (Join-Path $root 'watchdog.log') `
        -Value "$(Get-Date -Format s) trainer was down - relaunched"
}
