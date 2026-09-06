# Signed release procedure

General-availability distribution requires an organisation-controlled Windows
code-signing certificate. An unsigned ZIP is a release candidate only.

1. Run the full tests, dependency audit, static security scan and quality gates.
2. Build into a new, dedicated release directory:

   ```powershell
   python build_release.py --output C:\release\frontdesk-1.4.3 --tests-passed <FULL_TEST_COUNT> --allow-unsigned
   ```

3. Copy `install.ps1`, `rollback.ps1`, and `verify_release.ps1` into that dedicated
   directory before signing. Do not add or modify files after the catalog is made.
4. As the certificate owner, sign the directory without exposing the private key:

   ```powershell
   .\sign_release.ps1 -ReleaseDirectory C:\release\frontdesk-1.4.3 -CertificateThumbprint <APPROVED_CODE_SIGNING_CERTIFICATE_THUMBPRINT>
   ```

5. Verify the ZIP hash, Authenticode signature and catalog membership:

   ```powershell
   .\verify_release.ps1 -PackagePath <ZIP> -Sha256Path <ZIP.sha256> -CatalogPath .\frontdesk-release.cat
   ```

6. Install only after verification. The installer repeats every verification
   before changing the destination:

   ```powershell
   .\install.ps1 -PackagePath <ZIP> -Sha256Path <ZIP.sha256> -CatalogPath .\frontdesk-release.cat -InstallDirectory C:\Apps\FrontDesk
   ```

The installer retains the previous installation as a timestamped sibling. Use
`rollback.ps1` with that exact directory if acceptance checks fail. Never point
install or rollback at a drive root or user-profile root.

Record the build hash, certificate subject and expiry, timestamp status, signer,
verification output, acceptance result and rollback result in the release ticket.

## Cross-platform GitHub attestation

For public GitHub releases, run the `attest release` workflow after publishing
the ZIP. The workflow checks out the requested tag, reproduces the deterministic
archive, requires its SHA-256 digest to match the published checksum, and then
uses GitHub's Sigstore-backed identity to sign its build provenance.

After downloading the ZIP, verify that signature and repository identity with:

```powershell
gh attestation verify .\frontdesk-complete-1.7.0-2026-09-05.zip -R ciromu9899/frontdesk
```

This attestation is portable across operating systems and detects replacement or
tampering. It does not replace Authenticode for Windows SmartScreen publisher
trust; the certificate-and-catalog procedure above remains required for that.
