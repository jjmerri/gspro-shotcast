# Builds the Windows installer: dist\Shotcast-Setup-<version>.exe and its .sha256.
#   powershell -ExecutionPolicy Bypass -File tools\windows\build.ps1 -Version 1.2.3
# A private Python (python-build-standalone, which includes tkinter for the overlay) gets Shotcast
# and its packages, the OCR models are fetched into it, it's checked (tray --self-test and the test
# suite, run on that Python), and Inno Setup packs it. Needs Inno Setup 6 (iscc) and an internet connection.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$PythonMinor = "3.12",
    [string]$Extras = "capture,cloud,jev,publish,live,tray"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = Resolve-Path "$PSScriptRoot\..\.."
$Build = Join-Path $Root "build\windows"
$Python = Join-Path $Build "python\python.exe"

function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }

if (Test-Path $Build) { Remove-Item $Build -Recurse -Force }
New-Item -ItemType Directory $Build | Out-Null

Write-Host "Finding Python $PythonMinor (python-build-standalone)..."
$headers = @{ "User-Agent" = "shotcast-build" }
if ($env:GITHUB_TOKEN) { $headers["Authorization"] = "Bearer $env:GITHUB_TOKEN" }
$release = Invoke-RestMethod "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" -Headers $headers
$pattern = "^cpython-$([regex]::Escape($PythonMinor))\.\d+\+\d+-x86_64-pc-windows-msvc-install_only\.tar\.gz$"
$asset = $release.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1
if (-not $asset) { throw "No Python $PythonMinor Windows build in $($release.tag_name)" }
Write-Host "  $($asset.name)"
$archive = Join-Path $Build $asset.name
Invoke-WebRequest $asset.browser_download_url -OutFile $archive -Headers $headers
tar -xzf $archive -C $Build; Check "Unpacking Python"
Remove-Item $archive

Write-Host "Installing Shotcast $Version [$Extras]..."
# The version comes from the tag; pyproject.toml is put back afterwards.
$pyproject = Join-Path $Root "pyproject.toml"
$original = [IO.File]::ReadAllText($pyproject)
try {
    [IO.File]::WriteAllText($pyproject, ($original -replace '(?m)^version = "[^"]*"', "version = `"$Version`""))
    & $Python -m pip install --quiet --no-warn-script-location --upgrade pip; Check "pip upgrade"
    & $Python -m pip install --quiet --no-warn-script-location "$Root[$Extras]"; Check "pip install"
} finally {
    [IO.File]::WriteAllText($pyproject, $original)
}

Write-Host "Fetching the OCR models..."
& $Python -c "from rapidocr import RapidOCR; RapidOCR()"; Check "OCR models"

Write-Host "Checking the app..."
& $Python -m commentator tray --write-icon (Join-Path $Build "shotcast.ico"); Check "Icon"
$home_ = Join-Path $env:TEMP "shotcast-selftest-$PID"
& $Python -m commentator tray --home $home_ --self-test; Check "Self-test"
Remove-Item $home_ -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "Running the tests on the packaged Python..."
# Its packages are whatever pip resolved today, so a broken release of one fails the build here. The
# test runner goes in a throwaway venv layered over the bundle, so nothing is added to what ships.
$tests = Join-Path $env:TEMP "shotcast-tests-$PID"
& $Python -m venv --system-site-packages $tests; Check "Test venv"
$dev = & $Python -c "import tomllib; print(' '.join(tomllib.load(open(r'$pyproject', 'rb'))['project']['optional-dependencies']['dev']))"
& "$tests\Scripts\python.exe" -m pip install --quiet --no-warn-script-location $dev.Split(" "); Check "Test tools"
Push-Location $Root
try { & "$tests\Scripts\python.exe" -m pytest -q -p no:cacheprovider; Check "Tests" }
finally { Pop-Location; Remove-Item $tests -Recurse -Force -ErrorAction SilentlyContinue }
Get-ChildItem (Join-Path $Build "python") -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force

Write-Host "Building the installer..."
$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { throw "Inno Setup 6 isn't installed (https://jrsoftware.org/isdl.php)" }
& $iscc /Q "/DAppVersion=$Version" "/DBuildDir=$Build" (Join-Path $PSScriptRoot "shotcast.iss"); Check "Inno Setup"

$exe = Join-Path $Root "dist\Shotcast-Setup-$Version.exe"
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
[IO.File]::WriteAllText("$exe.sha256", "$hash  Shotcast-Setup-$Version.exe`n")
Write-Host ("Built {0} ({1:N0} MB)" -f $exe, ((Get-Item $exe).Length / 1MB))
