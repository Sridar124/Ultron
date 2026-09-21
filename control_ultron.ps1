param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Start', 'Stop', 'Restart', 'Status')]
    [string]$Action
)

$projectDir = Split-Path -Parent $PSCommandPath
$launcher = Join-Path $projectDir 'ultron_startup.vbs'
$disabledMarker = Join-Path $projectDir 'ultron.disabled'
$backgroundInstances = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*ultron.py*--background*' }

switch ($Action) {
    'Status' {
        if ($backgroundInstances) {
            $backgroundInstances | Select-Object ProcessId, Name, CommandLine
        } else {
            Write-Host 'ULTRON is not running.'
        }
    }
    'Stop' {
        New-Item -ItemType File -Path $disabledMarker -Force | Out-Null
        $backgroundInstances | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        Write-Host 'ULTRON background process stopped.'
    }
    'Start' {
        if ($backgroundInstances) { Write-Host 'ULTRON is already running.'; break }
        Remove-Item -LiteralPath $disabledMarker -Force -ErrorAction SilentlyContinue
        Start-Process -FilePath 'wscript.exe' -ArgumentList @('//B', '//Nologo', $launcher) -WindowStyle Hidden
        Write-Host 'ULTRON startup launcher started.'
    }
    'Restart' {
        New-Item -ItemType File -Path $disabledMarker -Force | Out-Null
        $backgroundInstances | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        Start-Sleep -Seconds 1
        Remove-Item -LiteralPath $disabledMarker -Force -ErrorAction SilentlyContinue
        Start-Process -FilePath 'wscript.exe' -ArgumentList @('//B', '//Nologo', $launcher) -WindowStyle Hidden
        Write-Host 'ULTRON restarted.'
    }
}
