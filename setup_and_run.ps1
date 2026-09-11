# Requires: Windows PowerShell 5+
param([switch]$InstallUi)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$MinPython = [version]'3.9'

function Set-Utf8Console {
    try {
        cmd /c 'chcp 65001 >nul'
        $utf8 = New-Object System.Text.UTF8Encoding $false
        [Console]::OutputEncoding = $utf8
        [Console]::InputEncoding = $utf8
        $script:OutputEncoding = $utf8
    } catch {}
}

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Widget = Join-Path $Here 'usage_widget.py'
if (-not (Test-Path -LiteralPath $Widget)) {
    Set-Utf8Console
    Write-Host 'usage_widget.py 를 찾지 못했습니다.'
    Read-Host 'Enter'
    exit 1
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = New-Object System.Collections.Generic.List[string]
    $seen = @{}
    foreach ($chunk in @($user, $machine, $env:Path)) {
        if (-not $chunk) { continue }
        foreach ($piece in ($chunk -split ';')) {
            if (-not $piece -or $seen.ContainsKey($piece)) { continue }
            $seen[$piece] = $true
            $parts.Add($piece) | Out-Null
        }
    }
    $env:Path = $parts -join ';'
}

function Test-TinyOrMissing([string]$Path) {
    try {
        $item = Get-Item -LiteralPath $Path -ErrorAction Stop
        return ($item.Length -lt 1024)
    } catch {
        return $true
    }
}

function Test-NeedsResolve([string]$Path) {
    $leaf = Split-Path -Leaf $Path
    return ($leaf -match '^py(\.exe)?$') -or ($Path -like '*WindowsApps*') -or (Test-TinyOrMissing $Path)
}

function Invoke-PythonText {
    param(
        [string]$Exe,
        [string[]]$Args,
        [int]$TimeoutMs = 3000
    )
    $outFile = [IO.Path]::GetTempFileName()
    $errFile = [IO.Path]::GetTempFileName()
    try {
        $p = Start-Process -FilePath $Exe -ArgumentList $Args -WorkingDirectory $Here -RedirectStandardOutput $outFile -RedirectStandardError $errFile -WindowStyle Hidden -PassThru
        if (-not $p.WaitForExit($TimeoutMs)) {
            try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
            return $null
        }
        if ($p.ExitCode -ne 0) { return $null }
        return ([IO.File]::ReadAllText($outFile).Trim())
    } catch {
        return $null
    } finally {
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-PythonArgs([string]$Exe, [string]$Code) {
    if ((Split-Path -Leaf $Exe) -match '^py(\.exe)?$') {
        return @('-3', '-B', '-c', $Code)
    }
    return @('-B', '-c', $Code)
}

function Resolve-PythonExe([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $null }
    if (-not (Test-NeedsResolve $Path)) { return $Path }
    $text = Invoke-PythonText -Exe $Path -Args (Get-PythonArgs $Path 'import sys; print(sys.executable)')
    if ($text -and (Test-Path -LiteralPath $text)) { return $text }
    return $null
}

function Add-RawCandidate {
    param([string]$Path, $Seen, $Hits)
    if (-not $Path) { return }
    try { $Path = (Get-Item -LiteralPath $Path).FullName } catch { return }
    if ($Path -like '*WindowsApps*') { return }
    $key = $Path.ToLowerInvariant()
    if ($Seen.ContainsKey($key)) { return }
    $Seen[$key] = $true
    $Hits.Add($Path) | Out-Null
}

function Get-RawPythonHits {
    Refresh-Path
    $seen = @{}
    $hits = New-Object System.Collections.Generic.List[string]

    foreach ($key in @(
            'HKCU:\Software\Python\PythonCore',
            'HKLM:\Software\Python\PythonCore',
            'HKLM:\Software\Wow6432Node\Python\PythonCore'
        )) {
        if (-not (Test-Path -LiteralPath $key)) { continue }
        Get-ChildItem -LiteralPath $key -ErrorAction SilentlyContinue | ForEach-Object {
            $install = Join-Path $_.PSPath 'InstallPath'
            if (-not (Test-Path -LiteralPath $install)) { return }
            $props = Get-ItemProperty -LiteralPath $install -ErrorAction SilentlyContinue
            $exe = $props.ExecutablePath
            if (-not $exe -and $props.'(default)') {
                $exe = Join-Path $props.'(default)' 'python.exe'
            }
            Add-RawCandidate -Path $exe -Seen $seen -Hits $hits
        }
    }

    $roots = New-Object System.Collections.Generic.List[string]
    foreach ($root in @(
            (Join-Path $env:LocalAppData 'Programs\Python'),
            (Join-Path $env:ProgramFiles 'Python'),
            (Join-Path ${env:ProgramFiles(x86)} 'Python')
        )) {
        if ($root) { $roots.Add($root) | Out-Null }
    }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $base -or -not (Test-Path -LiteralPath $base)) { continue }
        Get-ChildItem -LiteralPath $base -Directory -Filter 'Python*' -ErrorAction SilentlyContinue | ForEach-Object {
            $roots.Add($_.FullName) | Out-Null
        }
    }
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $direct = Join-Path $root 'python.exe'
        if (Test-Path -LiteralPath $direct) {
            Add-RawCandidate -Path $direct -Seen $seen -Hits $hits
            continue
        }
        Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            Add-RawCandidate -Path (Join-Path $_.FullName 'python.exe') -Seen $seen -Hits $hits
        }
    }

    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    foreach ($name in @('py', 'python', 'python3')) {
        foreach ($cmd in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            if ($cmd) { Add-RawCandidate -Path $cmd.Source -Seen $seen -Hits $hits }
        }
        foreach ($line in @(& where.exe $name 2>$null)) {
            if ($line) { Add-RawCandidate -Path $line.Trim() -Seen $seen -Hits $hits }
        }
    }
    $ErrorActionPreference = $prev
    return $hits
}

function Test-ReadyPython([string]$PythonExe) {
    $code = "import sys,tkinter; raise SystemExit(0 if sys.version_info>=($($MinPython.Major),$($MinPython.Minor)) else 3)"
    $text = Invoke-PythonText -Exe $PythonExe -Args (Get-PythonArgs $PythonExe $code)
    return ($null -ne $text)
}

function Get-ReadyPython {
    $raw = @(Get-RawPythonHits)
    $real = @($raw | Where-Object { -not (Test-NeedsResolve $_) })
    $launchers = @($raw | Where-Object { Test-NeedsResolve $_ })
    foreach ($exe in $real) {
        if (Test-ReadyPython $exe) { return $exe }
    }
    foreach ($launcher in $launchers) {
        $resolved = Resolve-PythonExe $launcher
        if ($resolved -and (Test-ReadyPython $resolved)) { return $resolved }
    }
    return $null
}

function Get-Pythonw([string]$PythonExe) {
    $dir = Split-Path -Parent $PythonExe
    foreach ($name in @('pythonw.exe', 'pyw.exe')) {
        $candidate = Join-Path $dir $name
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Install-WithWinget {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { return $false }
    foreach ($id in @('Python.Python.3.13', 'Python.Python.3.12')) {
        Write-Host "winget으로 $id 설치 중..."
        & winget install -e --id $id --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
        Refresh-Path
        if (Get-ReadyPython) { return $true }
    }
    return $false
}

function Install-FromPythonOrg {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }
    $ver = '3.12.10'
    $url = "https://www.python.org/ftp/python/$ver/python-$ver-$arch.exe"
    $tmp = Join-Path $env:TEMP "python-$ver-$arch.exe"
    Write-Host "Python $ver 설치 파일을 받는 중..."
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    Write-Host 'Python 설치 중... (1~2분)'
    $installArgs = '/quiet InstallAllUsers=0 PrependPath=1 Include_tcltk=1 Include_pip=1 Include_test=0 Include_doc=0 Include_launcher=1 SimpleInstall=1'
    $p = Start-Process -FilePath $tmp -ArgumentList $installArgs -Wait -PassThru
    Refresh-Path
    return ($p.ExitCode -eq 0) -and (Get-ReadyPython)
}

function Unblock-Here {
    Get-ChildItem -LiteralPath $Here -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
        Unblock-File -LiteralPath $_.FullName -ErrorAction SilentlyContinue
    }
}

function Write-LaunchLog([string]$Message) {
    $dir = Join-Path $env:APPDATA 'AiUsageWidget'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath (Join-Path $dir 'launch.log') -Value $line -Encoding UTF8
}

function Show-Popup([string]$Message, [int]$Icon = 64) {
    Write-LaunchLog $Message
    try {
        $wshell = New-Object -ComObject WScript.Shell
        $null = $wshell.Popup($Message, 0, 'AI Usage', $Icon)
        return
    } catch {}
    if ($InstallUi) {
        Write-Host $Message
        Read-Host 'Enter'
    }
}

function Show-LaunchError([string]$Message) {
    Show-Popup $Message 16
}

function Start-Widget([string]$PythonExe) {
    $pythonw = Get-Pythonw $PythonExe
    if (-not $pythonw) {
        Show-LaunchError "창 없는 Python(pythonw.exe)을 찾지 못했습니다.`n$PythonExe"
        exit 1
    }
    try {
        $p = Start-Process -FilePath $pythonw -ArgumentList @('-B', $Widget) -WorkingDirectory $Here -WindowStyle Hidden -PassThru
    } catch {
        Show-LaunchError "위젯을 시작하지 못했습니다.`n$pythonw`n$_"
        exit 1
    }
    if (-not $p) {
        Show-LaunchError "위젯 프로세스를 만들지 못했습니다.`n$pythonw"
        exit 1
    }
    Write-LaunchLog "widget start $pythonw pid $($p.Id)"
    Start-Sleep -Milliseconds 1200
    if (-not $p.HasExited) { return }
    Write-LaunchLog "widget exited $($p.ExitCode)"
    if ($p.ExitCode -eq 0) { return }
    $log = Join-Path $env:APPDATA 'AiUsageWidget\error.log'
    $extra = ''
    if (Test-Path -LiteralPath $log) {
        $item = Get-Item -LiteralPath $log
        if (((Get-Date) - $item.LastWriteTime).TotalSeconds -lt 10) {
            $extra = "`n`n" + [string](Get-Content -LiteralPath $log -Raw -ErrorAction SilentlyContinue)
        }
    }
    Show-LaunchError "위젯이 바로 종료되었습니다 (코드 $($p.ExitCode)).`nzip을 폴더로 푼 뒤 start_usage_widget.vbs 또는 start_usage_widget.bat 을 실행하세요.$extra"
    exit 1
}

try {
    Write-LaunchLog 'setup start'
    Unblock-Here
    $python = Get-ReadyPython
    if ($python) {
        Start-Widget $python
        exit 0
    }

    if (-not $InstallUi) {
        Show-Popup 'Python이 없어 설치합니다. 1~2분 걸릴 수 있습니다. 설치가 끝날 때까지 기다리세요.'
        $ui = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $PSCommandPath, '-InstallUi'
        ) -WindowStyle Hidden -Wait -PassThru
        if ($ui -and $ui.ExitCode -ne 0) { exit $ui.ExitCode }
        exit 0
    }
} catch {
    Show-LaunchError "실행에 실패했습니다.`n$_"
    exit 1
}

Write-LaunchLog 'InstallUi start'
$ok = $false
try { $ok = Install-WithWinget } catch { Write-LaunchLog $_; $ok = $false }
if (-not $ok) {
    try { $ok = Install-FromPythonOrg } catch { Write-LaunchLog $_; $ok = $false }
}
$python = Get-ReadyPython
if (-not $python) {
    Start-Process 'https://www.python.org/downloads/windows/'
    Show-LaunchError "자동 설치에 실패했습니다. 브라우저에서 Python 3.12를 설치한 뒤 start_usage_widget.vbs 를 다시 실행하세요.`n설치 시 Add python.exe to PATH 와 tcl/tk 를 켜세요."
    exit 1
}

Start-Widget $python
exit 0
