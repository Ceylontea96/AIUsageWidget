Set sh = CreateObject("Wscript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = fso.BuildPath(sh.CurrentDirectory, "setup_and_run.ps1")

On Error Resume Next
dir = fso.BuildPath(sh.ExpandEnvironmentStrings("%APPDATA%"), "AiUsageWidget")
If Not fso.FolderExists(dir) Then fso.CreateFolder dir
Set tf = fso.OpenTextFile(fso.BuildPath(dir, "launch.log"), 8, True)
tf.WriteLine Now & " vbs start " & WScript.ScriptFullName
If Not fso.FileExists(ps1) Then tf.WriteLine Now & " missing setup_and_run.ps1"
tf.Close
On Error GoTo 0

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """"
sh.Run cmd, 0, False
