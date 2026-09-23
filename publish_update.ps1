#Requires -Version 5
param([switch]$GitHub)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding $false
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$updater = Get-Content -LiteralPath (Join-Path $project 'updater.py') -Raw
if ($updater -notmatch "APP_VERSION = '([^']+)'") { throw 'APP_VERSION not found' }
$version = $Matches[1]

function Assert-NewReleaseVersion {
    param([string]$Version, [string[]]$PublishedVersions)
    if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid release version' }
    foreach ($published in $PublishedVersions) {
        $plain = $published -replace '^v', ''
        if ($plain -notmatch '^\d+\.\d+\.\d+$') { throw "Unrecognized published version: $published" }
        if ([version]$Version -le [version]$plain) {
            throw "Refusing same/older version $Version (published $published)"
        }
    }
}

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

# Files the archive is built from. The source guard checks the same list, so
# nothing can ship that git does not have.
$copy = @(
    'usage_widget.py', 'providers.py', 'runtime.py', 'poll_worker.py', 'updater.py',
    'codex_activity.py', 'cursor_activity.py', 'LICENSE',
    'claude_bridge.py', 'claude_integration.py', 'CLAUDE_INTEGRATION.md', 'polling.py', 'additional_ui.py',
    'quota_policy.py', 'codex_app_server.py',
    'setup_and_run.ps1', 'setup_login.ps1', 'create_shortcut.ps1',
    'start_usage_widget.vbs', 'start_usage_widget.bat',
    'toast.ps1', 'register_notifications.ps1', 'feed_url.txt', 'CHANGELOG.md'
)
$packaged = $copy + @(
    ':(glob)*.bat', ':(glob)*.vbs', ':(glob)assets/**', ':(glob)launcher/**',
    'README.txt', 'MANUAL.txt', 'build_launcher.ps1'
)

function Get-FeedRepository {
    param([string]$Feed)
    $match = [regex]::Match($Feed, '^https://github\.com/([^/]+/[^/]+)/releases/')
    if ($match.Success) { return $match.Groups[1].Value }
    return ''
}

function Assert-PublishSourceCommitted {
    param([string]$Project, [string[]]$Packaged)
    # The archive is built from this folder and the release tag is created on
    # the commit returned here, so both must be the source GitHub already has.
    $git = @('-C', $Project, '-c', 'safe.directory=*')
    $changed = @(& git @git status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read git status; publishing stopped' }
    if ($changed.Count -gt 0) {
        throw ("Uncommitted changes; commit and push before publishing:`n" + ($changed -join "`n"))
    }
    # Ignored files count too: a packaged file must never ship from outside git.
    $untracked = @(& git @git ls-files --others -- @Packaged)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot list untracked files; publishing stopped' }
    if ($untracked.Count -gt 0) {
        throw ("Files would ship without being committed:`n" + ($untracked -join "`n"))
    }
    & git @git fetch --quiet origin main
    if ($LASTEXITCODE -ne 0) { throw 'Cannot fetch origin; publishing stopped' }
    $head = ([string](& git @git rev-parse HEAD)).Trim()
    $remote = ([string](& git @git rev-parse origin/main)).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $head -or -not $remote) {
        throw 'Cannot resolve HEAD and origin/main; publishing stopped'
    }
    if ($head -ne $remote) {
        throw "HEAD $head is not origin/main $remote; push or pull before publishing"
    }
    return $head
}

$feed = ''
$feedFile = Join-Path $project 'feed_url.txt'
foreach ($line in Get-Content -LiteralPath $feedFile) {
    $trim = $line.Trim()
    if ($trim -and -not $trim.StartsWith('#')) { $feed = $trim; break }
}
$repository = Get-FeedRepository $feed
# An explicit repo keeps gh from reading this folder's git config.
$repoArgs = if ($repository) { @('--repo', $repository) } else { @() }
$gh = $null
$sourceCommit = ''
if ($GitHub) {
    $sourceCommit = Assert-PublishSourceCommitted -Project $project -Packaged $packaged
    foreach ($candidate in @((Get-Command gh -ErrorAction SilentlyContinue).Source, "$env:ProgramFiles\GitHub CLI\gh.exe")) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { $gh = $candidate; break }
    }
    if (-not $gh) { throw 'GitHub CLI is required to verify published releases' }
    $published = @(& $gh release list --limit 100 --json tagName --jq '.[].tagName' @repoArgs)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot verify published versions; publishing stopped' }
    if ($feed) {
        $remote = Invoke-RestMethod -Uri $feed -ErrorAction Stop
        if (-not $remote.version) { throw 'Remote latest.json has no version' }
        $published += [string]$remote.version
    }
    Assert-NewReleaseVersion -Version $version -PublishedVersions $published
}
# Check remote releases before building or writing local artifacts.
& (Join-Path $project 'build_launcher.ps1')
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

$stage = Join-Path $project ('dist\stage-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $stage 'assets\icons') -Force | Out-Null
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
Copy-Item -LiteralPath (Join-Path $project 'assets\fonts') -Destination (Join-Path $stage 'assets\fonts') -Recurse
$readme = Join-Path $project 'README.txt'
$manual = Join-Path $project 'MANUAL.txt'
if (-not (Test-Path -LiteralPath $readme) -or -not (Test-Path -LiteralPath $manual)) {
    throw 'README/MANUAL source txt not found'
}
[System.IO.File]::WriteAllText((Join-Path $stage 'README.txt'), [System.IO.File]::ReadAllText($readme), $utf8)
[System.IO.File]::WriteAllText((Join-Path $stage 'MANUAL.txt'), [System.IO.File]::ReadAllText($manual), $utf8)

$release = Join-Path $project ('dist\release-' + $version + '-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $release | Out-Null
$z1 = Join-Path $release 'AIUsageWidget.zip'
$z2 = $z1
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $z1, [System.IO.Compression.CompressionLevel]::Optimal, $false)
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
if ($doGitHub -and $gh) {
    $tag = "v$version"
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $null = & $gh release view $tag --json tagName @repoArgs 2>$null
    $exists = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEap
    if ($exists) {
        throw "Release $tag already exists; artifacts will not be overwritten"
    } else {
        # Tag the commit the archive was built from, never whatever main is now.
        & $gh release create $tag $z1 (Join-Path $release 'latest.json') --title $version --notes-file $notesFile --target $sourceCommit @repoArgs
        if ($LASTEXITCODE -ne 0) { throw "gh release create failed for $tag" }
        Write-Host "created GitHub release $tag"
    }
}
