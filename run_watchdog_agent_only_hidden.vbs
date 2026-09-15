' PPIS Campus Agent — Hidden Watchdog Runner (campus agent only)
' Runs watchdog.bat in agent-only mode, silently. This copy is meant for the
' SYSTEM-run scheduled task, which works with nobody logged on, so a
' night-time reboot or crash cannot leave parents without live photos.
Set WshShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir
WshShell.Run "cmd.exe /c """ & scriptDir & "\watchdog.bat"" agent-only", 0, True
Set WshShell = Nothing
