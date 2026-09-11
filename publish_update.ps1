$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding $true
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$updater = Get-Content -LiteralPath (Join-Path $project 'updater.py') -Raw
if ($updater -notmatch "APP_VERSION = '([^']+)'") { throw 'APP_VERSION not found' }
$version = $Matches[1]
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
$latest = [ordered]@{
    version = $version
    zip = $zipUrl
    notes = "AI Usage $version"
} | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText((Join-Path $project 'latest.json'), $latest + "`n", $utf8)

$stage = Join-Path $env:TEMP 'AIUsageWidget_dist_stage'
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $stage 'assets\icons') -Force | Out-Null
$copy = @(
    'usage_widget.py','providers.py','runtime.py','poll_worker.py','updater.py',
    'setup_and_run.ps1','setup_login.ps1','start_usage_widget.vbs','start_usage_widget.bat',
    'toast.ps1','register_notifications.ps1','feed_url.txt'
)
foreach ($f in $copy) {
    Copy-Item -LiteralPath (Join-Path $project $f) -Destination (Join-Path $stage $f) -Force
}
Get-ChildItem -LiteralPath (Join-Path $project 'assets\icons') -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $stage "assets\icons\$($_.Name)") -Force
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

Write-Host "version $version"
Write-Host "zip $z2"
if (-not $feed) {
    Write-Host 'feed_url.txt에 latest.json 공개 주소를 넣은 뒤 다시 실행하세요.'
    Write-Host '같은 폴더에 latest.json 과 AIUsageWidget.zip 을 올리면 됩니다.'
} else {
    Write-Host "feed $feed"
    Write-Host "zip url $zipUrl"
    Write-Host '이 두 파일을 그 주소가 가리키는 폴더에 올리면 친구 위젯에서 업데이트가 켜집니다.'
}
