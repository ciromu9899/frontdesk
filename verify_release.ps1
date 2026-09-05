param(
    [Parameter(Mandatory=$true)][string]$PackagePath,
    [Parameter(Mandatory=$true)][string]$Sha256Path,
    [string]$CatalogPath
)
$ErrorActionPreference = 'Stop'
$package = (Resolve-Path -LiteralPath $PackagePath).Path
$expected = ((Get-Content -LiteralPath $Sha256Path -Raw).Trim() -split '\s+')[0].ToUpperInvariant()
$actual = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToUpperInvariant()
if ($actual -ne $expected) { throw 'FAIL: SHA-256 mismatch.' }
if ([string]::IsNullOrWhiteSpace($CatalogPath)) {
    throw 'FAIL: no signed catalog supplied. This is an unsigned release candidate.'
}
$signature = Get-AuthenticodeSignature -FilePath (Resolve-Path -LiteralPath $CatalogPath).Path
if ($signature.Status -ne 'Valid') { throw ('FAIL: catalog signature is ' + $signature.Status) }
$catalogStatus = Test-FileCatalog -Path (Split-Path -Parent $package) -CatalogFilePath (Resolve-Path -LiteralPath $CatalogPath).Path
if ($catalogStatus -ne 'Valid') { throw ('FAIL: catalog content is ' + $catalogStatus) }
Write-Output 'PASS: package hash and signed catalog are valid.'
