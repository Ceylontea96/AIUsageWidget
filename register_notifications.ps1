param([string]$WidgetDirectory = $PSScriptRoot, [string]$Pythonw)
$ErrorActionPreference = 'Stop'
if (-not $Pythonw) {
    $python = & py -3 -c 'import sys; print(sys.executable)'
    $Pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
}
$script = Join-Path $WidgetDirectory 'usage_widget.py'
if (-not (Test-Path -LiteralPath $Pythonw) -or -not (Test-Path -LiteralPath $script)) { throw 'Widget/runtime not found.' }
$path = Join-Path ([Environment]::GetFolderPath('Programs')) 'AI Usage Widget.lnk'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($path)
$link.TargetPath = $Pythonw
$link.Arguments = '-B "' + $script + '"'
$link.WorkingDirectory = $WidgetDirectory
$link.Description = 'AI Usage Widget'
$link.Save()
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class UsageToastIdentity {
    [StructLayout(LayoutKind.Sequential)]
    public struct Key { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Explicit, Size=24)]
    public struct Value { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr pointer; }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface Store {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int GetAt(uint index, out Key key);
        [PreserveSig] int GetValue(ref Key key, out Value value);
        [PreserveSig] int SetValue(ref Key key, ref Value value);
        [PreserveSig] int Commit();
    }
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, PreserveSig=true)]
    static extern int SHGetPropertyStoreFromParsingName(string path, IntPtr bind, uint flags, ref Guid iid, out Store store);
    public static void Register(string path) {
        var iid = typeof(Store).GUID;
        Store store;
        Marshal.ThrowExceptionForHR(SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref iid, out store));
        var key = new Key {fmtid=new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"),pid=5};
        var value = new Value {vt=31,pointer=Marshal.StringToCoTaskMemUni("Jinho.AIUsageWidget")};
        try {
            Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref value));
            Marshal.ThrowExceptionForHR(store.Commit());
        } finally { Marshal.FreeCoTaskMem(value.pointer); Marshal.ReleaseComObject(store); }
    }
}
'@
[UsageToastIdentity]::Register($path)
Write-Output 'AI Usage notification identity registered.'
