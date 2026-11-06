#!/bin/bash
# Package dist/DocumentAnonymizer.app into a DMG; sign and notarize it when the
# Apple Developer secrets are configured. Run by GitHub Actions on macOS.
#
#   installer/macos/sign_and_package.sh <path/to/App.app> <version> <arch> <out_dir>
#
# Secrets (GitHub repository secrets -> env, never echoed):
#   APPLE_CERTIFICATE_BASE64     base64 of the Developer ID Application .p12
#   APPLE_CERTIFICATE_PASSWORD   password of that .p12
#   APPLE_SIGNING_IDENTITY       e.g. "Developer ID Application: Name (TEAMID)"
#   APPLE_ID                     Apple ID e-mail used for notarization
#   APPLE_APP_SPECIFIC_PASSWORD  app-specific password of that Apple ID
#   APPLE_TEAM_ID                10-character team id
#
# With ALL six set: inside-out codesign with the hardened runtime, DMG signed,
# notarized (notarytool --wait), stapled, verified -> signing=SIGNED_NOTARIZED.
# With ANY missing: an UNSIGNED DMG, named ...-UNSIGNED.dmg, signing=UNSIGNED,
# and the missing secret NAMES are listed. Ad-hoc signing is never used as a
# substitute and nothing is described as notarized unless notarytool accepted it.
set -euo pipefail

APP="$1"; VERSION="$2"; ARCH="$3"; OUT="$4"
ENT="$(cd "$(dirname "$0")" && pwd)/entitlements.plist"
mkdir -p "$OUT"
out_var() { if [ -n "${GITHUB_OUTPUT:-}" ]; then echo "$1=$2" >> "$GITHUB_OUTPUT"; fi; echo "$1=$2"; }

[ -d "$APP" ] || { echo "::error::$APP was not produced"; exit 1; }

missing=()
for name in APPLE_CERTIFICATE_BASE64 APPLE_CERTIFICATE_PASSWORD APPLE_SIGNING_IDENTITY \
            APPLE_ID APPLE_APP_SPECIFIC_PASSWORD APPLE_TEAM_ID; do
  if [ -z "${!name:-}" ]; then missing+=("$name"); fi
done

make_dmg() {
  local dmg="$1" staging
  staging="$(mktemp -d)"
  cp -R "$APP" "$staging/"
  ln -s /Applications "$staging/Applications"
  hdiutil create -volname "Document Anonymizer" -srcfolder "$staging" -ov -format UDZO "$dmg"
  rm -rf "$staging"
}

if [ ${#missing[@]} -gt 0 ]; then
  echo "::warning::macOS signing secrets not configured (${missing[*]}). Building an UNSIGNED development DMG."
  echo "::warning::An UNSIGNED DMG is NOT notarized and macOS Gatekeeper will block it on other Macs."
  DMG="$OUT/DocumentAnonymizer-v${VERSION}-macOS-${ARCH}-UNSIGNED.dmg"
  make_dmg "$DMG"
  out_var signing UNSIGNED
  out_var missing_secrets "${missing[*]}"
  out_var dmg "$DMG"
  exit 0
fi

# --- signed + notarized path ---------------------------------------------------
KEYCHAIN="$RUNNER_TEMP/build.keychain-db"
KEYCHAIN_PW="$(uuidgen)"
P12="$RUNNER_TEMP/cert.p12"
cleanup() { security delete-keychain "$KEYCHAIN" >/dev/null 2>&1 || true; rm -f "$P12"; }
trap cleanup EXIT

echo "$APPLE_CERTIFICATE_BASE64" | base64 --decode > "$P12"
security create-keychain -p "$KEYCHAIN_PW" "$KEYCHAIN"
security set-keychain-settings -lut 21600 "$KEYCHAIN"
security unlock-keychain -p "$KEYCHAIN_PW" "$KEYCHAIN"
security import "$P12" -k "$KEYCHAIN" -P "$APPLE_CERTIFICATE_PASSWORD" -T /usr/bin/codesign
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PW" "$KEYCHAIN" >/dev/null
security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')

sign() { codesign --force --timestamp --options runtime --entitlements "$ENT" --sign "$APPLE_SIGNING_IDENTITY" "$@"; }

# Inside-out: every Mach-O inside the bundle first, then the bundle itself.
echo "Signing nested code..."
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then sign "$f"; fi
done < <(find "$APP/Contents" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) -print0)
sign "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

DMG="$OUT/DocumentAnonymizer-v${VERSION}-macOS-${ARCH}.dmg"
make_dmg "$DMG"
codesign --force --timestamp --sign "$APPLE_SIGNING_IDENTITY" "$DMG"

echo "Submitting for notarization (this waits for Apple)..."
RESULT="$(xcrun notarytool submit "$DMG" --apple-id "$APPLE_ID" --password "$APPLE_APP_SPECIFIC_PASSWORD" \
          --team-id "$APPLE_TEAM_ID" --wait --output-format json)"
STATUS="$(echo "$RESULT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("status",""))')"
ID="$(echo "$RESULT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("id",""))')"
echo "notarization status: $STATUS (submission $ID)"
if [ "$STATUS" != "Accepted" ]; then
  xcrun notarytool log "$ID" --apple-id "$APPLE_ID" --password "$APPLE_APP_SPECIFIC_PASSWORD" --team-id "$APPLE_TEAM_ID" || true
  echo "::error::Notarization was not accepted ($STATUS). The DMG is NOT notarized."
  exit 1
fi
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"
spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
out_var signing SIGNED_NOTARIZED
out_var missing_secrets ""
out_var dmg "$DMG"
