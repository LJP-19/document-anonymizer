# Release guide

Official installers are built by **GitHub Actions**, not on a personal computer.
One workflow, `.github/workflows/build-release.yml`, builds both installers from
the **same commit**, tests them, and publishes them as a GitHub Release.

```
Claude Code -> push to GitHub -> GitHub Actions -> Windows .exe + macOS .dmg -> GitHub Release
```

You do not need to download a source ZIP, open PowerShell or run `PUBLISH.bat`.
(`PUBLISH.bat` / `PUBLISH.command` still work as an emergency fallback: they only push
a version tag, which starts this same workflow.)

## What each release contains

| File | What it is |
|---|---|
| `DocumentAnonymizer-Setup-vX.Y.Z-Windows-x64.exe` | Windows 10/11 installer (64-bit Intel/AMD). Installs into Program Files, adds a Start menu entry and an uninstaller. |
| `DocumentAnonymizer-vX.Y.Z-macOS-arm64.dmg` | macOS 13+ on **Apple Silicon** (M1/M2/M3/M4), **signed and notarized**. |
| `DocumentAnonymizer-vX.Y.Z-macOS-arm64-UNSIGNED.dmg` | The same app when the Apple signing secrets are **not** configured. Not notarized: macOS asks once before the first launch (System Settings > Privacy & Security > Open Anyway). The build fails if the app's own signature is not valid - a broken one is what macOS calls "damaged". |
| `SHA256SUMS.txt` | Checksums of the installers. |
| `build-manifest.json` | Version, exact source commit, workflow run link, macOS signing status, file sizes and checksums. |
| `RELEASE_NOTES.md` | What changed and known limitations. |

**Not supported:** Intel Macs (the pinned PyTorch release ships no Intel macOS build),
Windows on ARM, 32-bit Windows.

## 1. Update the version

The version lives in one place: `app/version.py` (`__version__ = "X.Y.Z"`). Raise it for
every release - a release is never overwritten, so the same version cannot be published
twice. Add a short section to `docs/RELEASE_NOTES.md` describing the change.

## 2. How Claude Code starts a release

After the code is finished and the full test suite passes, Claude Code:

1. commits the change and pushes the branch;
2. starts the workflow in one of two ways (both run the same checks):
   * **Tag** - pushes a tag `vX.Y.Z` that matches `app/version.py`. The tag starts the
     workflow, and a successful run **publishes** the release.
   * **Manual run** - triggers "Build and release" with `release` set to `draft` or
     `publish`;
3. watches the run and reports the result, the release link and the download links.

Claude Code may push and start a release only when you have authorized it and every
test and check has passed.

## 3. Trigger the workflow yourself

**From the GitHub website:** repository -> **Actions** -> **Build and release** ->
**Run workflow** -> choose the branch -> set **release** to `none` (just build),
`draft` (build and create an unpublished release) or `publish` -> **Run workflow**.

**With a tag (from any git client):**
```bash
git tag v0.42.0
```
```bash
git push origin v0.42.0
```
The workflow refuses to run if the tag does not match `app/version.py`.

## 4. Watch progress

Repository -> **Actions** -> the run. The jobs are:

| Job | Takes | What it proves |
|---|---|---|
| Version check | <1 min | the version is valid and matches the tag |
| Tests (Linux) | ~15-25 min | the full test suite and the false-positive benchmark (must be zero) |
| Windows installer (x64) | ~60-90 min | suite on Windows, frozen app self-test, installer built, **installed silently, the installed app launched, uninstalled** |
| macOS DMG (Apple Silicon arm64) | ~60-90 min | suite on macOS, app is arm64, signed + notarized when configured, **DMG mounted, app copied out, the copied app launched** |
| Release | ~2 min | checksums verified, manifest written, release created |

If **any** of the first four fails, there is no release.

## 5. Where the installers are

* **Released runs:** repository -> **Releases** -> `Document Anonymizer vX.Y.Z` ->
  **Assets**. Direct links have the form
  `https://github.com/LJP-19/document-anonymizer/releases/download/vX.Y.Z/<file name>`.
* **Every run (also without a release):** the run page -> **Artifacts** ->
  `windows-installer` / `macos-dmg` (kept 30 days; downloaded as a ZIP).

## 6. Download and check

Download the installer from the release's Assets. To check it is intact, compare its
SHA-256 with `SHA256SUMS.txt`:
* Windows (PowerShell): `Get-FileHash .\DocumentAnonymizer-Setup-vX.Y.Z-Windows-x64.exe`
* macOS (Terminal): `shasum -a 256 DocumentAnonymizer-vX.Y.Z-macOS-arm64.dmg`

## 7. Configure Apple signing (once)

Needed so the DMG opens on your boss's Mac without warnings. You need a paid
**Apple Developer Program** membership.

1. **Developer ID certificate.** In Xcode -> Settings -> Accounts -> Manage Certificates,
   or at developer.apple.com -> Certificates, create a **Developer ID Application**
   certificate. In Keychain Access, export it **with its private key** as a `.p12` file
   and choose a password.
2. **App-specific password.** At appleid.apple.com -> Sign-In and Security ->
   App-Specific Passwords, create one (for notarization).
3. **Team ID.** developer.apple.com -> Membership details (10 characters).
4. **Signing identity.** The certificate's full name, for example
   `Developer ID Application: Turnkey Financial Solutions (ABCDE12345)`.
5. Base64 the certificate. On a Mac: `base64 -i certificate.p12 | pbcopy`.
6. GitHub -> repository -> **Settings** -> **Secrets and variables** -> **Actions** ->
   **New repository secret**, one per name:

| Secret | Value |
|---|---|
| `APPLE_CERTIFICATE_BASE64` | the base64 text from step 5 |
| `APPLE_CERTIFICATE_PASSWORD` | the `.p12` password |
| `APPLE_SIGNING_IDENTITY` | the identity from step 4 |
| `APPLE_ID` | your Apple ID e-mail |
| `APPLE_APP_SPECIFIC_PASSWORD` | the password from step 2 |
| `APPLE_TEAM_ID` | the Team ID from step 3 |

Secrets are never printed in logs and are never stored in the repository.

## 8. Signed or unsigned?

* The DMG file name ends in **`-UNSIGNED.dmg`** when it is not signed.
* `build-manifest.json` -> `"macos_signing"`: `SIGNED_NOTARIZED` or `UNSIGNED`
  (with `macos_missing_secrets` listing which secrets were missing).
* The release notes say so in bold when the DMG is unsigned.
* On a Mac: `spctl --assess --type open --context context:primary-signature -v <file>.dmg`
  says "accepted ... Notarized Developer ID" for a signed, notarized DMG.

The Windows installer is not code-signed (no Windows certificate is configured);
Windows SmartScreen may show "Windows protected your PC" -> **More info** -> **Run anyway**.

## 9. When a build fails

1. Open the failed run -> the red job -> the red step. The last lines say why.
2. Common causes:
   * **Tests failed** - a real defect; fix the code, push, run again.
   * **"tag ... does not match app/version.py"** - delete the wrong tag (only if no
     release was created from it) or bump the version and tag again.
   * **pip / network error** - re-run the failed jobs (Re-run jobs -> Re-run failed jobs).
   * **Notarization was not accepted** - the step prints Apple's log; usually a nested
     binary was not signed or a secret is wrong.
   * **"over GitHub's 2 GB release-asset limit"** - the installer grew too large.
3. Nothing is published until every job passes, so a failed run never leaves a half release.

## 10. Roll back a bad release

Releases are never overwritten or deleted by the workflow.
1. Repository -> **Releases** -> the bad release -> **Edit** -> tick **Set as a pre-release**
   (or delete the release by hand if it must disappear) and say why in the notes.
2. The previous release stays available; point people to it.
3. Fix the problem, raise the version (e.g. `0.42.1`) and release again. Never reuse a
   version number.
