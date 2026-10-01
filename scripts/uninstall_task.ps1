#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Removes the scheduled scan task created by install_task.ps1.
    Your database, config, and logs are left alone.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall_task.ps1
#>
param(
    [string]$TaskName = "AssetTrackerScan"
)

$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task '$TaskName'."
} else {
    Write-Host "No task named '$TaskName' found. Nothing to remove."
}
