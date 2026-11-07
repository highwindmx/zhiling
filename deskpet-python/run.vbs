' Silent launcher for 智灵桌宠: no console window at all.
' Double-click this (or make a shortcut to it) instead of run.bat.
' Arguments are forwarded, e.g:  wscript run.vbs --state-port 8766
Option Explicit
Dim fso, sh, root, exe, cmd, i
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root
If fso.FileExists(root & "\.venv\Scripts\pythonw.exe") Then
    exe = root & "\.venv\Scripts\pythonw.exe"
Else
    exe = "pythonw.exe"
End If
cmd = """" & exe & """ """ & root & "\pet.py"""
For i = 0 To WScript.Arguments.Count - 1
    cmd = cmd & " """ & Replace(WScript.Arguments(i), """", "") & """"
Next
sh.Run cmd, 0, False      ' 0 = hidden window, False = do not wait
