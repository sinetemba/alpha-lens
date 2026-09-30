#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Removes the AlphaLens scheduled tasks.

.EXAMPLE
    .\scripts\uninstall_updater_task.ps1
#>
$ErrorActionPreference = "Stop"

$TaskNames = @("AlphaLensUpdater", "AlphaLensUpdaterLogon")

foreach ($TaskName in $TaskNames) {
    $Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $Existing) {
        Write-Host "Task '$TaskName' not found."
        continue
    }

    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task '$TaskName' removed."
}
