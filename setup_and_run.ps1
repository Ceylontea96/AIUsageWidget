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
        [int]$TimeoutMs = 10000
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
    $pythonw = Join-Path $dir 'pythonw.exe'
    if (Test-Path -LiteralPath $pythonw) { return $pythonw }
    return $PythonExe
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

function Start-Widget([string]$PythonExe) {
    $pythonw = Get-Pythonw $PythonExe
    Start-Process -FilePath $pythonw -ArgumentList @('-B', $Widget) -WorkingDirectory $Here
}

$python = Get-ReadyPython
if ($python) {
    Start-Widget $python
    exit 0
}

if (-not $InstallUi) {
    Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-InstallUi'
    ) -Wait
    exit 0
}

Set-Utf8Console
Write-Host 'AI Usage 위젯'
if (@(Get-RawPythonHits).Count -gt 0) {
    Write-Host "있는 Python이 $($MinPython) 미만이거나 Tk를 쓸 수 없습니다."
    Write-Host '기존 Python은 그대로 두고, 위젯용 Python을 추가로 설치합니다. 인터넷이 필요합니다.'
} else {
    Write-Host 'Python이 없어 설치합니다. 인터넷이 필요합니다.'
}
$ok = $false
try { $ok = Install-WithWinget } catch { $ok = $false }
if (-not $ok) {
    try { $ok = Install-FromPythonOrg } catch { Write-Host $_; $ok = $false }
}
$python = Get-ReadyPython
if (-not $python) {
    Write-Host '자동 설치에 실패했습니다. 브라우저에서 Python을 설치하세요.'
    Write-Host '설치 시 Add python.exe to PATH 와 tcl/tk 가 켜져 있어야 합니다.'
    Start-Process 'https://www.python.org/downloads/windows/'
    Read-Host '설치 후 Enter'
    Refresh-Path
    $python = Get-ReadyPython
}
if (-not $python) {
    Write-Host 'Python을 찾지 못해 위젯을 실행할 수 없습니다.'
    Read-Host 'Enter'
    exit 1
}

Write-Host '위젯을 시작합니다.'
Start-Widget $python
exit 0
