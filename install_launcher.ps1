$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$bin = Join-Path $project "bin"
$launcher = Join-Path $bin "hqin.cmd"

if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Launcher not found: $launcher"
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$entries = @($userPath -split ";" | Where-Object { $_ })

if ($entries -notcontains $bin) {
    $newPath = (@($entries) + $bin) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "Added to user PATH: $bin"
} else {
    Write-Host "Already on user PATH: $bin"
}

Write-Host ""
Write-Host "Open a new cmd/PowerShell window, cd into any project folder, then run:"
Write-Host "  hqin"
Write-Host ""
Write-Host "The current folder becomes the workspace. Use -w to override:"
Write-Host "  hqin -w D:\another\project"
