Set sh = CreateObject("Wscript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = fso.BuildPath(sh.CurrentDirectory, "setup_and_run.ps1")

' launch.log is UTF-8, shared with the launcher, setup and the widget, but a
' text stream writes the ANSI code page. So this script logs ASCII only: a
' numeric timestamp (Now would add a localized AM/PM word) and \uXXXX for any
' other character, such as a Korean folder name.
Function Pad(n)
    Pad = Right("0" & n, 2)
End Function

Function Stamp()
    t = Now
    Stamp = Year(t) & "-" & Pad(Month(t)) & "-" & Pad(Day(t)) & " " & _
        Pad(Hour(t)) & ":" & Pad(Minute(t)) & ":" & Pad(Second(t))
End Function

Function Ascii(s)
    Dim i, code, text
    text = ""
    For i = 1 To Len(s)
        code = AscW(Mid(s, i, 1))
        If code < 0 Then code = code + 65536
        If code >= 32 And code < 127 Then
            text = text & Mid(s, i, 1)
        Else
            text = text & "\u" & Right("000" & Hex(code), 4)
        End If
    Next
    Ascii = text
End Function

On Error Resume Next
dir = fso.BuildPath(sh.ExpandEnvironmentStrings("%APPDATA%"), "AiUsageWidget")
If Not fso.FolderExists(dir) Then fso.CreateFolder dir
Set tf = fso.OpenTextFile(fso.BuildPath(dir, "launch.log"), 8, True)
tf.WriteLine Stamp() & " vbs start " & Ascii(WScript.ScriptFullName)
If Not fso.FileExists(ps1) Then tf.WriteLine Stamp() & " missing setup_and_run.ps1"
tf.Close
On Error GoTo 0

exe = fso.BuildPath(sh.CurrentDirectory, "AI Usage.exe")
If fso.FileExists(exe) Then
    On Error Resume Next
    sh.Run """" & exe & """ --startup", 0, False
    started = (Err.Number = 0)
    On Error GoTo 0
    If started Then WScript.Quit 0
End If

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """"
sh.Run cmd, 0, False
