param([switch]$Preview)

$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonCommand = (Get-Command python.exe -ErrorAction Stop).Source
$pythonExecutable = & $pythonCommand -c 'import sys; print(sys.executable)'
if ($LASTEXITCODE -ne 0 -or -not $pythonExecutable) {
    throw 'No fue posible resolver el interprete Python instalado.'
}
$pythonPath = (Resolve-Path -LiteralPath ([string]$pythonExecutable).Trim()).Path
$windowlessPython = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
if (Test-Path -LiteralPath $windowlessPython -PathType Leaf) {
    $pythonPath = (Resolve-Path -LiteralPath $windowlessPython).Path
}

$profiles = @('trend', 'swing', 'scalping')
$dataPath = Join-Path $projectPath 'data'
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null

foreach ($profile in $profiles) {
    $configPath = Join-Path $projectPath ("configs\$profile.toml")
    $statePath = Join-Path $dataPath ("$profile.sqlite3")
    $arguments = '-m bot run --mode paper --config "' + $configPath + '" --state "' + $statePath + '"'
    if ($Preview) {
        [pscustomobject]@{ Profile = $profile; Execute = $pythonPath; Arguments = $arguments; WorkingDirectory = $projectPath }
        continue
    }
    $activityPath = [System.IO.Path]::ChangeExtension($statePath, '.activity.json')
    $alreadyHealthy = $false
    if (Test-Path -LiteralPath $activityPath -PathType Leaf) {
        try {
            $activity = Get-Content -LiteralPath $activityPath -Raw | ConvertFrom-Json
            $process = Get-Process -Id ([int]$activity.pid) -ErrorAction SilentlyContinue
            $alreadyHealthy = $null -ne $process -and (([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$activity.updated_at) -le 180)
        } catch {
            $alreadyHealthy = $false
        }
    }
    if ($alreadyHealthy) {
        Write-Output "Perfil $profile ya esta activo y saludable."
        continue
    }
    $process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectPath -WindowStyle Hidden -PassThru
    $process.Id | Set-Content -LiteralPath (Join-Path $dataPath ("$profile.pid"))
    Write-Output "Perfil $profile iniciado con PID $($process.Id)."
}
