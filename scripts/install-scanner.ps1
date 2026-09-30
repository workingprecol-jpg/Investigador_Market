param([switch]$Start, [switch]$Preview)
$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath (Join-Path $projectPath 'ANALYSIS_ONLY'))) {
    throw 'Se requiere la politica ANALYSIS_ONLY antes de instalar el buscador.'
}
$pythonCommand = (Get-Command python.exe -ErrorAction Stop).Source
$pythonExecutable = & $pythonCommand -c 'import sys; print(sys.executable)'
if ($LASTEXITCODE -ne 0 -or -not $pythonExecutable) { throw 'No se pudo resolver Python.' }
$pythonPath = Join-Path (Split-Path -Parent ([string]$pythonExecutable).Trim()) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'Se requiere pythonw.exe.' }
$scanPath = Join-Path $projectPath 'data\scanner.json'
$definitions = @(
    @{ Name = 'FinalBoss-scanner'; Arguments = '-m bot scan --output "' + $scanPath + '"' },
    @{ Name = 'FinalBoss-dashboard-analysis'; Arguments = '-m bot scan-dashboard --port 8765 --output "' + $scanPath + '"' }
)
if ($Preview) {
    foreach ($definition in $definitions) {
        [pscustomobject]@{TaskName=$definition.Name; Execute=$pythonPath; Arguments=$definition.Arguments; WorkingDirectory=$projectPath}
    }
    return
}
# Keep the historical tasks registered but prevent their login triggers.
foreach ($legacyName in @('FinalBoss-paper', 'FinalBoss-dashboard-paper')) {
    $legacy = Get-ScheduledTask -TaskName $legacyName -ErrorAction SilentlyContinue
    if ($legacy) {
        if (@($legacy.Actions | Where-Object { $_.WorkingDirectory -eq $projectPath }).Count -ne @($legacy.Actions).Count) {
            throw "La tarea $legacyName no corresponde a este proyecto."
        }
        if ($legacyName -eq 'FinalBoss-paper') {
            Push-Location $projectPath
            try {
                $state = & $pythonCommand -m bot status --mode paper | ConvertFrom-Json
                if ($LASTEXITCODE -ne 0 -or @($state.positions.PSObject.Properties).Count -gt 0 -or $state.pending_orders -ne 0) {
                    throw 'El motor debe estar plano antes de instalar el buscador.'
                }
            } finally { Pop-Location }
        }
        Disable-ScheduledTask -TaskName $legacyName | Out-Null
        Stop-ScheduledTask -TaskName $legacyName
    }
}
$userIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userIdentity
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $userIdentity -LogonType Interactive -RunLevel Limited
foreach ($definition in $definitions) {
    $existing = Get-ScheduledTask -TaskName $definition.Name -ErrorAction SilentlyContinue
    if ($existing) {
        if (@($existing.Actions | Where-Object { $_.WorkingDirectory -eq $projectPath }).Count -ne @($existing.Actions).Count) {
            throw "La tarea $($definition.Name) no corresponde a este proyecto."
        }
        Disable-ScheduledTask -TaskName $definition.Name | Out-Null
        Stop-ScheduledTask -TaskName $definition.Name
        $workerDeadline = [DateTime]::UtcNow.AddSeconds(15)
        do {
            $workerProcesses = @(Get-CimInstance Win32_Process | Where-Object {
                $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -and $_.CommandLine.Contains($definition.Arguments)
            })
            if ($workerProcesses.Count -eq 0) { break }
            Start-Sleep -Milliseconds 500
        } while ([DateTime]::UtcNow -lt $workerDeadline)
        if ($workerProcesses.Count -ne 0) { throw "La instancia anterior de $($definition.Name) sigue terminando." }
    }
    $action = New-ScheduledTaskAction -Execute $pythonPath -Argument $definition.Arguments -WorkingDirectory $projectPath
    Register-ScheduledTask -TaskName $definition.Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'FinalBoss: investigacion publica sin operaciones ni credenciales.' -Force | Out-Null
    Enable-ScheduledTask -TaskName $definition.Name | Out-Null
    if ($Start) { Start-ScheduledTask -TaskName $definition.Name }
    Write-Output "Tarea de solo analisis instalada: $($definition.Name)"
}
