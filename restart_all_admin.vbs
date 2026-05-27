' PPIS Campus Agent — One-Click Restart (with Admin Elevation)
' Double-click this file to restart all agents cleanly.
' It will ask for admin permission automatically.

Set objShell = CreateObject("Shell.Application")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
objShell.ShellExecute "cmd.exe", "/c """ & scriptDir & "\restart_all.bat""", scriptDir, "runas", 1
