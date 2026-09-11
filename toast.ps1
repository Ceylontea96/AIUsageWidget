$ErrorActionPreference = 'Stop'
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$key = [string]$payload.key
if ($key -notin @('chatgpt','cursor','test')) { exit 2 }
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotifier, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.UI.Notifications.NotificationSetting, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$title = [Security.SecurityElement]::Escape([string]$payload.title)
$body = [Security.SecurityElement]::Escape([string]$payload.body)
$url = if ($key -eq 'cursor') { 'https://cursor.com/dashboard/usage' } else { 'https://chatgpt.com/codex/settings/usage' }
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast activationType='protocol' launch='$url'><visual><binding template='ToastGeneric'><text>$title</text><text>$body</text></binding></visual></toast>")
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Jinho.AIUsageWidget')
$setting = $notifier.Setting
if ($null -ne $setting -and $setting.ToString() -ne 'Enabled') { Write-Output $setting; exit 3 }
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$toast.Tag = $key
$toast.Group = 'quota'
$toast.ExpirationTime = [DateTimeOffset]::Now.AddMinutes(10)
$notifier.Show($toast)
Write-Output 'SENT'
