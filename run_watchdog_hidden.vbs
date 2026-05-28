' PPIS Campus Agent — Hidden Watchdog Runner
' Runs watchdog.bat completely silently — no window flash.
Set WshShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir
WshShell.Run "cmd.exe /c """ & scriptDir & "\watchdog.bat""", 0, True
Set WshShell = Nothing
