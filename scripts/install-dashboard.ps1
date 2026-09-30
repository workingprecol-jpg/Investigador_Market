param(
    [ValidateSet('paper','demo')][string]$Mode = 'paper',
    [ValidateRange(1,65535)][int]$Port = 8765,
    [switch]$Start,
    [switch]$Preview
)
$ErrorActionPreference = 'Stop'
if (-not $PSBoundParameters.ContainsKey('Port') -and $Mode -eq 'demo') { $Port = 8766 }
$projectPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonCommand = (Get-Command python.exe -ErrorAction Stop).Source
$pythonExecutable = & $pythonCommand -c 'import sys; print(sys.executable)'
if ($LASTEXITCODE -ne 0 -or -not $pythonExecutable) { throw 'No fue posible resolver Python.' }
$pythonPath = (Resolve-Path -LiteralPath ([string]$pythonExecutable).Trim()).Path
$windowlessPython = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $windowlessPython -PathType Leaf)) { throw 'Se requiere pythonw.exe para ejecutar el panel sin ventana.' }
$windowlessPython = (Resolve-Path -LiteralPath $windowlessPython).Path
$taskName = "FinalBoss-dashboard-$Mode"
$runtimeArgs = '-m bot dashboard --mode ' + $Mode + ' --port ' + $Port + ' --config "' + (Join-Path $projectPath 'config.toml') + '"'
if ($Preview) {
    [pscustomobject]@{ TaskName=$taskName; Execute=$windowlessPython; Arguments=$runtimeArgs; WorkingDirectory=$projectPath; Address="http://127.0.0.1:$Port" }
    return
}
$action = New-ScheduledTaskAction -Execute $windowlessPython -Argument $runtimeArgs -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'Panel local de solo lectura para Final Boss; no ejecuta operaciones' -Force | Out-Null
if ($Start) { Start-ScheduledTask -TaskName $taskName }
Write-Output "Panel local instalado: $taskName. Abre http://127.0.0.1:$Port. El motor de trading se supervisa por separado."
