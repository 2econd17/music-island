"""Глобальные горячие клавиши Windows (RegisterHotKey).

Работают, даже когда фокус в игре/браузере/любом другом приложении.
Формат строки — как в Qt: "Ctrl+Alt+M", "Ctrl+Shift+Right", "Win+F9" и т.п.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from typing import Callable, Optional

from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312

MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "meta": MOD_WIN, "super": MOD_WIN,
}

NAMED_KEYS = {
    "space": 0x20, "pgup": 0x21, "pageup": 0x21, "pgdown": 0x22, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "ins": 0x2D, "insert": 0x2D, "del": 0x2E, "delete": 0x2E,
    "backspace": 0x08, "tab": 0x09, "return": 0x0D, "enter": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "pause": 0x13, "print": 0x2C, "scrolllock": 0x91,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "media play": 0xB3, "media next": 0xB0, "media previous": 0xB1, "media stop": 0xB2,
    "volume mute": 0xAD, "volume down": 0xAE, "volume up": 0xAF,
}

# Если клавиши записаны в русской раскладке — переводим в физические клавиши
_RU = "йцукенгшщзхъфывапролджэячсмитьбю"
_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,."
RU_TO_EN = dict(zip(_RU, _EN))


def parse_hotkey(text: str) -> Optional[tuple[int, int]]:
    """'Ctrl+Alt+M' -> (модификаторы, virtual-key code) или None."""
    if not text or not text.strip():
        return None
    raw = text.strip()
    # "Ctrl++" — клавиша плюс
    parts = [x for x in raw.split("+")]
    if raw.endswith("++"):
        parts = parts[:-2] + ["="]
    mods, key, keypad = 0, None, False
    for part in parts:
        name = part.strip().lower()
        if not name:
            continue
        if name == "num":          # Qt пишет клавиши цифрового блока как "Num+1"
            keypad = True
            continue
        if name in MODIFIERS:
            mods |= MODIFIERS[name]
            continue
        if key is not None:
            return None  # две «обычные» клавиши
        name = RU_TO_EN.get(name, name)
        if keypad and len(name) == 1 and name.isdigit():
            key = 0x60 + int(name)
        elif len(name) == 1 and (name.isascii() and name.isalnum()):
            key = ord(name.upper())
        elif name in NAMED_KEYS:
            key = NAMED_KEYS[name]
        elif name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
            key = 0x6F + int(name[1:])
        elif name.startswith("num") and name[3:].strip().isdigit():
            key = 0x60 + int(name[3:].strip())
        else:
            return None
    if key is None:
        return None
    return mods, key


class HotkeyManager(QWidget):
    """Невидимое окно, которое принимает WM_HOTKEY."""

    def __init__(self):
        super().__init__(None)
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next_id = 1
        self.errors: list[str] = []
        if sys.platform == "win32":
            self.winId()  # создаём HWND, окно при этом не показываем

    def register_all(self, bindings: dict[str, str], actions: dict[str, Callable[[], None]]) -> list[str]:
        """Перерегистрировать все сочетания. Возвращает список проблем."""
        self.unregister_all()
        self.errors = []
        if sys.platform != "win32":
            return self.errors
        used: set[tuple[int, int]] = set()
        for action, text in bindings.items():
            if not text or action not in actions:
                continue
            parsed = parse_hotkey(text)
            if parsed is None:
                self.errors.append(f"«{text}»: не понимаю такое сочетание")
                continue
            if parsed in used:
                self.errors.append(f"«{text}»: назначено на два действия")
                continue
            mods, vk = parsed
            hid = self._next_id
            self._next_id += 1
            ok = ctypes.windll.user32.RegisterHotKey(int(self.winId()), hid, mods | MOD_NOREPEAT, vk)
            if not ok:
                self.errors.append(f"«{text}»: уже занято другой программой")
                continue
            used.add(parsed)
            self._callbacks[hid] = actions[action]
        for e in self.errors:
            log.warning("Горячая клавиша %s", e)
        return self.errors

    def unregister_all(self) -> None:
        if sys.platform == "win32":
            for hid in list(self._callbacks):
                ctypes.windll.user32.UnregisterHotKey(int(self.winId()), hid)
        self._callbacks.clear()

    def nativeEvent(self, event_type, message):
        if sys.platform == "win32":
            try:
                from ctypes import wintypes
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY:
                    cb = self._callbacks.get(int(msg.wParam))
                    if cb is not None:
                        cb()
                        return True, 0
            except Exception:
                log.exception("Ошибка обработки горячей клавиши")
        return False, 0
