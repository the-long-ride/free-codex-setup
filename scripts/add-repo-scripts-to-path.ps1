Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptsDir = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$separator = [IO.Path]::PathSeparator
$entries = @()

if (-not [string]::IsNullOrWhiteSpace($userPath)) {
    $entries = $userPath -split [regex]::Escape([string] $separator)
}

if ($entries -contains $scriptsDir) {
    Write-Host "User PATH already contains $scriptsDir"
}
else {
    $newEntries = @($entries + $scriptsDir | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $newUserPath = $newEntries -join $separator
    [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
    Write-Host "Added $scriptsDir to the user PATH."
}

if (($env:Path -split [regex]::Escape([string] $separator)) -notcontains $scriptsDir) {
    $env:Path = if ([string]::IsNullOrWhiteSpace($env:Path)) {
        $scriptsDir
    }
    else {
        "$scriptsDir$separator$env:Path"
    }
}

Write-Host "Open a new terminal, then run: free-codex"
