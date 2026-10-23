"""Where VagOn keeps backups, journals and user data on each platform."""

from __future__ import annotations

import os
import sys


def is_pythonista() -> bool:
    if "Pythonista" in sys.executable or "com.omz-software" in os.environ.get("HOME", ""):
        return True
    import importlib.util
    try:  # Pythonista-only modules: ui (UIKit) and cb (CoreBluetooth)
        return importlib.util.find_spec("cb") is not None and importlib.util.find_spec("ui") is not None
    except (ImportError, ValueError):
        return False


def is_ios() -> bool:
    return sys.platform == "ios" or is_pythonista() or \
        os.path.expanduser("~").startswith("/private/var/mobile")


def data_dir() -> str:
    """$VAGON_HOME, else ~/Documents/VagOn on iOS (visible in the Files app), else ~/.vagon."""
    env = os.environ.get("VAGON_HOME")
    if env:
        path = os.path.expanduser(env)
    elif is_ios():
        path = os.path.join(os.path.expanduser("~"), "Documents", "VagOn")
    else:
        path = os.path.join(os.path.expanduser("~"), ".vagon")
    os.makedirs(path, exist_ok=True)
    return path


def sub_dir(*parts: str) -> str:
    path = os.path.join(data_dir(), *parts)
    os.makedirs(path, exist_ok=True)
    return path


def package_data(*parts: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", *parts)
