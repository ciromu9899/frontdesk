# Desktop distribution status

The Windows portable builder reads the version from pyproject.toml. The salon
setup form is included when rebuilding from the current source.

## Windows installer

Build on Windows x64 with the pinned build requirements, Inno Setup and Windows
SDK SignTool installed. Use a trusted code-signing identity already available to
SignTool. Do not distribute a development or self-signed certificate to buyers.

Run `build_windows_app.py`, then run `build_installer.ps1` with `-AppSource`,
`-AppVersion`, `-OutputDirectory` and `-CertificateThumbprint`. Use a staging copy
for AppSource: signing modifies executable files. The script signs bundled code,
the installer and uninstaller, and verifies the final installer with SignTool.
The installation is per-user and does not require administrator privileges.
Uninstall preserves customer data and models.

## macOS

`package_macos.py FrontDesk.app FrontDesk.dmg --keychain-profile PROFILE` requires
an already built and Developer-ID-signed application on a Mac. It verifies the
app, submits the DMG to Apple, requires Accepted, staples and validates the ticket,
and runs a Gatekeeper assessment before generating the final checksum.
Failed or incomplete output is not a distributable release.

The native macOS application builder and GUI launcher are still outstanding.
This repository does not yet supply an installable, tested Mac application.

## Acceptance on clean machines

Test installation, first launch, model download cancellation and retry, salon
setup, restart, update and uninstall on clean Windows and Mac machines. Test
Apple Silicon and Intel separately if both are offered. Keep the previous signed
installer for rollback and back up customer data before updates. Never replace
customer data during rollback.

If SmartScreen, Defender or Gatekeeper blocks a download, stop and verify the
publisher, package hash and signature. Submit suspected false positives through
the platform vendor's official process. Do not disable OS protection, remove
quarantine attributes or add antivirus exclusions. A valid signature does not
guarantee that reputation-based warnings will never appear.

Neither installer compilation nor signing/notarization has yet been verified
end-to-end for this change. The build scripts are preparation, not certification.
