<#
.SYNOPSIS
    Installs the AlphaLens dashboard as a Windows Scheduled Task.

.DESCRIPTION
    Creates "AlphaLensDashboard" under the current user: starts the Streamlit
    dashboard at logon (via scripts\run_dashboard.py, with the project venv's
    pythonw.exe - no console window) and restarts it if it crashes.

    Unlike the updater task this runs WITHOUT elevation: a web server has no
    reason to run as administrator, so no admin prompt is needed to install.

    The dashboard binds to STREAMLIT_SERVER_ADDRESS from .env (default
    localhost - this PC only). Output goes to data\logs\dashboard.log.

.EXAMPLE
    .\scripts\install_dashboard_task.ps1
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$Script = Join-Path $RepoRoot "scripts\run_dashboard.py"

# The venv only: running the dashboard on whatever Python is on PATH is how it
# ended up on an untested Streamlit version.
$PythonW = Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $PythonW)) { $PythonW = Join-Path $RepoRoot "venv\Scripts\pythonw.exe" }
if (-not (Test-Path $PythonW)) {
    throw "Project virtual environment not found (.venv or venv). Create it and pip install -r requirements.txt first."
}

$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew

$Action = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$Script`"" -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings `
    -Description "AlphaLens Streamlit dashboard (http://localhost:8501)"
Register-ScheduledTask -TaskName "AlphaLensDashboard" -InputObject $Task -Force | Out-Null

Write-Host "AlphaLensDashboard scheduled task installed - the dashboard starts at every logon."
Write-Host "Run 'schtasks /run /tn AlphaLensDashboard' to start it now (stop any other dashboard first)."
