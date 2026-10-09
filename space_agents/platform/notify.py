"""Desktop notifications (the OS pop-ups shown even when the app isn't in front).

No extra dependencies; each platform uses a built-in mechanism:
- macOS: `osascript` ("display notification"). Until the app is packaged as a signed .app, macOS attributes these to
  "Script Editor"; the user may need to allow notifications for it once (System Settings → Notifications).
- Windows: a toast via Windows PowerShell's WinRT bindings. Until the installer registers an AppUserModelID for Space
  Agents, toasts are attributed to "Windows PowerShell".
- Linux: `notify-send`, if installed.

Text is passed as arguments/environment variables, never spliced into a script, so agent-written text can't inject
commands. Delivery is fire-and-forget on a background process; failures are logged, never raised.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

APP_NAME = "Space Agents"
MAX_TITLE = 80
MAX_BODY = 220

# Windows PowerShell 5.1 (not pwsh 7) exposes the WinRT toast APIs.
_WINDOWS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$esc = [System.Security.SecurityElement]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>" + $esc::Escape($env:SA_TITLE) +
  "</text><text>" + $esc::Escape($env:SA_BODY) + "</text></binding></visual></toast>")
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
  [Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Notifier:
    def notify(self, title: str, body: str) -> bool:
        """Show a desktop notification. Returns True if a notifier was launched."""
        title, body = _clip(title, MAX_TITLE), _clip(body, MAX_BODY)
        try:
            if sys.platform == "darwin":
                return self._spawn(
                    [
                        "osascript",
                        "-e",
                        "on run argv",
                        "-e",
                        "display notification (item 2 of argv) with title (item 1 of argv) sound name \"default\"",
                        "-e",
                        "end run",
                        title,
                        body,
                    ]
                )
            if sys.platform == "win32":
                env = {**os.environ, "SA_TITLE": title, "SA_BODY": body}
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_TOAST]
                return self._spawn(cmd, env=env, creationflags=flags)
            if shutil.which("notify-send"):
                return self._spawn(["notify-send", "--app-name", APP_NAME, title, body])
        except OSError as e:
            log.warning("Desktop notification failed: %s", e)
            return False
        log.info("No desktop notification mechanism on this platform")
        return False

    @staticmethod
    def _spawn(cmd: list[str], **kwargs) -> bool:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        return True
