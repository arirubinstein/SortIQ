' Truly hidden launcher for the trainer watchdog. Task Scheduler flashes
' a console for any console program it starts - even powershell with
' -WindowStyle Hidden - so the every-2-minutes check would blink a window
' over the desktop. wscript.exe is a GUI-subsystem host with no console
' at all; it runs the PowerShell check at window state 0 (hidden).
' Installed as the scheduled task's action by trainer_watchdog_windows.bat.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & _
    here & "\trainer_watchdog.ps1""", 0, False
