"""Окно настроек. Все изменения сразу видны на острове (предпросмотр)."""
from __future__ import annotations

import os
import sys
from typing import Callable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFontComboBox, QFormLayout, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSlider, QSpinBox, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from . import autostart
from .config import APP_NAME, CONFIG_DIR, CORNERS, HOTKEY_ACTIONS, VERSION, Config

REPO_URL = "https://github.com/YOUR_GITHUB_NAME/music-island"


class ColorButton(QPushButton):
    def __init__(self, color: str, on_pick: Callable[[str], None]):
        super().__init__()
        self._on_pick = on_pick
        self.setFixedWidth(110)
        self.set_color(color)
        self.clicked.connect(self._pick)

    def set_color(self, color: str) -> None:
        c = QColor(color)
        if not c.isValid():
            c = QColor("#000000")
        self._color = c
        fg = "#000" if c.lightnessF() > 0.55 else "#fff"
        self.setText(c.name().upper())
        self.setStyleSheet(
            f"QPushButton {{ background:{c.name()}; color:{fg}; border:1px solid #666;"
            f" border-radius:6px; padding:4px 8px; }}")

    def _pick(self) -> None:
        c = QColorDialog.getColor(self._color, self, "Выбор цвета")
        if c.isValid():
            self.set_color(c.name())
            self._on_pick(c.name())


def _slider(lo: int, hi: int, value: int, suffix: str, on_change: Callable[[int], None]) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    s = QSlider(Qt.Orientation.Horizontal)
    s.setRange(lo, hi)
    s.setValue(value)
    lbl = QLabel(f"{value}{suffix}")
    lbl.setMinimumWidth(48)

    def changed(v: int) -> None:
        lbl.setText(f"{v}{suffix}")
        on_change(v)

    s.valueChanged.connect(changed)
    lay.addWidget(s, 1)
    lay.addWidget(lbl)
    return w


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, on_change: Callable[[], None],
                 on_save: Callable[[], list[str]], current_app: Callable[[], str]):
        super().__init__(None)
        self.cfg = cfg
        self._backup = cfg.copy()
        self._on_change = on_change
        self._on_save = on_save
        self._current_app = current_app
        self.setWindowTitle(f"{APP_NAME} — настройки")
        self.setMinimumWidth(520)

        tabs = QTabWidget()
        tabs.addTab(self._tab_look(), "Внешний вид")
        tabs.addTab(self._tab_size(), "Размер и место")
        tabs.addTab(self._tab_behavior(), "Поведение")
        tabs.addTab(self._tab_hotkeys(), "Горячие клавиши")
        tabs.addTab(self._tab_system(), "Система")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        hint = QLabel("Изменения сразу видны на острове. «Сохранить» — запомнить их.")
        hint.setStyleSheet("color: gray;")

        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(hint)
        lay.addWidget(buttons)

    # ------------------------------------------------------------ helpers
    def _set(self, name: str, value) -> None:
        setattr(self.cfg, name, value)
        self._on_change()

    def _check(self, text: str, name: str) -> QCheckBox:
        cb = QCheckBox(text)
        cb.setChecked(bool(getattr(self.cfg, name)))
        cb.toggled.connect(lambda v: self._set(name, v))
        return cb

    def _spin(self, name: str, lo: int, hi: int, suffix: str = "") -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setValue(int(getattr(self.cfg, name)))
        if suffix:
            sp.setSuffix(suffix)
        sp.valueChanged.connect(lambda v: self._set(name, v))
        return sp

    # ------------------------------------------------------------ вкладки
    def _tab_look(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)

        font_row = QWidget()
        fl = QHBoxLayout(font_row)
        fl.setContentsMargins(0, 0, 0, 0)
        self.font_box = QFontComboBox()
        if self.cfg.font_family:
            self.font_box.setCurrentFont(QFont(self.cfg.font_family))
        else:
            self.font_box.setCurrentFont(QFont("Segoe UI"))
        self.font_box.currentFontChanged.connect(lambda fnt: self._set("font_family", fnt.family()))
        reset_font = QToolButton()
        reset_font.setText("Системный")
        reset_font.clicked.connect(self._reset_font)
        fl.addWidget(self.font_box, 1)
        fl.addWidget(reset_font)
        f.addRow("Шрифт:", font_row)

        size = QDoubleSpinBox()
        size.setRange(8, 24)
        size.setSingleStep(0.5)
        size.setValue(self.cfg.font_size)
        size.setSuffix(" px")
        size.valueChanged.connect(lambda v: self._set("font_size", v))
        f.addRow("Размер текста:", size)
        f.addRow("", self._check("Жирное название трека", "bold_title"))

        f.addRow("Цвет текста:", ColorButton(self.cfg.text_color, lambda c: self._set("text_color", c)))
        f.addRow("Цвет фона:", ColorButton(self.cfg.bg_color, lambda c: self._set("bg_color", c)))
        f.addRow("Непрозрачность фона:", _slider(
            0, 100, round(self.cfg.bg_opacity * 100), " %", lambda v: self._set("bg_opacity", v / 100)))
        f.addRow("Непрозрачность острова:", _slider(
            15, 100, round(self.cfg.opacity * 100), " %", lambda v: self._set("opacity", v / 100)))
        f.addRow("", self._check("Тонкая обводка", "border"))

        acc_row = QWidget()
        al = QHBoxLayout(acc_row)
        al.setContentsMargins(0, 0, 0, 0)
        mode = QComboBox()
        mode.addItem("Из обложки трека", "cover")
        mode.addItem("Свой цвет", "custom")
        mode.setCurrentIndex(0 if self.cfg.accent_mode == "cover" else 1)
        acc_btn = ColorButton(self.cfg.accent_color, lambda c: self._set("accent_color", c))
        acc_btn.setEnabled(self.cfg.accent_mode == "custom")

        def mode_changed(_i: int) -> None:
            m = mode.currentData()
            acc_btn.setEnabled(m == "custom")
            self._set("accent_mode", m)

        mode.currentIndexChanged.connect(mode_changed)
        al.addWidget(mode, 1)
        al.addWidget(acc_btn)
        f.addRow("Цвет визуализатора:", acc_row)

        f.addRow("Баров в свёрнутом виде:", self._spin("bars", 0, 12))
        f.addRow("", self._check("Показывать обложку", "show_cover"))
        f.addRow("", self._check("Показывать исполнителя рядом с названием", "show_artist"))
        return w

    def _reset_font(self) -> None:
        self.font_box.blockSignals(True)
        self.font_box.setCurrentFont(QFont("Segoe UI"))
        self.font_box.blockSignals(False)
        self._set("font_family", "")

    def _tab_size(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.addRow("Масштаб:", _slider(50, 250, round(self.cfg.scale * 100), " %",
                                     lambda v: self._set("scale", v / 100)))
        f.addRow("Ширина (свёрнутый):", self._spin("compact_width", 120, 600, " px"))
        f.addRow("Высота (свёрнутый):", self._spin("compact_height", 24, 80, " px"))
        f.addRow("Ширина (развёрнутый):", self._spin("expanded_width", 300, 700, " px"))
        f.addRow("Скругление (развёрнутый):", self._spin("radius", 0, 60, " px"))

        corner = QComboBox()
        for key, title in CORNERS.items():
            corner.addItem(title, key)
        corner.setCurrentIndex(list(CORNERS).index(self.cfg.corner))
        corner.currentIndexChanged.connect(lambda _i: self._set("corner", corner.currentData()))
        f.addRow("Положение:", corner)

        screen = QComboBox()
        screen.addItem("Основной монитор", -1)
        for i, s in enumerate(QGuiApplication.screens()):
            g = s.geometry()
            screen.addItem(f"{i + 1}: {s.name()} ({g.width()}×{g.height()})", i)
        idx = screen.findData(self.cfg.screen)
        screen.setCurrentIndex(max(0, idx))
        screen.currentIndexChanged.connect(lambda _i: self._set("screen", screen.currentData()))
        f.addRow("Монитор:", screen)

        f.addRow("Отступ от края по X:", self._spin("margin_x", 0, 2000, " px"))
        f.addRow("Отступ от края по Y:", self._spin("margin_y", 0, 2000, " px"))
        return w

    def _tab_behavior(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.addRow("", self._check("Остров включён", "enabled"))

        hide = QSpinBox()
        hide.setRange(0, 3600)
        hide.setSuffix(" с")
        hide.setSpecialValueText("никогда")
        hide.setValue(int(self.cfg.hide_when_paused_after))
        hide.valueChanged.connect(lambda v: self._set("hide_when_paused_after", float(v)))
        f.addRow("Прятать на паузе через:", hide)

        f.addRow("", self._check("Разворачивать при наведении мыши", "expand_on_hover"))
        f.addRow("", self._check("Пропускать клики сквозь остров (мышь его не трогает)", "click_through"))
        f.addRow("", self._check("Визуализатор по реальному звуку системы", "visualizer"))
        f.addRow("Чувствительность:", _slider(20, 400, round(self.cfg.sensitivity * 100), " %",
                                              lambda v: self._set("sensitivity", v / 100)))

        f.addRow("", self._check("Показывать только Яндекс Музыку (и плееры из списка ниже)",
                                 "only_preferred"))
        apps = QLineEdit(", ".join(self.cfg.preferred_apps))
        apps.setPlaceholderText("yandex, spotify, chrome …")
        apps.textChanged.connect(lambda t: self._set(
            "preferred_apps", [x.strip().lower() for x in t.split(",") if x.strip()]))
        f.addRow("Приоритетные плееры:", apps)
        now = QLabel(f"Сейчас играет в: {self._current_app() or '—'}")
        now.setStyleSheet("color: gray;")
        now.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f.addRow("", now)
        return w

    def _tab_hotkeys(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self._key_edits: dict[str, QKeySequenceEdit] = {}
        for action, title in HOTKEY_ACTIONS.items():
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            ed = QKeySequenceEdit(QKeySequence(self.cfg.hotkeys.get(action, "")))
            if hasattr(ed, "setMaximumSequenceLength"):
                ed.setMaximumSequenceLength(1)
            ed.editingFinished.connect(lambda a=action, e=ed: self._hotkey_changed(a, e))
            clr = QToolButton()
            clr.setText("✕")
            clr.setToolTip("Убрать сочетание")
            clr.clicked.connect(lambda _=False, a=action, e=ed: (e.clear(), self._hotkey_changed(a, e)))
            rl.addWidget(ed, 1)
            rl.addWidget(clr)
            f.addRow(title + ":", row)
            self._key_edits[action] = ed
        info = QLabel("Кликните в поле и нажмите сочетание. Работают глобально — "
                      "даже когда активна игра или браузер. Лучше использовать Ctrl/Alt/Shift/Win "
                      "+ клавишу, чтобы не перехватить обычный ввод.")
        info.setWordWrap(True)
        info.setStyleSheet("color: gray;")
        f.addRow(info)
        self.hotkey_errors = QLabel("")
        self.hotkey_errors.setWordWrap(True)
        self.hotkey_errors.setStyleSheet("color: #e5484d;")
        f.addRow(self.hotkey_errors)
        return w

    def _hotkey_changed(self, action: str, ed: QKeySequenceEdit) -> None:
        text = ed.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        self.cfg.hotkeys[action] = text.split(", ")[0].strip()

    def _tab_system(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        auto = QCheckBox("Запускать вместе с Windows")
        auto.setChecked(autostart.is_enabled())
        auto.setEnabled(sys.platform == "win32")

        def auto_toggled(v: bool) -> None:
            if not autostart.set_enabled(v):
                QMessageBox.warning(self, APP_NAME, "Не удалось изменить автозапуск.")
                auto.blockSignals(True)
                auto.setChecked(autostart.is_enabled())
                auto.blockSignals(False)

        auto.toggled.connect(auto_toggled)
        f.addRow("", auto)

        open_dir = QPushButton("Открыть папку с настройками")
        open_dir.clicked.connect(lambda: (os.makedirs(CONFIG_DIR, exist_ok=True),
                                          QDesktopServices.openUrl(QUrl.fromLocalFile(CONFIG_DIR))))
        f.addRow("", open_dir)

        reset = QPushButton("Сбросить всё по умолчанию")
        reset.clicked.connect(self._reset_all)
        f.addRow("", reset)

        about = QLabel(f'{APP_NAME} {VERSION}<br><a href="{REPO_URL}">{REPO_URL}</a>')
        about.setOpenExternalLinks(True)
        about.setStyleSheet("color: gray;")
        f.addRow(about)
        return w

    def _reset_all(self) -> None:
        if QMessageBox.question(self, APP_NAME, "Сбросить все настройки по умолчанию?") \
                != QMessageBox.StandardButton.Yes:
            return
        fresh = Config()
        for k, v in vars(fresh).items():
            setattr(self.cfg, k, v)
        self._on_change()
        errors = self._on_save()
        self._backup = self.cfg.copy()
        QMessageBox.information(self, APP_NAME, "Готово. Откройте настройки заново, чтобы увидеть значения."
                                + ("\n\n" + "\n".join(errors) if errors else ""))
        self.accept()

    # ------------------------------------------------------------ сохранение
    def _save(self) -> None:
        errors = self._on_save()
        if errors:
            self.hotkey_errors.setText("Не все горячие клавиши заработали:\n• " + "\n• ".join(errors))
            QMessageBox.warning(self, APP_NAME, "Настройки сохранены, но часть горячих клавиш "
                                                "не удалось назначить — см. вкладку «Горячие клавиши».")
            self._backup = self.cfg.copy()
            return
        self._backup = self.cfg.copy()
        self.accept()

    def reject(self) -> None:
        for k, v in vars(self._backup.copy()).items():
            setattr(self.cfg, k, v)
        self._on_change()
        super().reject()
