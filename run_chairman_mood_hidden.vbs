' PPIS Chairman Mood Monitor — Hidden Background Runner
' Runs run_chairman_mood.bat completely silently — no window flash.
'
' Usage: wscript.exe run_chairman_mood_hidden.vbs
' To stop: Open Task Manager > Details > find python.exe > End Task

Set WshShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir
WshShell.Run """" & scriptDir & "\run_chairman_mood.bat""", 0, False
