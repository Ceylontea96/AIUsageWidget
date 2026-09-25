#Requires -Version 5
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$Desktop
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
try {
    $exe = Join-Path $Root 'AI Usage.exe'
    if (-not (Test-Path -LiteralPath $exe)) {
        throw 'AI Usage.exe를 찾지 못했습니다. zip을 폴더로 푼 뒤 다시 시도하세요.'
    }
    if (-not $Desktop) {
        $Desktop = [Environment]::GetFolderPath('Desktop')
    }
    if (-not $Desktop) {
        throw '바탕화면 폴더를 찾지 못했습니다.'
    }
    New-Item -ItemType Directory -Force -Path $Desktop | Out-Null
    $lnk = Join-Path $Desktop 'AI Usage.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($lnk)
    $shortcut.TargetPath = (Get-Item -LiteralPath $exe).FullName
    $shortcut.WorkingDirectory = (Get-Item -LiteralPath $Root).FullName
    $shortcut.WindowStyle = 1
    $shortcut.Description = 'AI Usage'
    $shortcut.IconLocation = $shortcut.TargetPath
    $shortcut.Save()
    Write-Output $lnk
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
