#Requires -Version 5
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $here 'launcher\AIUsageLauncher.cs'
$out = Join-Path $here 'AI Usage.exe'
if (-not (Test-Path -LiteralPath $source)) { throw 'launcher\AIUsageLauncher.cs 를 찾지 못했습니다.' }

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

& $csc /nologo /target:winexe /platform:anycpu /optimize+ /codepage:65001 "/out:$out" "/reference:$forms" $source
if ($LASTEXITCODE -ne 0) { throw "launcher build failed ($LASTEXITCODE)" }
if (-not (Test-Path -LiteralPath $out)) { throw 'AI Usage.exe 가 만들어지지 않았습니다.' }
Write-Host "built $out"
