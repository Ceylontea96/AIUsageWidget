$ErrorActionPreference = 'Stop'
$widgetDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $widgetDir 'usage_widget.py'
$candidateCommand = Get-Command pythonw -ErrorAction SilentlyContinue
$pythonwPath = if ($candidateCommand) { $candidateCommand.Source } else { $null }
if (-not $pythonwPath) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $pythonPath = & $launcher.Source -3 -c 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -eq 0) {
            $candidatePath = Join-Path (Split-Path $pythonPath) 'pythonw.exe'
            if (Test-Path -LiteralPath $candidatePath) { $pythonwPath = $candidatePath }
        }
    }
}
if (-not $pythonwPath) { throw 'Python 3.9 이상과 Tk가 필요합니다.' }
$startupPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\AIUsageWidget.vbs'
$content = @"
Set sh = CreateObject("Wscript.Shell")
sh.CurrentDirectory = "$widgetDir"
sh.Run """$pythonwPath"" ""$script""", 0, False
"@
[System.IO.File]::WriteAllText($startupPath, $content, [System.Text.Encoding]::Unicode)
Write-Host 'AI Usage 시작 실행을 등록했습니다.'
