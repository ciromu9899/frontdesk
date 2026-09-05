param(
    [Parameter(Mandatory=$true)][string]$PackagePath,
    [Parameter(Mandatory=$true)][string]$Sha256Path,
    [Parameter(Mandatory=$true)][string]$CatalogPath,
    [Parameter(Mandatory=$true)][string]$InstallDirectory
)
$ErrorActionPreference = 'Stop'
$package = (Resolve-Path -LiteralPath $PackagePath).Path
$hashFile = (Resolve-Path -LiteralPath $Sha256Path).Path
$catalog = (Resolve-Path -LiteralPath $CatalogPath).Path
$signature = Get-AuthenticodeSignature -FilePath $catalog
if ($signature.Status -ne 'Valid') { throw ('Release catalog signature is ' + $signature.Status) }
$catalogStatus = Test-FileCatalog -Path (Split-Path -Parent $package) -CatalogFilePath $catalog
if ($catalogStatus -ne 'Valid') { throw ('Release catalog content is ' + $catalogStatus) }
$target = [System.IO.Path]::GetFullPath($InstallDirectory)
$profile = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath('UserProfile'))
$root = [System.IO.Path]::GetPathRoot($target)
if ($target -eq $root -or $target.TrimEnd('\') -eq $profile.TrimEnd('\')) {
    throw 'InstallDirectory must be a dedicated application directory, not a drive or profile root.'
}
$expected = ((Get-Content -LiteralPath $hashFile -Raw).Trim() -split '\s+')[0].ToUpperInvariant()
$actual = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToUpperInvariant()
if ($actual -ne $expected) { throw 'Release SHA-256 does not match.' }
$parent = Split-Path -Parent $target
if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$staging = Join-Path $parent ('.frontdesk-staging-' + $stamp)
$backup = $target + '.backup-' + $stamp
New-Item -ItemType Directory -Path $staging | Out-Null
try {
    Expand-Archive -LiteralPath $package -DestinationPath $staging
    $source = Join-Path $staging 'frontdesk'
    if (-not (Test-Path -LiteralPath (Join-Path $source 'RELEASE-MANIFEST.json'))) {
        throw 'Release manifest is missing from the package.'
    }
    if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $backup }
    Move-Item -LiteralPath $source -Destination $target
    Write-Output ('Installed FrontDesk to ' + $target)
    if (Test-Path -LiteralPath $backup) { Write-Output ('Rollback backup: ' + $backup) }
} catch {
    if (-not (Test-Path -LiteralPath $target) -and (Test-Path -LiteralPath $backup)) {
        Move-Item -LiteralPath $backup -Destination $target
    }
    throw
} finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
