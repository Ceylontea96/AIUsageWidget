# Requires: Windows PowerShell 5+
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('codex-install', 'codex-login', 'cursor-install', 'cursor-open', 'prepare')]
    [string]$Action,
    [switch]$Codex,
    [switch]$Cursor
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Set-Utf8Console {
    try {
        cmd /c 'chcp 65001 >nul'
        $utf8 = New-Object System.Text.UTF8Encoding $false
        [Console]::OutputEncoding = $utf8
        [Console]::InputEncoding = $utf8
        $script:OutputEncoding = $utf8
    } catch {}
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

function Get-Codex {
    Refresh-Path
    $cmd = Get-Command codex -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }
    # Keep in step with _known_locations in codex_app_server.py.
    $candidates = @()
    if ($env:CODEX_INSTALL_DIR) { $candidates += (Join-Path $env:CODEX_INSTALL_DIR 'codex.exe') }
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA 'Programs\OpenAI\Codex\bin\codex.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\codex.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\codex\codex.exe')
    )
    $packages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    $candidates += @(Get-ChildItem -Path (Join-Path $packages 'OpenAI.Codex_*\codex-*-windows-msvc.exe') -ErrorAction SilentlyContinue | ForEach-Object FullName)
    $candidates += @(Get-ChildItem -Path (Join-Path $packages 'OpenAI.Codex_*\codex.exe') -ErrorAction SilentlyContinue | ForEach-Object FullName)
    $candidates += @(
        (Join-Path $env:USERPROFILE 'scoop\shims\codex.exe'),
        (Join-Path $env:USERPROFILE '.codex\bin\codex.exe'),
        (Join-Path $env:USERPROFILE '.local\bin\codex.exe'),
        (Join-Path $env:APPDATA 'npm\codex.cmd')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    # The Codex desktop app bundles codex.exe in a hashed folder that is not on PATH.
    $bundled = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin\*\codex.exe') -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($bundled) { return $bundled.FullName }
    return $null
}

function Get-Cursor {
    Refresh-Path
    foreach ($candidate in @(
            (Join-Path $env:LOCALAPPDATA 'Programs\cursor\Cursor.exe'),
            (Join-Path $env:LOCALAPPDATA 'cursor\Cursor.exe'),
            (Join-Path $env:ProgramFiles 'Cursor\Cursor.exe')
        )) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    $cmd = Get-Command cursor -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }
    return $null
}

function Install-Codex {
    if (Get-Codex) { return $true }
    Write-Host 'Codex CLI를 설치합니다. ChatGPT 데스크톱 앱과는 다릅니다.'
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host 'winget으로 OpenAI.Codex 설치 중...'
        & winget install -e --id OpenAI.Codex --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
        if (Get-Codex) { return $true }
    }
    Write-Host '공식 Codex 설치 스크립트를 실행합니다...'
    $script = (Invoke-WebRequest -Uri 'https://chatgpt.com/codex/install.ps1' -UseBasicParsing).Content
    Invoke-Expression $script
    return [bool](Get-Codex)
}

function Start-CodexLogin {
    $exe = Get-Codex
    if (-not $exe) {
        Write-Host 'Codex CLI를 찾지 못했습니다.'
        Start-Process 'https://github.com/openai/codex'
        return $false
    }
    Write-Host '브라우저가 열리면 ChatGPT 계정으로 로그인하세요.'
    Write-Host '끝나면 이 창을 닫아도 됩니다.'
    & $exe login
    return $true
}

function Install-Cursor {
    if (Get-Cursor) { return $true }
    Write-Host 'Cursor 앱을 설치합니다...'
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        & winget install -e --id Anysphere.Cursor --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
        if (Get-Cursor) { return $true }
    }
    Write-Host '설치 페이지를 엽니다. 설치가 끝나면 Enter를 누르세요.'
    Start-Process 'https://cursor.com/download'
    Read-Host '설치 후 Enter'
    return [bool](Get-Cursor)
}

function Start-CursorApp {
    $exe = Get-Cursor
    if ($exe) {
        Write-Host 'Cursor를 엽니다. 앱에서 로그인하세요.'
        Start-Process -FilePath $exe
        return $true
    }
    Start-Process 'https://cursor.com/download'
    return $false
}

Set-Utf8Console
Write-Host 'AI Usage 로그인 준비'
$needCodex = ($Action -eq 'codex-install') -or ($Action -eq 'codex-login') -or (($Action -eq 'prepare') -and $Codex)
$needCursor = ($Action -eq 'cursor-install') -or ($Action -eq 'cursor-open') -or (($Action -eq 'prepare') -and $Cursor)
$ok = $true

if ($needCodex) {
    if ($Action -eq 'codex-login') {
        if (-not (Start-CodexLogin)) { $ok = $false }
    } else {
        try {
            if (-not (Install-Codex)) { $ok = $false }
        } catch {
            Write-Host $_
            $ok = $false
        }
        if (-not (Start-CodexLogin)) { $ok = $false }
    }
}

if ($needCursor) {
    if ($Action -eq 'cursor-open') {
        if (-not (Start-CursorApp)) { $ok = $false }
    } else {
        try {
            if (-not (Install-Cursor)) { $ok = $false }
        } catch {
            Write-Host $_
            $ok = $false
        }
        if (-not (Start-CursorApp)) { $ok = $false }
    }
}

if (-not $ok) {
    Write-Host '일부 준비에 실패했습니다. 위 안내를 확인하세요.'
    Read-Host 'Enter'
    exit 1
}

Write-Host '끝나면 위젯이 로그인을 자동으로 감지합니다.'
if ($needCodex -or $needCursor) {
    Read-Host 'Enter'
}
exit 0
