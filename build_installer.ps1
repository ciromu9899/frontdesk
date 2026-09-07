param(
    [Parameter(Mandatory=$true)][string]$AppSource,
    [Parameter(Mandatory=$true)][string]$AppVersion,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [Parameter(Mandatory=$true)][string]$CertificateThumbprint
)
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $AppSource).Path
$signTool = (Get-Command signtool.exe -ErrorAction Stop).Source
$compiler = (Get-Command ISCC.exe -ErrorAction Stop).Source
if ($AppVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid version.' }
if ($CertificateThumbprint -notmatch '^[A-Fa-f0-9]{40}$') { throw 'Invalid certificate thumbprint.' }
if (-not (Test-Path -LiteralPath (Join-Path $source 'FrontDesk.exe'))) { throw 'FrontDesk.exe is missing.' }
function Sign-And-Verify([string]$file) {
    & $signTool sign /sha1 $CertificateThumbprint /fd SHA256 /tr 'http://timestamp.digicert.com' /td SHA256 $file
    if ($LASTEXITCODE -ne 0) { throw "Signing failed: $file" }
    & $signTool verify /pa /all /v $file
    if ($LASTEXITCODE -ne 0) { throw "Signature verification failed: $file" }
}
Get-ChildItem -LiteralPath $source -Recurse -File | Where-Object { $_.Extension -in '.exe','.dll','.pyd' } | ForEach-Object {
    Sign-And-Verify $_.FullName
}
$signCommand = '"' + $signTool + '" sign /sha1 ' + $CertificateThumbprint + ' /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $f'
& $compiler "/Sfrontdesk=$signCommand" "/DAppSource=$source" "/DAppVersion=$AppVersion" "/O$OutputDirectory" (Join-Path $PSScriptRoot 'packaging\windows.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
$installer = Join-Path $OutputDirectory "FrontDesk-Setup-$AppVersion.exe"
Sign-And-Verify $installer
Get-FileHash -LiteralPath $installer -Algorithm SHA256
