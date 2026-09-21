"""
ultron/desktop.py — Full Windows desktop access for Ultron.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

try:
    import pyautogui
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    _PYCAW = True
except ImportError:
    _PYCAW = False

APP_MAP: dict[str, str] = {
    "notepad":            "notepad.exe",
    "calculator":         "calc.exe",
    "task manager":       "taskmgr.exe",
    "paint":              "mspaint.exe",
    "wordpad":            "wordpad.exe",
    "control panel":      "control.exe",
    "file explorer":      "explorer.exe",
    "explorer":           "explorer.exe",
    "cmd":                "cmd.exe",
    "command prompt":     "cmd.exe",
    "powershell":         "powershell.exe",
    "word":               "WINWORD.EXE",
    "excel":              "EXCEL.EXE",
    "powerpoint":         "POWERPNT.EXE",
    "outlook":            "OUTLOOK.EXE",
    "vs code":            "Code.exe",
    "vscode":             "Code.exe",
    "visual studio code": "Code.exe",
    "chrome":             "chrome.exe",
    "firefox":            "firefox.exe",
    "edge":               "msedge.exe",
    "vlc":                "vlc.exe",
    "discord":            "Discord.exe",
    "spotify":            "Spotify.exe",
    "telegram":           "Telegram.exe",
    "whatsapp":           "WhatsApp.exe",
    "steam":              "steam.exe",
    "obs":                "obs64.exe",
    "snipping tool":      "SnippingTool.exe",
}

FOLDER_MAP: dict[str, str] = {
    "desktop":   str(Path.home() / "Desktop"),
    "documents": str(Path.home() / "Documents"),
    "downloads": str(Path.home() / "Downloads"),
    "pictures":  str(Path.home() / "Pictures"),
    "music":     str(Path.home() / "Music"),
    "videos":    str(Path.home() / "Videos"),
    "home":      str(Path.home()),
    "temp":      os.environ.get("TEMP", "C:\\Windows\\Temp"),
}


class DesktopExecutor:
    """Executes OS-level voice commands safely."""

    def open_app(self, name: str) -> tuple[bool, str]:
        key = name.strip().lower()
        exe = APP_MAP.get(key)
        if exe:
            try:
                subprocess.Popen([exe], shell=True)
                return True, f"Opening {name}."
            except OSError as e:
                logging.warning("open_app failed: %s", e)
                return False, f"Could not open {name}."
        found = shutil.which(key) or shutil.which(key + ".exe")
        if found:
            try:
                subprocess.Popen([found], shell=True)
                return True, f"Opening {name}."
            except OSError:
                pass
        return False, f"I don't know how to open '{name}'."

    def close_app(self, name: str) -> tuple[bool, str]:
        exe = APP_MAP.get(name.lower(), name)
        if not exe.endswith(".exe"):
            exe = exe + ".exe"
        exe_base = Path(exe).name
        result = subprocess.run(["taskkill", "/F", "/IM", exe_base], capture_output=True, text=True)
        if result.returncode == 0:
            return True, f"Closed {name}."
        return False, f"Could not close {name}. Is it running?"

    def open_folder(self, name: str) -> tuple[bool, str]:
        key = name.strip().lower()
        path = FOLDER_MAP.get(key, name)
        if os.path.isdir(path):
            os.startfile(path)
            return True, f"Opening {name} folder."
        return False, f"Could not find folder: {name}"

    def open_file(self, name: str) -> tuple[bool, str]:
        search_dirs = [Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads"]
        for directory in search_dirs:
            matches = list(directory.glob(f"*{name}*"))
            if matches:
                try:
                    os.startfile(str(matches[0]))
                    return True, f"Opening {matches[0].name}."
                except OSError:
                    pass
        if os.path.isfile(name):
            os.startfile(name)
            return True, f"Opening {Path(name).name}."
        return False, f"I couldn't find a file named '{name}'."

    def find_file(self, name: str) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                f'dir /s /b "%USERPROFILE%\\*{name}*"',
                capture_output=True, text=True, timeout=10, shell=True
            )
            lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
            if lines:
                count = len(lines)
                subprocess.Popen(f'explorer /select,"{lines[0]}"', shell=True)
                return True, f"Found {count} file{'s' if count > 1 else ''}. Showing you '{Path(lines[0]).name}' in Explorer."
            return False, f"No files found matching '{name}'."
        except subprocess.TimeoutExpired:
            return False, "File search timed out."
        except Exception as e:
            return False, f"Search failed: {e}"

    def take_screenshot(self) -> tuple[bool, str]:
        desktop = Path.home() / "Desktop"
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        filepath = desktop / f"screenshot_{ts}.png"
        if _PYAUTOGUI:
            try:
                img = pyautogui.screenshot()
                img.save(str(filepath))
                return True, f"Screenshot saved to your Desktop as screenshot_{ts}.png"
            except Exception as e:
                logging.warning("pyautogui screenshot failed: %s", e)
        ps = (
            "$b = New-Object System.Drawing.Bitmap([System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width,"
            "[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height);"
            "$g = [System.Drawing.Graphics]::FromImage($b);"
            "$g.CopyFromScreen(0,0,0,0,$b.Size);"
            f'$b.Save("{filepath}");'
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; " + ps],
                capture_output=True, timeout=10
            )
            if filepath.exists():
                return True, f"Screenshot saved to your Desktop as screenshot_{ts}.png"
        except Exception:
            pass
        return False, "Could not take a screenshot."

    def set_system_volume(self, delta: int | None = None, absolute: int | None = None) -> tuple[bool, str]:
        if _PYCAW:
            try:
                devices = AudioUtilities.GetSpeakers()
                interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                volume = cast(interface, POINTER(IAudioEndpointVolume))
                current = round(volume.GetMasterVolumeLevelScalar() * 100)
                target = max(0, min(100, absolute if absolute is not None else current + (delta or 0)))
                volume.SetMasterVolumeLevelScalar(target / 100.0, None)
                return True, f"System volume set to {target} percent."
            except Exception as e:
                logging.warning("pycaw volume control failed: %s", e)
        # PowerShell fallback using WScript.Shell SendKeys
        try:
            if absolute is not None:
                target = max(0, min(100, absolute))
                # Set to 0 first then increase
                press_down = 50
                press_up = target // 2
            else:
                press_down = max(0, -(delta or 0) // 2) if (delta or 0) < 0 else 0
                press_up   = max(0,  (delta or 0) // 2) if (delta or 0) > 0 else 0
            ps = (
                f"$ws = New-Object -ComObject WScript.Shell; "
                f"for($i=0;$i -lt {press_down};$i++){{$ws.SendKeys([char]174)}}; "
                f"for($i=0;$i -lt {press_up};$i++){{$ws.SendKeys([char]175)}}"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=5)
            return True, "System volume adjusted."
        except Exception as e:
            return False, f"Could not change system volume: {e}"

    def lock_screen(self) -> tuple[bool, str]:
        subprocess.Popen("rundll32.exe user32.dll,LockWorkStation", shell=True)
        return True, "Locking your screen."

    def shutdown(self) -> tuple[bool, str]:
        subprocess.Popen("shutdown /s /t 30", shell=True)
        return True, "Shutting down in 30 seconds. Say cancel shutdown to abort."

    def restart(self) -> tuple[bool, str]:
        subprocess.Popen("shutdown /r /t 30", shell=True)
        return True, "Restarting in 30 seconds. Say cancel shutdown to abort."

    def cancel_shutdown(self) -> tuple[bool, str]:
        subprocess.Popen("shutdown /a", shell=True)
        return True, "Shutdown cancelled."
