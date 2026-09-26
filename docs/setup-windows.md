# Windows setup

How the Windows dev machine is set up (DT-2). Everything lives under one folder, `D:\Hackathon`, so it's easy
to find and remove. To use another folder, replace `D:\Hackathon` everywhere below, and **pick a path without
spaces**: several installer switches below (`TargetDir=`, `/D=`) break on paths with spaces.

Component-specific commands live in each component's README ([hub](../hub/README.md),
[dashboard](../dashboard/README.md), [android](../android/README.md)). This page only covers what is special
about Windows.

## 1. Allow PowerShell to run scripts (once)

Windows 11 blocks PowerShell scripts by default. Without this step, `npm` (which runs `npm.ps1`) and
`scripts\dev-hub.ps1` fail with *"running scripts is disabled on this system"*.

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

This allows scripts you create locally, and scripts from the internet only if they are signed. It only
affects your user account.

## 2. What is installed and where

| Tool | Version | Where | How to install |
|---|---|---|---|
| Git | 2.55 | `D:\Hackathon\Tools\Git` | `winget install --id Git.Git -e --location "D:\Hackathon\Tools\Git"` |
| GitHub CLI | 2.101 | `D:\Hackathon\Tools\GitHubCLI` | `winget install --id GitHub.cli -e --location "D:\Hackathon\Tools\GitHubCLI"` |
| Python | 3.12.10 | `D:\Hackathon\Tools\Python312` | `winget install --id Python.Python.3.12 -e --scope user --override "/quiet InstallAllUsers=0 TargetDir=D:\Hackathon\Tools\Python312 PrependPath=1 Include_launcher=0 Include_test=0"` |
| Node.js | 24.19.0 LTS | `D:\Hackathon\Tools\NodeJS` | Portable zip, see [Node.js](#nodejs-portable-zip) below |
| Tailscale | 1.102 | `D:\Hackathon\Tools\Tailscale` | `winget install --id Tailscale.Tailscale -e --location "D:\Hackathon\Tools\Tailscale"` |
| LM Studio | 0.4.25 | `D:\Hackathon\Tools\LMStudio` | `winget install --id ElementLabs.LMStudio -e --location "D:\Hackathon\Tools\LMStudio"` |
| Android Studio | 2026.1 | `C:\Program Files\Android\Android Studio` | `winget install --id Google.AndroidStudio -e`, see [Android Studio](#android-studio) below |
| VS Code | n/a | wherever you already have it | Needed for `code tunnel` (DT-7) |

Other folders:

| Folder | Used for |
|---|---|
| `D:\Hackathon\daytrace` | this repo |
| `D:\Hackathon\data` | hub databases (never committed) |
| `D:\Hackathon\Android\Sdk` | Android SDK |
| `D:\Hackathon\Models` | LM Studio models |
| `D:\Hackathon\Cache\{npm,pip,gradle}` | package caches |
| `D:\Hackathon\keys` | Android signing key (DT-25, never committed) |

### Node.js (portable zip)

The Node MSI needs admin rights and fails in silent mode, so we use the official zip. It unpacks into a
`node-v24.19.0-win-x64` subfolder; move that folder's **contents** up to `D:\Hackathon\Tools\NodeJS`.

```powershell
$ver = 'v24.19.0'
$zip = "D:\Hackathon\Cache\node-$ver-win-x64.zip"
Invoke-WebRequest "https://nodejs.org/dist/$ver/node-$ver-win-x64.zip" -OutFile $zip
# Compare with the line for node-$ver-win-x64.zip in https://nodejs.org/dist/$ver/SHASUMS256.txt
(Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
Expand-Archive $zip -DestinationPath D:\Hackathon\Cache\node-extract
Move-Item "D:\Hackathon\Cache\node-extract\node-$ver-win-x64" D:\Hackathon\Tools\NodeJS
```

Then add `D:\Hackathon\Tools\NodeJS` to your user PATH (section 3).

### Android Studio

The winget installer ignores `--location` and always installs to `C:\Program Files`. To put it somewhere
else, download the installer and run it yourself from an **admin** PowerShell. `/D=` must be the **last**
argument and **must not be quoted**:

```powershell
winget download --id Google.AndroidStudio -e --download-directory D:\Hackathon\Cache\AndroidStudio
& "D:\Hackathon\Cache\AndroidStudio\<installer>.exe" /S /D=D:\Hackathon\Tools\AndroidStudio
```

## 3. Environment variables

Set these as **user** variables: Start menu, search for **"Edit environment variables for your account"**
(don't use *Advanced system settings*, which runs as admin and can end up editing the admin's variables).
Or from PowerShell:

```powershell
[Environment]::SetEnvironmentVariable('NPM_CONFIG_CACHE', 'D:\Hackathon\Cache\npm', 'User')
```

| Variable | Value | Why |
|---|---|---|
| `NPM_CONFIG_CACHE` | `D:\Hackathon\Cache\npm` | npm cache off C: |
| `PIP_CACHE_DIR` | `D:\Hackathon\Cache\pip` | pip cache off C: |
| `GRADLE_USER_HOME` | `D:\Hackathon\Cache\gradle` | Gradle caches off C: |
| `ANDROID_HOME` | `D:\Hackathon\Android\Sdk` | Android SDK location |
| `JAVA_HOME` | `C:\Program Files\Android\Android Studio\jbr` | Makes terminal builds (`gradlew`) use Android Studio's JDK instead of whichever `java` is first on PATH |
| `DAYTRACE_DATA_DIR` | `D:\Hackathon\data` | hub databases (must be an absolute path) |
| `Path` (add) | `D:\Hackathon\Tools\NodeJS`, your VS Code `bin` folder | `node`, `npm`, `code` |

Optional, only if you use them: `ANDROID_USER_HOME` (emulator images, default `C:\Users\<you>\.android`, several
GB per emulator). LM Studio always keeps its settings and runtimes in `C:\Users\<you>\.lmstudio`; only its
models folder can be moved (section 4).

Git, GitHub CLI, Python and Tailscale add themselves to PATH. After changing any variable, **fully restart**
VS Code, Android Studio and any open terminals: apps keep the environment they were started with, so a new
tab isn't enough.

## 4. Sign-ins and first launch

1. **GitHub:** `gh auth login` (GitHub.com, HTTPS, *Yes* to authenticate Git, log in with a web browser). If
   Git wasn't on PATH yet when you logged in, run `gh auth setup-git` afterwards.
2. **Email privacy on GitHub:** under Settings > Emails, turn on **Keep my email addresses private** and
   **Block command line pushes that expose my email**. This is what keeps your real email out of commits
   GitHub makes for you (merge buttons, web edits).
3. **Clone the repo and set your commit identity for it** (using the no-reply address from Settings > Emails):
   ```powershell
   git clone https://github.com/SaiGaneshPS/daytrace.git D:\Hackathon\daytrace
   cd D:\Hackathon\daytrace
   git config user.name  "<your GitHub username>"
   git config user.email "<id>+<username>@users.noreply.github.com"   # id: gh api user --jq .id
   ```
4. **Tailscale:** sign in from the tray icon (free Personal plan). The tailnet setup and ACL are in DT-7.
5. **Android Studio:** on first launch pick **Custom** and set the SDK location to `D:\Hackathon\Android\Sdk`.
6. **LM Studio:** before downloading any model, open **My Models** and change the models folder to
   `D:\Hackathon\Models`.

## 5. Run things

Commands run from the **repo root** (`D:\Hackathon\daytrace`) unless noted:

- **Hub:** set up the virtual environment as in [hub/README.md](../hub/README.md). Start a profile with
  `.\scripts\dev-hub.ps1 -Profile personal` (other profiles: `shared-dev`, `demo`; works once DT-10 lands).
- **Dashboard:** see [dashboard/README.md](../dashboard/README.md).
- **Android:** once DT-19 has generated the Gradle files (see [android/README.md](../android/README.md)), open
  `android\` in Android Studio, or build from a terminal with `cd android; .\gradlew.bat assembleDebug`. The app is
  installed on your phone as an APK: there is no Play Store (see [install-android.md](install-android.md)).

## 6. Local AI

1. In LM Studio, download a model that supports tool calling:
   - A **14B-20B** model for a desktop GPU with 16 GB of VRAM (e.g. an RX 9070 XT), using the **Vulkan**
     runtime on AMD cards.
   - A **3B-4B** model for quick tests and for laptops.
2. **Developer** tab: start the server. It listens on `http://127.0.0.1:1234/v1` (OpenAI-compatible).
3. Check that it answers: `curl.exe http://127.0.0.1:1234/v1/models`

DT-37 adds the hub's settings for which server and model to use.

## 7. Check that everything works

```powershell
git --version; gh --version; python --version; node --version; npm --version; tailscale version
```

## 8. Uninstall

| Tool | How |
|---|---|
| Git, GitHub CLI, Python, Tailscale, LM Studio, Android Studio | `winget uninstall --id <winget id>` (IDs in the table in section 2) |
| Node.js | Delete `D:\Hackathon\Tools\NodeJS` and remove it from your user PATH |
| Environment variables | Remove them in "Edit environment variables for your account" |
| Leftovers on C: | `C:\Users\<you>\.gitconfig`, `C:\Users\<you>\.lmstudio`, `C:\Users\<you>\.android`, `C:\ProgramData\Tailscale` |
