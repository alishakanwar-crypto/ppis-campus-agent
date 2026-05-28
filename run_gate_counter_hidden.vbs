' PPIS Gate Head Count Counter — Hidden Background Runner
' Runs run_gate_counter.bat completely silently — no window flash.
'
' Usage: wscript.exe run_gate_counter_hidden.vbs
' To stop: Open Task Manager > Details > find python.exe > End Task

Set WshShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir
WshShell.Run """" & scriptDir & "\run_gate_counter.bat""", 0, False
