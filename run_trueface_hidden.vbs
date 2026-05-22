Set WshShell = CreateObject("WScript.Shell")
WshShell.Run chr(34) & CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\run_trueface.bat" & chr(34), 0
Set WshShell = Nothing
