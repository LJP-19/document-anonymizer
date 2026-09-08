# Releasing

## One time, before the first release

1. Create the repository on GitHub: <https://github.com/new>
   - Name it to match `repository` in `release_config.json`
   - Set it **Private**
   - Do **not** add a README, .gitignore, or licence
2. Install Git on the machine you release from. Python 3.11 or 3.12 is
   optional — it only enables the local test run, and the launcher will offer
   to install it. Publishing works without it.
3. Open a terminal in this project once and run `git push`. Complete the GitHub
   sign-in when prompted. After that the launcher works with a double-click.

## Every release

Save the new `document-anonymizer-v*.zip` anywhere normal (Downloads, Desktop,
Documents), then double-click **`PUBLISH.bat`** (Windows) or **`PUBLISH.command`**
(macOS) in the project root. Nothing else.

```
find newest zip -> copy over the project -> sync version and commit message
   -> install missing dependencies -> run tests
   -> commit -> push -> tag v<version> -> report
```

Two things are always protected: `release/release_config.json` keeps your
settings, and `.git` is never written to. If no zip is found, it publishes the
project exactly as it stands.

`release/release.py` still exists and can be run on its own when you want to
publish without applying a build.

then GitHub Actions builds on native runners:

```
                    GitHub
                      |
        +-------------+-------------+
        |                           |
  windows-latest              macos-14
        |                           |
   PyInstaller                PyInstaller
        |                           |
      .exe                       .app -> .dmg
        |                           |
        +----------> Release <------+
```

Installers appear at `<your repo>/releases` (tagged runs) or under
`<your repo>/actions` -> latest run -> **Artifacts** (untagged pushes).

## What the launcher will not do

- run `git reset --hard`, `git checkout --force`, or `git clean`
- discard uncommitted or untracked work
- move you between branches
- push if the tests fail
- push if a required file is missing
- claim success when the push failed
- force-push, or rewrite history that is already on GitHub
- overwrite your `release_config.json`
- extract an archive containing a path that escapes the project folder

It pushes the current working tree, not a copy or a cached snapshot.

## Configuration

`release_config.json`:

| Key | Meaning |
|---|---|
| `repository` | full HTTPS clone URL |
| `branch` | branch to push |
| `version` | mirrored from `app/version.py`, do not edit here |
| `git_name`, `git_email` | local commit identity |
| `commit_message` | message for this release commit |
| `build_windows`, `build_macos` | reported in the summary |
| `run_tests_before_push` | set false only if you know why |
| `create_github_release` | tag and publish a Release |

**Never put a token, password, certificate, or notarization credential in this
file or anywhere in the repository.** GitHub's own login prompt handles the
push; anything CI needs belongs in repository Secrets.

## If GitHub is ahead of you

The launcher handles it. When a push is rejected because the remote has commits
this folder does not, it fetches, replays your work on top, and pushes again,
telling you it did so. If the replay hits a genuine conflict it aborts, leaves
everything exactly as it was, and tells you the one command to run.

## If the build fails

The launcher cannot see the build result — it only reports that the push
succeeded. Check `<your repo>/actions`. A red X means no installers were
produced. Open the failing job and read the last step that ran.

Most common first-run failures:

- **PyInstaller misses a module** — add it to `COLLECT_ALL` or `HIDDEN_IMPORTS`
  in `buildtools/build.py` and release again.
- **`hdiutil` fails on macOS** — usually a stale `dist/` folder; the build
  cleans it, but a partial `.app` from a cancelled run can linger.
- **Gatekeeper blocks the DMG on the target Mac** — expected, the build is
  unsigned. See Limitations in the main README.
