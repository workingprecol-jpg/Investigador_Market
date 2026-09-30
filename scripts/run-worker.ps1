param([ValidateSet('paper','demo')][string]$Mode = 'paper')
$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectPath
& (Get-Command python.exe -ErrorAction Stop).Source -m bot run --mode $Mode
exit $LASTEXITCODE
