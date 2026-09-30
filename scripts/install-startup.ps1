param(
    [ValidateSet('paper','demo')][string]$Mode = 'paper',
    [switch]$Preview
)
$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonCommand = (Get-Command python.exe -ErrorAction Stop).Source
$pythonExecutable = & $pythonCommand -c 'import sys; print(sys.executable)'
if ($LASTEXITCODE -ne 0 -or -not $pythonExecutable) {
    throw 'No fue posible resolver el interprete Python instalado.'
}
$pythonPath = (Resolve-Path -LiteralPath ([string]$pythonExecutable).Trim()).Path
$windowlessPython = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $windowlessPython -PathType Leaf)) {
    throw 'El arranque programado sin ventana requiere pythonw.exe junto a python.exe. Instala Python para Windows con ese ejecutable; mientras tanto usa scripts/start-windows.ps1 para un proceso oculto.'
}
$windowlessPython = (Resolve-Path -LiteralPath $windowlessPython).Path
$runtimeArgs = '-m bot run --mode ' + $Mode + ' --config "' + (Join-Path $projectPath 'config.toml') + '"'
if ($Preview) {
    [pscustomobject]@{ TaskName = "FinalBoss-$Mode"; Execute = $windowlessPython; Arguments = $runtimeArgs; WorkingDirectory = $projectPath }
    return
}
# The scheduler owns the trading process itself, so stopping the task does not
# leave a Python child running after an intermediary PowerShell process exits.
$action = New-ScheduledTaskAction -Execute $windowlessPython -Argument $runtimeArgs -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName "FinalBoss-$Mode" -Action $action -Trigger $trigger -Settings $settings -Description 'Binance Demo futures monitoring; starts at user logon' -Force | Out-Null
Write-Output "Inicio automatico al iniciar sesion instalado: FinalBoss-$Mode. Ejecuta Python directamente, sin ventana. No evita suspension/apagado."
