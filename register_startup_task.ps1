$ErrorActionPreference = 'Stop'

$projectDir = Split-Path -Parent $PSCommandPath
$launcher = Join-Path $projectDir 'ultron_startup.vbs'
$taskName = 'ULTRON Voice Assistant'

if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Launcher not found: $launcher"
}

$taskCommand = 'wscript.exe //B //Nologo "' + $launcher + '"'
schtasks.exe /Create /TN $taskName /TR $taskCommand /SC ONLOGON /RL LIMITED /F | Out-Host
Write-Host "Registered '$taskName'. It starts at your next sign-in."
Write-Host "Start now: schtasks.exe /Run /TN '$taskName'"
Write-Host "Stop now:  schtasks.exe /End /TN '$taskName'"
Write-Host "Remove it:  schtasks.exe /Delete /TN '$taskName' /F"
