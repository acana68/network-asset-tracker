#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Creates a Windows Task Scheduler task that runs a network scan every 30 minutes.

.DESCRIPTION
    The task runs "python -m tracker --log logs\scan.log scan" from the project
    folder with highest privileges (scapy/Npcap needs admin rights to send raw
    packets). It uses pythonw.exe so no console window pops up. Subnets and the
    timeout come from config.json.

    The task runs only while you're signed in, so it can use your
    DISCORD_WEBHOOK_URL user environment variable.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -IntervalMinutes 15
#>
param(
    [int]$IntervalMinutes = 30,
    [string]$Python,                       # path to pythonw.exe; found automatically if omitted
    [string]$TaskName = "AssetTrackerScan"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot

if (-not $Python) {
    $exe = (& python -c "import sys; print(sys.executable)").Trim()
    $Python = Join-Path (Split-Path $exe) "pythonw.exe"
    if (-not (Test-Path $Python)) { $Python = $exe }   # fall back to python.exe (shows a window)
}
if (-not (Test-Path $Python)) { throw "Python not found at $Python. Pass -Python <path to pythonw.exe>." }

$action = New-ScheduledTaskAction -Execute $Python `
    -Argument "-m tracker --log logs\scan.log scan" `
    -WorkingDirectory $ProjectDir

# Start a minute from now, then repeat forever
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force `
    -Description "Network asset tracker: ARP scan every $IntervalMinutes minutes" | Out-Null

Write-Host "Created task '$TaskName': scans every $IntervalMinutes minutes."
Write-Host "  Python:  $Python"
Write-Host "  Folder:  $ProjectDir"
Write-Host "  Log:     $ProjectDir\logs\scan.log"

if (-not (Test-Path (Join-Path $ProjectDir "config.json"))) {
    Write-Warning "No config.json found, so scans will auto-detect a single /24 subnet. Copy config.example.json to config.json."
}
if (-not [Environment]::GetEnvironmentVariable("DISCORD_WEBHOOK_URL", "User")) {
    Write-Warning "DISCORD_WEBHOOK_URL isn't set as a user environment variable, so scheduled scans won't send alerts."
}
Write-Host "Run it now with:  Start-ScheduledTask -TaskName $TaskName"
