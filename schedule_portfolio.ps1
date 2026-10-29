# Registers a Windows scheduled task: run_portfolio.bat every Tue-Sat at 07:00 (Bangkok time),
# i.e. the morning after each US trading day (US close = 03:00/04:00 Bangkok).
# Run once:  powershell -ExecutionPolicy Bypass -File .\schedule_portfolio.ps1
# Remove:    Unregister-ScheduledTask -TaskName "Portfolio Stage" -Confirm:$false
param([string]$At = "7:00AM")

$bat = Join-Path $PSScriptRoot "run_portfolio.bat"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday,Wednesday,Thursday,Friday,Saturday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "Portfolio Stage" -Action $action -Trigger $trigger -Settings $settings `
    -Description "portfolio_stage run --telegram (alerts.md -> Telegram)" -Force | Out-Null
Write-Host "Scheduled 'Portfolio Stage' Tue-Sat at $At. Test now with: Start-ScheduledTask -TaskName 'Portfolio Stage'"
