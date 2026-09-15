#Requires -Version 5
param([switch]$GitHub)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding $false
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $project 'build_launcher.ps1')
$updater = Get-Content -LiteralPath (Join-Path $project 'updater.py') -Raw
if ($updater -notmatch "APP_VERSION = '([^']+)'") { throw 'APP_VERSION not found' }
$version = $Matches[1]

function Get-ChangelogSection {
    param([string]$Path, [string]$Version)
    if (-not (Test-Path -LiteralPath $Path)) { return "AI Usage $Version" }
    $text = [System.IO.File]::ReadAllText($Path)
    $escaped = [regex]::Escape($Version)
    $match = [regex]::Match($text, "(?ms)^## \[$escaped\][^\r\n]*\r?\n(.*?)(?=^## |\z)")
    if (-not $match.Success) { return "AI Usage $Version" }
    $body = $match.Groups[1].Value.Trim()
    $header = "## $Version"
    $dateMatch = [regex]::Match($text, "(?m)^## \[$escaped\][ \t]*-[ \t]*(\S+)")
    if ($dateMatch.Success) { $header = "## $Version - $($dateMatch.Groups[1].Value)" }
    return ($header + "`n`n" + $body).Trim() + "`n"
}

function Get-LatestNotes {
    param([string]$Section, [string]$Version)
    $added = [regex]::Matches($Section, '(?m)^- (.+)$')
    if ($added.Count -gt 0) {
        $bits = @()
        foreach ($item in $added) {
            $bits += $item.Groups[1].Value.Trim()
            if ($bits.Count -ge 3) { break }
        }
        return ($bits -join ' / ')
    }
    return "AI Usage $Version"
}

$feed = ''
$feedFile = Join-Path $project 'feed_url.txt'
foreach ($line in Get-Content -LiteralPath $feedFile) {
    $trim = $line.Trim()
    if ($trim -and -not $trim.StartsWith('#')) { $feed = $trim; break }
}
$zipUrl = ''
if ($feed.EndsWith('latest.json')) {
    $zipUrl = $feed.Substring(0, $feed.Length - 'latest.json'.Length) + 'AIUsageWidget.zip'
}
$changelogPath = Join-Path $project 'CHANGELOG.md'
$releaseNotes = Get-ChangelogSection -Path $changelogPath -Version $version
$shortNotes = Get-LatestNotes -Section $releaseNotes -Version $version
$latest = [ordered]@{
    version = $version
    zip     = $zipUrl
    notes   = $shortNotes
} | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText((Join-Path $project 'latest.json'), $latest + "`n", $utf8)

$stage = Join-Path $env:TEMP 'AIUsageWidget_dist_stage'
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $stage 'assets\icons') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage 'assets\fonts') -Force | Out-Null
$copy = @(
    'usage_widget.py', 'providers.py', 'runtime.py', 'codex_activity.py', 'poll_worker.py', 'updater.py',
    'setup_and_run.ps1', 'setup_login.ps1', 'create_shortcut.ps1',
    'start_usage_widget.vbs', 'start_usage_widget.bat',
    'toast.ps1', 'register_notifications.ps1', 'feed_url.txt', 'CHANGELOG.md', 'LICENSE', 'CODEX_ACTIVITY.md'
)
foreach ($f in $copy) {
    $src = Join-Path $project $f
    if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $stage $f) -Force
    }
}
$launcher = Join-Path $project 'AI Usage.exe'
if (-not (Test-Path -LiteralPath $launcher)) { throw 'AI Usage.exe missing after build' }
Copy-Item -LiteralPath $launcher -Destination (Join-Path $stage 'AI Usage.exe') -Force
Get-ChildItem -LiteralPath $project -File | Where-Object { $_.Extension -in '.bat', '.vbs' } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $stage $_.Name) -Force
}
Get-ChildItem -LiteralPath (Join-Path $project 'assets\icons') -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $stage "assets\icons\$($_.Name)") -Force
}
Get-ChildItem -LiteralPath (Join-Path $project 'assets\fonts') -File -ErrorAction SilentlyContinue | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $stage "assets\fonts\$($_.Name)") -Force
}
$readme = $null
$manual = $null
Get-ChildItem -LiteralPath $project -File -Filter '*.txt' | ForEach-Object {
    $head = [System.IO.File]::ReadAllText($_.FullName)
    if ($head -match 'MANUAL.txt') { $readme = $_.FullName }
    elseif ($head -match 'unittest discover') { $manual = $_.FullName }
}
if (-not $readme -or -not $manual) { throw 'README/MANUAL source txt not found' }
[System.IO.File]::WriteAllText((Join-Path $stage 'README.txt'), [System.IO.File]::ReadAllText($readme), $utf8)
[System.IO.File]::WriteAllText((Join-Path $stage 'MANUAL.txt'), [System.IO.File]::ReadAllText($manual), $utf8)

$release = Join-Path $env:USERPROFILE 'Downloads\AIUsageWidget-release'
if (Test-Path -LiteralPath $release) { Remove-Item -LiteralPath $release -Recurse -Force }
New-Item -ItemType Directory -Path $release | Out-Null
$z1 = Join-Path $release 'AIUsageWidget.zip'
$z2 = Join-Path $env:USERPROFILE 'Downloads\AIUsageWidget.zip'
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $z1, [System.IO.Compression.CompressionLevel]::Optimal, $false)
Copy-Item -LiteralPath $z1 -Destination $z2 -Force
Copy-Item -LiteralPath (Join-Path $project 'latest.json') -Destination (Join-Path $release 'latest.json') -Force
$notesFile = Join-Path $release 'RELEASE_NOTES.md'
[System.IO.File]::WriteAllText($notesFile, $releaseNotes, $utf8)
Copy-Item -LiteralPath $changelogPath -Destination (Join-Path $release 'CHANGELOG.md') -Force

Write-Host "version $version"
Write-Host "zip $z2"
Write-Host "notes $notesFile"
if (-not $feed) {
    Write-Host 'feed_url.txt is empty; friends cannot auto-update until it points at latest.json'
} else {
    Write-Host "feed $feed"
    Write-Host "zip url $zipUrl"
}

$doGitHub = $GitHub
if (-not $doGitHub -and (Get-Command gh -ErrorAction SilentlyContinue)) { $doGitHub = $true }
$gh = $null
foreach ($candidate in @((Get-Command gh -ErrorAction SilentlyContinue).Source, "$env:ProgramFiles\GitHub CLI\gh.exe")) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) { $gh = $candidate; break }
}
if ($doGitHub -and $gh) {
    $tag = "v$version"
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $null = & $gh release view $tag --json tagName 2>$null
    $exists = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEap
    if ($exists) {
        & $gh release upload $tag $z1 (Join-Path $release 'latest.json') --clobber
        if ($LASTEXITCODE -ne 0) { throw "gh release upload failed for $tag" }
        & $gh release edit $tag --title $version --notes-file $notesFile
        if ($LASTEXITCODE -ne 0) { throw "gh release edit failed for $tag" }
        Write-Host "updated GitHub release $tag"
    } else {
        & $gh release create $tag $z1 (Join-Path $release 'latest.json') --title $version --notes-file $notesFile
        if ($LASTEXITCODE -ne 0) { throw "gh release create failed for $tag" }
        Write-Host "created GitHub release $tag"
    }
}
