# update.bat's path when this folder isn't a git checkout: download the latest main as a ZIP and copy
# it over this folder. The ZIP holds only the repo's files, so config.yaml, .env, data\league.json,
# logs\ and .venv\ are never touched. Files removed upstream stay behind (harmless).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
$zip = Join-Path $env:TEMP 'shotcast-update.zip'
$dir = Join-Path $env:TEMP 'shotcast-update'
Invoke-WebRequest 'https://github.com/jjmerri/gspro-shotcast/archive/refs/heads/main.zip' -OutFile $zip -UseBasicParsing
if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
Expand-Archive $zip $dir
$src = Get-ChildItem $dir -Directory | Select-Object -First 1
robocopy $src.FullName $root /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
Remove-Item $zip, $dir -Recurse -Force
Write-Host 'Copied the latest version.'
exit 0
