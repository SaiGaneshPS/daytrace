# Windows setup

How the Windows dev machine is set up (DT-2). Everything lives under one folder, `D:\Hackathon`, so it's easy
to find and remove. If you use a different drive or folder, replace `D:\Hackathon` everywhere below.

## What is installed and where

| Tool | Version | Where | How it was installed |
|---|---|---|---|
| Git | 2.55 | `D:\Hackathon\Tools\Git` | `winget install --id Git.Git -e --location D:\Hackathon\Tools\Git` |
| GitHub CLI | 2.101 | `D:\Hackathon\Tools\GitHubCLI` | `winget install --id GitHub.cli -e --location D:\Hackathon\Tools\GitHubCLI` |
| Python | 3.12.10 | `D:\Hackathon\Tools\Python312` | `winget install --id Python.Python.3.12 -e --scope user --override "/quiet InstallAllUsers=0 TargetDir=D:\Hackathon\Tools\Python312 PrependPath=1 Include_launcher=0 Include_test=0"` |
| Node.js | 24 LTS | `D:\Hackathon\Tools\NodeJS` | Official portable zip from nodejs.org, SHA-256 checked against `SHASUMS256.txt`, then added to the user PATH. The MSI needs admin rights and fails silently otherwise. |
| Tailscale | 1.102 | `D:\Hackathon\Tools\Tailscale` | `winget install --id Tailscale.Tailscale -e --location D:\Hackathon\Tools\Tailscale` |
| LM Studio | 0.4.25 | `D:\Hackathon\Tools\LMStudio` | `winget install --id ElementLabs.LMStudio -e --location D:\Hackathon\Tools\LMStudio` |
| Android Studio | 2026.1 | `C:\Program Files\Android\Android Studio` | `winget install --id Google.AndroidStudio -e`. Its installer ignores `--location`, so to put it elsewhere, run it with `/S /D=<folder>` from an admin prompt. |
| VS Code | n/a | wherever you already have it | Needed for `code tunnel` (DT-7) |

`D:\Hackathon\installed-software.txt` is the full log for this machine: versions, paths, what ended up on C:,
and how to uninstall each tool.

Other folders:

| Folder | Used for |
|---|---|
| `D:\Hackathon\daytrace` | this repo |
| `D:\Hackathon\data` | hub databases (never committed) |
| `D:\Hackathon\Android\Sdk` | Android SDK |
| `D:\Hackathon\Models` | LM Studio models |
| `D:\Hackathon\Cache\{npm,pip,gradle}` | package caches |
| `D:\Hackathon\keys` | Android signing key (DT-25, never committed) |

## Environment variables

Set as **user** variables (Settings > System > About > Advanced system settings > Environment Variables), so
caches and data stay out of `C:\Users`:

| Variable | Value |
|---|---|
| `NPM_CONFIG_CACHE` | `D:\Hackathon\Cache\npm` |
| `PIP_CACHE_DIR` | `D:\Hackathon\Cache\pip` |
| `GRADLE_USER_HOME` | `D:\Hackathon\Cache\gradle` |
| `ANDROID_HOME` | `D:\Hackathon\Android\Sdk` |
| `DAYTRACE_DATA_DIR` | `D:\Hackathon\data` |
| `Path` (added) | `D:\Hackathon\Tools\NodeJS`, your VS Code `bin` folder |

Git, GitHub CLI, Python and Tailscale add themselves to PATH. **Open a new terminal** after installing
anything, because terminals that are already open keep the old PATH.

## Sign-ins

1. **GitHub:** `gh auth login` (GitHub.com, HTTPS, *Yes* to authenticate Git, log in with a web browser),
   then `gh auth setup-git`.
2. **Commit identity for this repo only**, using your GitHub no-reply address so your real email stays out of
   the public history:
   ```powershell
   git config user.name  "<your GitHub username>"
   git config user.email "<id>+<username>@users.noreply.github.com"   # id: gh api user --jq .id
   ```
3. **Tailscale:** sign in from the tray icon (free Personal plan). The tailnet setup and ACL are in DT-7.
4. **Android Studio:** on first launch, pick **Custom** and set the SDK location to `D:\Hackathon\Android\Sdk`.
5. **LM Studio:** before downloading any model, open **My Models** and change the models folder to
   `D:\Hackathon\Models`.

## Hub

```powershell
cd D:\Hackathon\daytrace\hub
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m daytrace_hub --help
```

Start a profile with `.\scripts\dev-hub.ps1 -Profile personal|shared-dev|demo` (works once DT-10 lands).

## Dashboard

```powershell
cd D:\Hackathon\daytrace\dashboard
npm ci
npm run build     # type-check + production build
npm run dev       # dev server
```

## Android

Android Studio's bundled JDK is used for Gradle. Open `D:\Hackathon\daytrace\android` in Android Studio once
DT-19 has generated the Gradle files (see `android/README.md`). The app is installed on your phone as an APK:
there is no Play Store (see `docs/install-android.md`).

## Local AI

1. In LM Studio, download a model that supports tool calling:
   - A **14B-20B** model for a desktop GPU with 16 GB of VRAM (e.g. an RX 9070 XT), using the **Vulkan**
     runtime on AMD cards.
   - A **3B-4B** model for quick tests and for laptops.
2. **Developer** tab: start the server. It listens on `http://127.0.0.1:1234/v1` (OpenAI-compatible).
3. Check that it answers:
   ```powershell
   curl.exe http://127.0.0.1:1234/v1/models
   ```
The hub reads `LLM_BASE_URL` and `LLM_MODEL` (DT-37).

## Check that everything works

```powershell
git --version; gh --version; python --version; node --version; npm --version; tailscale version
```
