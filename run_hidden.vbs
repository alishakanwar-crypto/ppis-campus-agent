' PPIS Campus Agent — Hidden Background Runner
' This VBS script launches run_forever.bat without showing a Command Prompt window.
' The agent runs completely in the background — no visible window needed.
'
' Usage: Double-click this file, or run via Task Scheduler.
' To stop: Open Task Manager > Details > find python.exe > End Task

Set WshShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir
WshShell.Run """" & scriptDir & "\run_forever.bat""", 0, False
