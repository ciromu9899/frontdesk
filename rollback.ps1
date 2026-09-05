param(
    [Parameter(Mandatory=$true)][string]$InstallDirectory,
    [Parameter(Mandatory=$true)][string]$BackupDirectory
)
$ErrorActionPreference = 'Stop'
$target = [System.IO.Path]::GetFullPath($InstallDirectory)
$backup = (Resolve-Path -LiteralPath $BackupDirectory).Path
$root = [System.IO.Path]::GetPathRoot($target)
$profile = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath('UserProfile'))
if ($target -eq $root -or $target.TrimEnd('\') -eq $profile.TrimEnd('\')) {
    throw 'InstallDirectory must be a dedicated application directory.'
}
if (-not ($backup.StartsWith($target + '.backup-', [System.StringComparison]::OrdinalIgnoreCase))) {
    throw 'BackupDirectory is not a backup of the requested installation.'
}
$failed = $target + '.failed-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $failed }
try {
    Move-Item -LiteralPath $backup -Destination $target
    Write-Output ('Rolled back FrontDesk to ' + $target)
    if (Test-Path -LiteralPath $failed) { Write-Output ('Replaced version retained at ' + $failed) }
} catch {
    if (-not (Test-Path -LiteralPath $target) -and (Test-Path -LiteralPath $failed)) {
        Move-Item -LiteralPath $failed -Destination $target
    }
    throw
}
