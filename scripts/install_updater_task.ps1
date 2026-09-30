#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installs the AlphaLens Windows Scheduled Task.

.DESCRIPTION
    Creates two tasks under the current user:
      - "AlphaLensUpdater" starts the headless updater daemon at logon and
        keeps it running. The daemon itself schedules 09:00, 13:00, 17:00
        price/news updates, an 18:00 full update and a Sunday 02:00 cleanup.
      - "AlphaLensUpdaterLogon" runs a lightweight debounced refresh at logon
        and on workstation unlock (lock/unlock is not a logon event, so it
        needs its own SessionStateChange trigger).

    This is the recommended deployment method on Windows because Task Scheduler
    handles startup/logon, network unavailability and battery/AC power without
    you writing a Windows Service by hand.

.EXAMPLE
    .\scripts\install_updater_task.ps1
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$Script = Join-Path $RepoRoot "scripts\run_updater.py"

function Find-Python($Name) {
    # Prefer the project venv, then any activated/available interpreter.
    $VenvPath = Join-Path $RepoRoot ".venv\Scripts\$Name.exe"
    if (Test-Path $VenvPath) { return $VenvPath }

    $VenvPath = Join-Path $RepoRoot "venv\Scripts\$Name.exe"
    if (Test-Path $VenvPath) { return $VenvPath }

    $Cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($Cmd) { return $Cmd.Source }
    return $null
}

$Python = Find-Python "python"
$PythonW = Find-Python "pythonw"

if (-not $Python) {
    throw "Could not find python.exe. Make sure the virtual environment exists or Python is on PATH."
}

$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RunOnlyIfNetworkAvailable:$false

# Daemon task: starts at logon and runs forever.
$DaemonAction = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`"" -WorkingDirectory $RepoRoot
if ($PythonW) {
    # pythonw avoids a console window for the long-running daemon.
    $DaemonAction = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$Script`"" -WorkingDirectory $RepoRoot
}
$LogonTrigger = New-ScheduledTaskTrigger -AtLogon
$DaemonTask = New-ScheduledTask -Action $DaemonAction -Trigger $LogonTrigger -Principal $Principal -Settings $Settings -Description "AlphaLens headless updater daemon"
Register-ScheduledTask -TaskName "AlphaLensUpdater" -InputObject $DaemonTask -Force | Out-Null

# Logon refresh task: runs once at logon and on workstation unlock,
# debounced in the Python code.
$RefreshAction = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`" --logon" -WorkingDirectory $RepoRoot

# Lock/unlock is not a logon, so -AtLogon does not cover it. Build a
# SessionStateChange trigger via CIM (New-ScheduledTaskTrigger cannot).
# StateChange: 7 = SessionLock, 8 = SessionUnlock.
$TriggerClass = Get-CimClass -ClassName MSFT_TaskSessionStateChangeTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$UnlockTrigger = New-CimInstance -CimClass $TriggerClass -Property @{
    Enabled     = $true
    StateChange = 8
    UserId      = "$env:USERDOMAIN\$env:USERNAME"
} -ClientOnly

$RefreshTask = New-ScheduledTask -Action $RefreshAction -Trigger @($LogonTrigger, $UnlockTrigger) -Principal $Principal -Settings $Settings -Description "AlphaLens logon/unlock refresh"
Register-ScheduledTask -TaskName "AlphaLensUpdaterLogon" -InputObject $RefreshTask -Force | Out-Null

Write-Host "AlphaLensUpdater and AlphaLensUpdaterLogon scheduled tasks installed successfully."
Write-Host "The daemon will start the next time you log on."
Write-Host "Run 'schtasks /run /tn AlphaLensUpdater' to start the daemon now."
