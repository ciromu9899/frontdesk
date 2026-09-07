"""Package an already Developer-ID-signed app; fail closed on notarization errors.

Run on macOS with an existing notarytool Keychain profile. This never exports keys.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def package(app: Path, output: Path, profile: str) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("macOS is required for Gatekeeper and notarization checks.")
    app = app.resolve()
    if app.suffix != ".app" or not (app / "Contents" / "MacOS").is_dir():
        raise ValueError("A built FrontDesk.app is required.")
    if output.exists():
        raise ValueError("Choose a new output path; existing packages are not overwritten.")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    details = subprocess.run(["codesign", "-dv", "--verbose=4", str(app)],
                             capture_output=True, text=True, check=True)
    if "Authority=Developer ID Application:" not in details.stderr:
        raise RuntimeError("A Developer ID Application signature is required.")
    subprocess.run(["hdiutil", "create", "-volname", "FrontDesk", "-srcfolder", str(app),
                    "-format", "UDZO", str(output)], check=True)
    result = subprocess.run(["xcrun", "notarytool", "submit", str(output),
                             "--keychain-profile", profile, "--wait", "--output-format", "json"],
                            capture_output=True, text=True, check=True)
    if json.loads(result.stdout).get("status") != "Accepted":
        raise RuntimeError("Notarization was not accepted; do not distribute this DMG.")
    subprocess.run(["xcrun", "stapler", "staple", str(output)], check=True)
    subprocess.run(["xcrun", "stapler", "validate", str(output)], check=True)
    subprocess.run(["spctl", "--assess", "--type", "open", "--context",
                    "context:primary-signature", "--verbose=2", str(output)], check=True)
    with output.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    output.with_suffix(".dmg.sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--keychain-profile", required=True)
    args = parser.parse_args()
    package(args.app, args.output, args.keychain_profile)
