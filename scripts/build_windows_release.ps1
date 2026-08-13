[CmdletBinding()]
param(
    [switch]$SkipExecutableBuild,
    [switch]$SkipInstaller,
    [string]$Python = ".venv\Scripts\python.exe",
    [string]$IsccPath = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$versionOutput = & $Python -c "from snla.version import APP_VERSION; print(APP_VERSION)"
if ($LASTEXITCODE -ne 0 -or -not $versionOutput) {
    throw "Unable to read the StatsTalk version."
}
$version = $versionOutput.Trim()
if ($version -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$') {
    throw "Invalid release version: $version"
}

$distDir = Join-Path $projectRoot "dist"
$releaseDir = Join-Path $projectRoot "release"
$exePath = Join-Path $distDir "StatsTalk.exe"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null

if (-not $SkipExecutableBuild) {
    & $Python -m PyInstaller snla.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
}
if (-not (Test-Path -LiteralPath $exePath)) { throw "Missing executable: $exePath" }

$previousProbeData = $env:STATSTALK_PORTABLE_DATA_DIR
$probeData = Join-Path $releaseDir ".probe-data"
$env:STATSTALK_PORTABLE_DATA_DIR = $probeData
$versionProbe = & $exePath --version
$probeExitCode = $LASTEXITCODE
if ($null -eq $previousProbeData) {
    Remove-Item Env:STATSTALK_PORTABLE_DATA_DIR -ErrorAction SilentlyContinue
} else {
    $env:STATSTALK_PORTABLE_DATA_DIR = $previousProbeData
}
if (Test-Path -LiteralPath $probeData) { Remove-Item -Recurse -Force -LiteralPath $probeData }
if ($probeExitCode -ne 0 -or $versionProbe.Trim() -ne "StatsTalk $version") {
    throw "Executable version probe failed: $versionProbe"
}
$exeSize = (Get-Item -LiteralPath $exePath).Length
if ($exeSize -lt 20MB -or $exeSize -gt 500MB) {
    throw "Unexpected executable size: $exeSize bytes"
}

$portableName = "StatsTalk-$version-windows-x64-portable"
$portableStage = Join-Path $releaseDir $portableName
$portableZip = Join-Path $releaseDir "$portableName.zip"
if (Test-Path -LiteralPath $portableStage) { Remove-Item -Recurse -Force -LiteralPath $portableStage }
New-Item -ItemType Directory -Path $portableStage | Out-Null
Copy-Item -LiteralPath $exePath -Destination (Join-Path $portableStage "StatsTalk.exe")
Copy-Item -LiteralPath "packaging\PORTABLE_README.txt" -Destination (Join-Path $portableStage "README.txt")
New-Item -ItemType File -Path (Join-Path $portableStage "portable.marker") | Out-Null
if (Test-Path -LiteralPath $portableZip) { Remove-Item -Force -LiteralPath $portableZip }
Compress-Archive -Path (Join-Path $portableStage "*") -DestinationPath $portableZip -CompressionLevel Optimal
Remove-Item -Recurse -Force -LiteralPath $portableStage

if (-not $SkipInstaller) {
    if (-not $IsccPath) {
        $candidates = @(
            (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
        $IsccPath = $candidates | Select-Object -First 1
    }
    if (-not $IsccPath) { throw "Inno Setup 6 ISCC.exe was not found." }
    & $IsccPath "/DAppVersion=$version" "/DSourceDir=$distDir" "/DOutputDir=$releaseDir" "packaging\StatsTalk.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed." }
}

$artifacts = Get-ChildItem -LiteralPath $releaseDir -File | Where-Object {
    $_.Name -eq "$portableName.zip" -or $_.Name -eq "StatsTalk-$version-windows-x64-setup.exe"
} | Sort-Object Name
if (-not $SkipInstaller -and $artifacts.Count -ne 2) { throw "Expected installer and portable ZIP." }
if ($SkipInstaller -and $artifacts.Count -lt 1) { throw "Expected portable ZIP." }

$checksumPath = Join-Path $releaseDir "SHA256SUMS.txt"
$checksumLines = foreach ($artifact in $artifacts) {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $artifact.FullName).Hash.ToLowerInvariant()
    "$hash  $($artifact.Name)"
}
Set-Content -LiteralPath $checksumPath -Value $checksumLines -Encoding ascii

$manifest = [ordered]@{
    version = $version
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    executable_size = $exeSize
    artifacts = @($artifacts | ForEach-Object {
        [ordered]@{
            name = $_.Name
            size = $_.Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
        }
    })
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $releaseDir "release-manifest.json") -Encoding utf8
$manifest | ConvertTo-Json -Depth 4
