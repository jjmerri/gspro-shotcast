# Prints the folder GSPro is installed in (the one holding Core\GSP\GSPro.exe), or nothing.
# Used by the installer's GSPro page and the Windows app's Settings. Looks at, in order: a running
# GSPro, Start menu and desktop shortcuts to it or its launcher, and \GSProV1 on each drive.
param([string]$Out)
$ErrorActionPreference = "SilentlyContinue"
$found = @()
Get-Process GSPro | ForEach-Object { if ($_.Path) { $found += (Split-Path (Split-Path (Split-Path $_.Path))) } }
$shell = New-Object -ComObject WScript.Shell
$places = "Programs", "CommonPrograms", "Desktop", "CommonDesktopDirectory" | ForEach-Object { [Environment]::GetFolderPath($_) }
Get-ChildItem $places -Recurse -Filter *.lnk | Where-Object Name -match "GSP" | ForEach-Object {
    $target = $shell.CreateShortcut($_.FullName).TargetPath
    if ($target) { $found += (Split-Path $target); $found += (Split-Path (Split-Path (Split-Path $target))) }
}
Get-PSDrive -PSProvider FileSystem | ForEach-Object { $found += (Join-Path $_.Root "GSProV1") }
$dir = $found | Where-Object { $_ -and (Test-Path -LiteralPath (Join-Path $_ "Core\GSP\GSPro.exe")) } | Select-Object -First 1
# The installer reads the file as ANSI.
if ($Out) { [IO.File]::WriteAllText($Out, [string]$dir, [Text.Encoding]::Default) } else { $dir }
