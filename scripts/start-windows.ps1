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
if (Test-Path -LiteralPath $windowlessPython -PathType Leaf) {
    $pythonPath = (Resolve-Path -LiteralPath $windowlessPython).Path
}
$runtimeArgs = '-m bot run --mode ' + $Mode + ' --config "' + (Join-Path $projectPath 'config.toml') + '"'
if ($Preview) {
    [pscustomobject]@{ Execute = $pythonPath; Arguments = $runtimeArgs; WorkingDirectory = $projectPath }
    return
}
$dataPath = Join-Path $projectPath 'data'
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
$process = Start-Process -FilePath $pythonPath -ArgumentList $runtimeArgs -WorkingDirectory $projectPath -WindowStyle Hidden -PassThru
$process.Id | Set-Content -LiteralPath (Join-Path $dataPath ($Mode + '.pid'))
Write-Output "Proceso $($process.Id) iniciado. Verificar con: python -m bot health --mode $Mode"
