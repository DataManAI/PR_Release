# Registers a Windows Scheduled Task that runs the monitor at logon and keeps it running.
# Review before running: creates a persistent background task under your user account.

$scriptDir = $PSScriptRoot
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $pythonw = (Get-Command python.exe).Source
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "monitor.py" -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName "PressReleaseMonitor" -Action $action -Trigger $trigger -Settings $settings -Description "Monitors PR wire feeds and emails matches"

Write-Host "Task 'PressReleaseMonitor' registered. It will start at your next logon."
Write-Host "To start it immediately: Start-ScheduledTask -TaskName 'PressReleaseMonitor'"
Write-Host "To remove it: Unregister-ScheduledTask -TaskName 'PressReleaseMonitor' -Confirm:`$false"
