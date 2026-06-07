# Registers Rocky to auto-start at login via Windows Task Scheduler.
# Run once from an elevated PowerShell:
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_task.ps1
#
# Re-running updates the existing task. Use -Remove to unregister.

param(
    [string]$Repo = "C:\Repos\rocky_ai",
    [string]$TaskName = "RockyServer",
    [switch]$Remove
)

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$bat = Join-Path $Repo "scripts\windows\start_rocky.bat"
if (-not (Test-Path $bat)) { throw "Launcher not found: $bat" }

$action  = New-ScheduledTaskAction -Execute $bat
$trigger = New-ScheduledTaskTrigger -AtLogOn
# Keep it running quietly; restart if it dies.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Limited -Force | Out-Null

Write-Host "Registered '$TaskName' to run at login."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
