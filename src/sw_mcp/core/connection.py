"""Attach to (or start) SolidWorks. Runs only on the COM worker thread.

Rules taken from the reference implementations:
- Attach to the running instance through the Running Object Table first.
- Never start a second SLDWORKS.exe: if a process exists but COM cannot see it yet,
  it is still loading, stuck on a dialog, or running at a different privilege level.
- Start SolidWorks as a normal detached process (not through COM), so the tool call
  returns at once instead of blocking for a minute while it loads.
- Late binding only (no makepy cache), so any SolidWorks version works.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import winreg
from typing import Any

import pythoncom
import pywintypes
import win32com.client.dynamic

from .. import config
from .com_utils import call, try_call
from .errors import Code, SwError

log = logging.getLogger(__name__)

PROGID = "SldWorks.Application"
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_NEW_GROUP = 0x00000200

_local = threading.local()  # one cached app handle per COM thread
_launch_lock = threading.Lock()
_launched_at: float | None = None
LAUNCH_GRACE_S = 180.0

_STARTING_FIX = (
    "Wait about 20 seconds, then call get_status again. If this repeats, ask the user to check the "
    "SolidWorks window for an open dialog box. SolidWorks and OpenCode must run as the same Windows "
    "user, and either both or neither with 'Run as administrator'."
)


# ---------------------------------------------------------------- process helpers
def solidworks_pids() -> list[int]:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq SLDWORKS.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        ).stdout
    except Exception:  # noqa: BLE001 - diagnostics must never break a tool
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower().startswith("sldworks"):
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def find_solidworks_exe() -> str | None:
    """Locate SLDWORKS.exe from the COM registration (works for every installed version)."""
    try:
        clsid = str(pywintypes.IID(PROGID))
    except pywintypes.com_error:
        return None
    for view in (winreg.KEY_WOW64_64KEY, 0):
        try:
            with winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32", 0, winreg.KEY_READ | view
            ) as key:
                raw, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        raw = str(raw).strip()
        if raw.startswith('"'):
            path = raw[1:].split('"', 1)[0]
        else:
            low = raw.lower()
            end = low.find(".exe")
            path = raw[: end + 4] if end >= 0 else raw.split(" ")[0]
        if os.path.isfile(path):
            return path
    return None


def _launch(exe: str) -> None:
    subprocess.Popen(  # noqa: S603 - path comes from the COM registration
        [exe],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, creationflags=_DETACHED_PROCESS | _NEW_GROUP,
    )


def _attach_running() -> Any:
    unknown = pythoncom.GetActiveObject(PROGID)
    disp = unknown.QueryInterface(pythoncom.IID_IDispatch)
    return win32com.client.dynamic.Dispatch(disp)


# ---------------------------------------------------------------- public API
def reset() -> None:
    """Forget this thread's cached handle (after a disconnect, or on thread exit)."""
    _local.app = None


def is_alive(app: Any) -> bool:
    try:
        call(app, "RevisionNumber")
        return True
    except Exception:  # noqa: BLE001
        return False


def get_app(allow_launch: bool = False) -> Any:
    """Return a live SolidWorks application object, or raise SwError explaining why not."""
    global _launched_at
    app = getattr(_local, "app", None)
    if app is not None:
        if is_alive(app):
            return app
        log.info("cached SolidWorks handle is dead; re-attaching")
        reset()

    try:
        app = _attach_running()
    except pywintypes.com_error:
        pids = solidworks_pids()
        if pids:
            raise SwError(
                Code.SW_STARTING,
                f"SolidWorks is running (process {', '.join(map(str, pids))}) but is not answering yet. "
                "It may still be loading, or a dialog box is open.",
                _STARTING_FIX,
            ) from None
        if not allow_launch or not config.AUTO_LAUNCH:
            fix = (
                "Call get_status to start it, or ask the user to open SolidWorks."
                if config.AUTO_LAUNCH
                else "Ask the user to start SolidWorks, then call get_status."
            )
            raise SwError(Code.NO_SOLIDWORKS, "SolidWorks is not running.", fix) from None
        with _launch_lock:
            recently = _launched_at is not None and time.monotonic() - _launched_at < LAUNCH_GRACE_S
            if not recently:
                exe = find_solidworks_exe()
                if exe is None:
                    raise SwError(
                        Code.NO_SOLIDWORKS,
                        "SolidWorks does not seem to be installed on this PC (SldWorks.Application is not registered).",
                        "Ask the user to install or repair SolidWorks.",
                    ) from None
                log.info("starting SolidWorks: %s", exe)
                _launch(exe)
                _launched_at = time.monotonic()
        raise SwError(
            Code.SW_STARTING,
            "SolidWorks was not running, so it is being started now. This takes 30 to 90 seconds.",
            "Wait about 30 seconds, then call get_status again.",
        ) from None

    try:
        if not call(app, "Visible"):
            app.Visible = True  # the user must see what the automation does
    except Exception:  # noqa: BLE001 - visibility is best effort
        pass
    _local.app = app
    return app


def version_label(revision: str | None) -> str | None:
    """'33.1.0' -> '2025 SP1'. SolidWorks major revision + 1992 = release year."""
    if not revision:
        return None
    parts = str(revision).split(".")
    try:
        year = int(parts[0]) + 1992
    except ValueError:
        return str(revision)
    sp = parts[1] if len(parts) > 1 else "0"
    return f"{year} SP{sp}"


def active_doc(app: Any) -> Any:
    return try_call(app, "ActiveDoc")
