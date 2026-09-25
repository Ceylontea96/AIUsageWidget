#Requires -Version 5
# Builds AI Usage.exe from launcher\AIUsageLauncher.cs, but only when the
# source or icon changed since the committed exe was built. csc output is not
# byte-for-byte reproducible, so an unneeded rebuild would change the exe on
# every release. launcher\build.stamp records what the current exe was built
# from; -Force rebuilds anyway.
param([switch]$Force)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $here 'launcher\AIUsageLauncher.cs'
$stamp = Join-Path $here 'launcher\build.stamp'
$out = Join-Path $here 'AI Usage.exe'
if (-not (Test-Path -LiteralPath $source)) { throw 'launcher\AIUsageLauncher.cs 를 찾지 못했습니다.' }

$icon = (Join-Path $here 'assets\icons\app.ico')
if (-not (Test-Path -LiteralPath $icon)) {
    # Failing loudly beats shipping an executable with the generic .NET icon.
    throw "assets\icons\app.ico 를 찾지 못했습니다: $icon"
}
$icon = (Get-Item -LiteralPath $icon).FullName

function Get-Sha256Hex([byte[]]$Bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

$options = '/target:winexe /platform:anycpu /optimize+ /codepage:65001'
# Line endings follow git's checkout settings, so they are not part of the input.
$code = [System.IO.File]::ReadAllText($source).Replace("`r`n", "`n")
$inputs = @(
    (Get-Sha256Hex ([System.Text.Encoding]::UTF8.GetBytes($code))),
    (Get-Sha256Hex ([System.IO.File]::ReadAllBytes($icon))),
    $options
) -join "`n"
$sourceHash = Get-Sha256Hex ([System.Text.Encoding]::UTF8.GetBytes($inputs))

if (-not $Force -and (Test-Path -LiteralPath $out) -and (Test-Path -LiteralPath $stamp)) {
    $recorded = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($stamp)) {
        $parts = $line.Trim() -split '\s+', 2
        if ($parts.Count -eq 2) { $recorded[$parts[0]] = $parts[1] }
    }
    $exeHash = Get-Sha256Hex ([System.IO.File]::ReadAllBytes($out))
    if ($recorded['source'] -eq $sourceHash -and $recorded['exe'] -eq $exeHash) {
        Write-Host "launcher up to date $out"
        return
    }
}

$csc = @(
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $csc) { throw '.NET Framework csc.exe 를 찾지 못했습니다.' }

$winDir = $env:WINDIR
$forms = Join-Path $winDir 'Microsoft.NET\Framework64\v4.0.30319\System.Windows.Forms.dll'
if (-not (Test-Path -LiteralPath $forms)) {
    $forms = Join-Path $winDir 'Microsoft.NET\Framework\v4.0.30319\System.Windows.Forms.dll'
}

& $csc /nologo @($options -split ' ') "/out:$out" "/win32icon:$icon" "/reference:$forms" $source
if ($LASTEXITCODE -ne 0) { throw "launcher build failed ($LASTEXITCODE)" }
if (-not (Test-Path -LiteralPath $out)) { throw 'AI Usage.exe 가 만들어지지 않았습니다.' }
$exeHash = Get-Sha256Hex ([System.IO.File]::ReadAllBytes($out))
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($stamp, "source $sourceHash`nexe $exeHash`n", $utf8)
Write-Host "built $out"
