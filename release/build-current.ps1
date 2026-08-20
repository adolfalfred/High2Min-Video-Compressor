param(
    [string]$Python = "python",
    [string]$Output = "",
    [string]$SmokeVideo = "",
    [switch]$SkipUiSmoke
)

$ErrorActionPreference = "Stop"
$releaseDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDirectory = Split-Path -Parent $releaseDirectory
if (-not $Output) {
    $Output = Join-Path $projectDirectory "releases"
}
$temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("adt-video-native-build-" + [guid]::NewGuid().ToString("N"))

try {
    & $Python -m venv $temporary
    if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated build environment." }
    $venvPython = Join-Path $temporary "Scripts\python.exe"
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $releaseDirectory "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "Could not install pinned build dependencies." }
    $arguments = @((Join-Path $releaseDirectory "build_release.py"), "--output", $Output)
    if ($SmokeVideo) { $arguments += @("--smoke-video", $SmokeVideo) }
    if ($SkipUiSmoke) { $arguments += "--skip-ui-smoke" }
    & $venvPython @arguments
    if ($LASTEXITCODE -ne 0) { throw "Native release build or verification failed." }
}
finally {
    if (Test-Path -LiteralPath $temporary) {
        [System.IO.Directory]::Delete($temporary, $true)
    }
}
