"""Настройки приложения: хранятся в %APPDATA%/MusicIsland/config.json.

Всё это меняется в окне «Настройки» (иконка в трее → Настройки…),
но файл можно редактировать и руками — неизвестные/битые поля игнорируются.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields

APP_NAME = "Music Island"
APP_ID = "MusicIsland"
VERSION = "1.0.0"

CONFIG_DIR = os.path.join(
    os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config"),
    APP_ID,
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "log.txt")

CORNERS = {
    "top-right": "Сверху справа",
    "top-left": "Сверху слева",
    "top-center": "Сверху по центру (как Dynamic Island)",
    "bottom-right": "Снизу справа",
    "bottom-left": "Снизу слева",
    "bottom-center": "Снизу по центру",
}

HOTKEY_ACTIONS = {
    "toggle": "Показать / скрыть остров",
    "play_pause": "Пауза / воспроизведение",
    "next": "Следующий трек",
    "prev": "Предыдущий трек",
    "expand": "Развернуть / свернуть остров",
}

log = logging.getLogger(__name__)


@dataclass
class Config:
    # --- Расположение ---
    corner: str = "top-right"
    margin_x: int = 16
    margin_y: int = 12
    screen: int = -1               # -1 = основной монитор, иначе номер монитора

    # --- Размер ---
    scale: float = 1.0             # общий масштаб (0.6 … 2.0)
    compact_width: int = 236       # ширина свёрнутого острова
    compact_height: int = 36       # высота свёрнутого острова
    expanded_width: int = 372      # ширина развёрнутого острова
    radius: int = 30               # скругление развёрнутого острова

    # --- Внешний вид ---
    font_family: str = ""          # пусто = системный (Segoe UI)
    font_size: float = 12.5        # размер текста в свёрнутом виде
    bold_title: bool = True
    bg_color: str = "#000000"
    bg_opacity: float = 1.0        # прозрачность фона (0 … 1)
    opacity: float = 1.0           # прозрачность всего острова (0.2 … 1)
    border: bool = True            # тонкая светлая обводка
    text_color: str = "#ffffff"
    accent_mode: str = "cover"     # "cover" = цвет из обложки, "custom" = accent_color
    accent_color: str = "#ffffff"
    show_artist: bool = True
    show_cover: bool = True
    bars: int = 5                  # количество баров в свёрнутом виде

    # --- Поведение ---
    enabled: bool = True           # остров включён (переключается горячей клавишей)
    hide_when_paused_after: float = 8.0   # сек. на паузе до исчезновения, 0 = не прятать
    expand_on_hover: bool = True
    click_through: bool = False    # True = мышь «проходит сквозь» остров
    visualizer: bool = True        # реальный визуализатор по звуку системы
    sensitivity: float = 1.0       # чувствительность визуализатора (0.3 … 3)

    # --- Плееры ---
    # Подстроки, которые ищутся в ID приложения. "yandex" ловит и приложение
    # Яндекс Музыки. Чтобы показывать только его — only_preferred = true.
    preferred_apps: list = field(default_factory=lambda: ["yandex"])
    only_preferred: bool = False

    # --- Горячие клавиши (пусто = выключено) ---
    hotkeys: dict = field(default_factory=lambda: {
        "toggle": "Ctrl+Alt+M",
        "play_pause": "Ctrl+Alt+P",
        "next": "Ctrl+Alt+Right",
        "prev": "Ctrl+Alt+Left",
        "expand": "Ctrl+Alt+E",
    })

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            defaults = cls()
            for fl in fields(cls):
                if fl.name not in data:
                    continue
                v, d = data[fl.name], getattr(defaults, fl.name)
                if isinstance(d, bool):
                    v = bool(v)
                elif isinstance(d, (int, float)) and not isinstance(v, (int, float)):
                    continue
                elif isinstance(d, int) and isinstance(v, float) and not isinstance(d, bool):
                    v = int(v)
                elif isinstance(d, dict):
                    if not isinstance(v, dict):
                        continue
                    v = {**d, **{k: str(x or "") for k, x in v.items() if k in d}}
                elif isinstance(d, list) and not isinstance(v, list):
                    continue
                elif isinstance(d, str) and not isinstance(v, str):
                    continue
                setattr(cfg, fl.name, v)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Не удалось прочитать config.json, использую настройки по умолчанию")
        cfg.sanitize()
        return cfg

    def sanitize(self) -> None:
        if self.corner not in CORNERS:
            self.corner = "top-right"
        if self.accent_mode not in ("cover", "custom"):
            self.accent_mode = "cover"
        self.scale = min(2.5, max(0.5, float(self.scale)))
        self.compact_width = min(600, max(120, int(self.compact_width)))
        self.compact_height = min(80, max(24, int(self.compact_height)))
        self.expanded_width = min(700, max(300, int(self.expanded_width)))
        self.radius = min(60, max(0, int(self.radius)))
        self.font_size = min(24.0, max(8.0, float(self.font_size)))
        self.bg_opacity = min(1.0, max(0.0, float(self.bg_opacity)))
        self.opacity = min(1.0, max(0.15, float(self.opacity)))
        self.bars = min(12, max(0, int(self.bars)))
        self.sensitivity = min(4.0, max(0.2, float(self.sensitivity)))

    def copy(self) -> "Config":
        return copy.deepcopy(self)

    @staticmethod
    def exists() -> bool:
        return os.path.exists(CONFIG_PATH)

    def save(self) -> None:
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)
        except Exception:
            log.exception("Не удалось сохранить config.json")
