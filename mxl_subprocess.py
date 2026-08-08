"""subprocess wrappers that keep console windows from flashing on Windows.

The Windows launcher runs everything through pythonw.exe, a GUI-subsystem
process with no console attached. git.exe is a console-subsystem program:
when a process with no console starts one, Windows allocates a fresh console
window and shows it for as long as the child runs. A single install spawns
roughly two dozen git processes and an uninstall about twenty, so without
this the user sees that many windows blink across the screen.

CREATE_NO_WINDOW (0x08000000) tells CreateProcess to skip that allocation.
The flag does not exist off Windows and subprocess raises if it is passed
there, so every wrapper here adds it only when sys.platform == "win32", and
only when the caller has not already supplied its own creationflags.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

CREATE_NO_WINDOW = 0x08000000


def _hide_console(kwargs: dict[str, Any]) -> dict[str, Any]:
    if sys.platform == "win32" and "creationflags" not in kwargs:
        kwargs = dict(kwargs, creationflags=CREATE_NO_WINDOW)
    return kwargs


def run(*args: Any, **kwargs: Any) -> "subprocess.CompletedProcess[Any]":
    """subprocess.run, without a flashing console window on Windows."""

    return subprocess.run(*args, **_hide_console(kwargs))


def popen(*args: Any, **kwargs: Any) -> "subprocess.Popen[Any]":
    """subprocess.Popen, without a flashing console window on Windows."""

    return subprocess.Popen(*args, **_hide_console(kwargs))


def check_output(*args: Any, **kwargs: Any) -> Any:
    """subprocess.check_output, without a flashing console window on Windows."""

    return subprocess.check_output(*args, **_hide_console(kwargs))
