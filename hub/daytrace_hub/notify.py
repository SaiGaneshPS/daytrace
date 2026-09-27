"""DT-43: desktop notifications for a nudge from this computer's own activity: a Windows toast, a macOS
notification, or notify-send on Linux when it is there.

A nudge's words are data (an app's name, a calendar event's title from a phone), so they never become part of a
script. Windows runs one fixed PowerShell script that reads them from environment variables and puts them into the
toast's XML as text; macOS and Linux get them as arguments. (winotify, which the ticket named, pastes the words into
a PowerShell double-quoted here-string, where `$(...)` in a calendar title would run as a command.) The toast shows
under Windows PowerShell's registered app id, so nothing is written to the registry. Showing one never blocks the
caller, never raises, and never touches the network.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

# Windows PowerShell's own AppUserModelID: toasts from an app id nobody registered are dropped on Windows 10 and 11.
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$kind = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($kind)
$lines = $xml.GetElementsByTagName('text')
$lines.Item(0).AppendChild($xml.CreateTextNode($env:DAYTRACE_TOAST_TITLE)) | Out-Null
$lines.Item(1).AppendChild($xml.CreateTextNode($env:DAYTRACE_TOAST_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:DAYTRACE_TOAST_APP).Show($toast)
"""
# AppleScript gets the words as its arguments (argv), never as part of its text.
MAC_SCRIPT = ("on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run")
TIMEOUT_SECONDS = 20


def command(title: str, body: str, platform: str = sys.platform) -> tuple[list[str], dict[str, str]] | None:
    """What to run for this platform, and the environment variables it reads: None where there is no way to show one."""
    if platform == "win32":
        encoded = base64.b64encode(WINDOWS_SCRIPT.encode("utf-16-le")).decode("ascii")
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded]
        return argv, {"DAYTRACE_TOAST_TITLE": title, "DAYTRACE_TOAST_BODY": body, "DAYTRACE_TOAST_APP": POWERSHELL_APP_ID}
    if platform == "darwin":
        return ["osascript", *(part for line in MAC_SCRIPT for part in ("-e", line)), title, body], {}
    sender = shutil.which("notify-send")
    if sender:
        return [sender, "--app-name=Daytrace", "--", title, body], {}
    return None


def available() -> bool:
    """Whether this system has a way to show a notification."""
    return command("", "") is not None


def _run(argv: list[str], extra: dict[str, str]) -> None:
    subprocess.run(
        argv,
        env={**os.environ, **extra},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=TIMEOUT_SECONDS,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console window flashing up on Windows
    )


def _show(argv: list[str], extra: dict[str, str]) -> bool:
    try:
        _run(argv, extra)
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("the desktop notification could not be shown (%s)", error)
        return False
    except Exception:  # a notification is never worth a crashed tracker
        logger.exception("the desktop notification failed")
        return False
    return True


def show(title: str, body: str, *, wait: bool = False) -> bool:
    """Show a notification. In the background by default (True means it was started); with wait=True, whether it
    was shown."""
    found = command(title, body)
    if found is None:
        logger.info("no desktop notifications on this system: %s", title)
        return False
    argv, extra = found
    if wait:
        return _show(argv, extra)
    threading.Thread(target=_show, args=(argv, extra), name="daytrace-notify", daemon=True).start()
    return True
