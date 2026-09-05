param(
    [Parameter(Mandatory=$true)][string]$ReleaseDirectory,
    [Parameter(Mandatory=$true)][string]$CertificateThumbprint
)
$ErrorActionPreference = 'Stop'
$directory = (Resolve-Path -LiteralPath $ReleaseDirectory).Path
$certificate = Get-Item -LiteralPath ('Cert:\CurrentUser\My\' + $CertificateThumbprint)
if (-not $certificate.HasPrivateKey) { throw 'The selected code-signing certificate has no private key.' }
$catalog = Join-Path $directory 'frontdesk-release.cat'
New-FileCatalog -Path $directory -CatalogFilePath $catalog -CatalogVersion 2.0 | Out-Null
$signedCatalog = Set-AuthenticodeSignature -FilePath $catalog -Certificate $certificate -TimestampServer 'http://timestamp.digicert.com'
if ($signedCatalog.Status -ne 'Valid') { throw ('Catalog signing failed: ' + $signedCatalog.StatusMessage) }
Get-ChildItem -LiteralPath $directory -Filter '*.ps1' -File | ForEach-Object {
    $result = Set-AuthenticodeSignature -FilePath $_.FullName -Certificate $certificate -TimestampServer 'http://timestamp.digicert.com'
    if ($result.Status -ne 'Valid') { throw ('Script signing failed: ' + $_.Name) }
}
Write-Output ('Signed release catalog: ' + $catalog)
