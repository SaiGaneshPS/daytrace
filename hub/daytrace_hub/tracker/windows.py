"""DT-16: Windows foreground app, window title and idle time, straight from the Win32 API (ctypes; psutil only as a
fallback).

- The foreground window gives the process; `QueryFullProcessImageNameW` with limited query rights gives its
  program even when it runs as administrator. Store apps (Settings, Photos, Calculator...) sit inside an
  ApplicationFrameHost frame, so their own child window is used to find the real app. The program's own
  description ("Visual Studio Code" for Code.exe) is its display name, read once per program.
- `GetLastInputInfo` gives the time since the last keyboard or mouse input (the tick counter wraps every
  49.7 days, which the arithmetic allows for).
- The screen is locked when the input desktop cannot be opened (the lock screen and UAC's secure desktop) or
  the lock app is in front.
- A display request (a video or presentation keeping the screen on) shows up in the system execution state.

Nothing here raises for an odd window: missing information comes back as None.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from functools import lru_cache

from .base import Reading

if sys.platform != "win32":  # pragma: no cover - imported only on Windows (tracker.base.platform_probe)
    raise ImportError("the Windows probe needs Windows")

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a hub dependency; the probe works without it
    psutil = None

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_powrprof = ctypes.WinDLL("powrprof")
_version = ctypes.WinDLL("version")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DESKTOP_SWITCHDESKTOP = 0x0100
SYSTEM_EXECUTION_STATE = 16
ES_DISPLAY_REQUIRED = 0x2
LOCK_APPS = frozenset({"lockapp.exe", "logonui.exe"})
FRAME_HOSTS = frozenset({"applicationframehost.exe"})
PATH_CHARS = 32768


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.EnumChildWindows.argtypes = [wintypes.HWND, _EnumProc, wintypes.LPARAM]
_user32.OpenInputDesktop.restype = wintypes.HANDLE
_user32.CloseDesktop.argtypes = [wintypes.HANDLE]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.GetTickCount.restype = wintypes.DWORD


def idle_seconds() -> float:
    info = _LastInputInfo(ctypes.sizeof(_LastInputInfo), 0)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((_kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def input_desktop_open() -> bool:
    """False on the lock screen and on UAC's secure desktop, where nothing can be read or typed into."""
    desktop = _user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not desktop:
        return False
    _user32.CloseDesktop(desktop)
    return True


def display_required() -> bool:
    state = wintypes.ULONG()
    status = _powrprof.CallNtPowerInformation(SYSTEM_EXECUTION_STATE, None, 0, ctypes.byref(state), ctypes.sizeof(state))
    return status == 0 and bool(state.value & ES_DISPLAY_REQUIRED)


def window_title(hwnd: int) -> str | None:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return None
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value or None


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def store_app_pid(frame: int, frame_pid: int) -> int | None:
    """A Store app's own process: the child window of its ApplicationFrameHost frame that another process owns."""
    found: list[int] = []

    def visit(child: int, _: int) -> bool:
        pid = window_pid(child)
        if pid and pid != frame_pid:
            found.append(pid)
            return False  # stop: found it
        return True

    _user32.EnumChildWindows(frame, _EnumProc(visit), 0)
    return found[0] if found else None


def process_path(pid: int, buffer: ctypes.Array[ctypes.c_wchar] | None = None) -> str | None:
    buffer = buffer if buffer is not None else ctypes.create_unicode_buffer(PATH_CHARS)
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            size = wintypes.DWORD(len(buffer))
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
        finally:
            _kernel32.CloseHandle(handle)
    if psutil is not None:  # psutil can sometimes read what the limited query could not
        try:
            return psutil.Process(pid).exe() or None
        except (psutil.Error, OSError):  # access denied, a process that just exited, ...
            return None
    return None


@lru_cache(maxsize=512)
def program_description(path: str) -> str | None:
    """The FileDescription in the program's version resource ("Visual Studio Code"), or None."""
    try:
        size = _version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not _version.GetFileVersionInfoW(path, 0, size, data):
            return None
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        keys = []
        if _version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)) and length.value >= 4:
            lang, codepage = ctypes.cast(pointer, ctypes.POINTER(wintypes.WORD * 2)).contents
            keys.append(f"{lang:04x}{codepage:04x}")
        keys += ["040904b0", "040904e4"]  # US English, the usual fallbacks
        for key in keys:
            if _version.VerQueryValueW(data, f"\\StringFileInfo\\{key}\\FileDescription", ctypes.byref(pointer),
                                       ctypes.byref(length)) and length.value > 1:
                text = ctypes.wstring_at(pointer, length.value - 1).strip()
                if text:
                    return text
    except OSError:
        return None
    return None


def display_name(path: str | None) -> str | None:
    if not path:
        return None
    exe = os.path.basename(path)
    return program_description(path) or os.path.splitext(exe)[0] or None


class WindowsProbe:
    """Reads the foreground window every poll (a few microseconds of work, no allocation of note)."""

    def __init__(self) -> None:
        self._buffer = ctypes.create_unicode_buffer(PATH_CHARS)  # reused on every poll

    def read(self) -> Reading | None:
        idle = idle_seconds()
        showing = display_required()
        if not input_desktop_open():
            return Reading(None, None, None, idle, locked=True, display_required=showing)
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:  # switching desktops, or the moment a window closes
            return None
        pid = window_pid(hwnd)
        path = process_path(pid, self._buffer) if pid else None
        exe = os.path.basename(path) if path else None
        if exe and exe.lower() in FRAME_HOSTS:  # a Store app: find the app inside the frame
            app_pid = store_app_pid(hwnd, pid)
            app_path = process_path(app_pid, self._buffer) if app_pid else None
            if app_path:
                path, exe = app_path, os.path.basename(app_path)
        if exe and exe.lower() in LOCK_APPS:
            return Reading(None, None, None, idle, locked=True, display_required=showing)
        return Reading(display_name(path), exe, window_title(hwnd), idle, display_required=showing)
