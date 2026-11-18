<#
.SYNOPSIS
    Stops and removes the AlphaLensDashboard scheduled task.

.EXAMPLE
    .\scripts\uninstall_dashboard_task.ps1
#>
$ErrorActionPreference = "Stop"

$TaskName = "AlphaLensDashboard"
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $Existing) {
    Write-Host "Task '$TaskName' not found."
    return
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Task '$TaskName' removed."
