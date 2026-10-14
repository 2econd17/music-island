"""Автозапуск с Windows через HKCU\\...\\Run (права администратора не нужны)."""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MusicIsland"


def launch_command() -> str:
    """Команда запуска текущей копии приложения."""
    if getattr(sys, "frozen", False):          # собранный MusicIsland.exe
        return f'"{os.path.abspath(sys.executable)}"'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw                               # без чёрного окна консоли
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return f'"{exe}" "{os.path.join(root, "run.pyw")}"'


def _key(write: bool = False):
    import winreg
    access = winreg.KEY_READ | (winreg.KEY_SET_VALUE if write else 0)
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def current() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with _key() as k:
            value, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return value
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("Не удалось прочитать автозапуск")
        return None


def is_enabled() -> bool:
    return current() is not None


def set_enabled(enabled: bool) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with _key(write=True) as k:
            if enabled:
                winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(k, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        log.exception("Не удалось изменить автозапуск")
        return False


def refresh_path() -> None:
    """Если .exe перенесли в другую папку — обновить путь в автозапуске."""
    cur = current()
    if cur is not None and cur != launch_command():
        set_enabled(True)
