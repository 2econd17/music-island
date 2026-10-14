"""Точка входа: трей, остров, горячие клавиши, настройки."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import autostart
from .audio import AudioVisualizer
from .config import APP_ID, APP_NAME, CONFIG_DIR, CORNERS, HOTKEY_ACTIONS, LOG_PATH, Config
from .hotkeys import HotkeyManager
from .media import DemoMedia, MediaWatcher
from .overlay import IslandOverlay
from .settings import SettingsDialog

log = logging.getLogger("island")


def setup_logging(debug: bool) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=1, encoding="utf-8")]
    if sys.stderr is not None:          # у pythonw/собранного exe консоли нет
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)

    def hook(exc_type, exc, tb):
        log.critical("Необработанная ошибка", exc_info=(exc_type, exc, tb))
    sys.excepthook = hook


def make_icon() -> QIcon:
    """Иконка рисуется кодом: чёрная «таблетка» с барами."""
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(size * 0.04, size * 0.24, size * 0.92, size * 0.52)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#000000"))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setBrush(QColor(255, 255, 255, 40))
        heights = (0.45, 0.8, 0.6, 1.0, 0.5)
        bw = size * 0.075
        gap = size * 0.06
        total = len(heights) * bw + (len(heights) - 1) * gap
        x = (size - total) / 2
        colors = ("#ff4d6d", "#ff7a59", "#ffb703", "#ff7a59", "#ff4d6d")
        for h, c in zip(heights, colors):
            hh = max(bw, r.height() * 0.62 * h)
            p.setBrush(QColor(c))
            p.drawRoundedRect(QRectF(x, size / 2 - hh / 2, bw, hh), bw / 2, bw / 2)
            x += bw + gap
        p.end()
        icon.addPixmap(pm)
    return icon


def dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    base, alt, text = QColor(32, 32, 36), QColor(44, 44, 50), QColor(235, 235, 240)
    pal.setColor(QPalette.ColorRole.Window, base)
    pal.setColor(QPalette.ColorRole.WindowText, text)
    pal.setColor(QPalette.ColorRole.Base, QColor(24, 24, 28))
    pal.setColor(QPalette.ColorRole.AlternateBase, alt)
    pal.setColor(QPalette.ColorRole.ToolTipBase, alt)
    pal.setColor(QPalette.ColorRole.ToolTipText, text)
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.Button, alt)
    pal.setColor(QPalette.ColorRole.ButtonText, text)
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#ff4d6d"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.Link, QColor("#ff7a8f"))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(120, 120, 128))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(120, 120, 128))
    app.setPalette(pal)


def _add(menu: QMenu, text: str, fn) -> QAction:
    act = QAction(text, menu)
    act.triggered.connect(lambda _=False: fn())
    menu.addAction(act)
    return act


class IslandApp:
    def __init__(self, app: QApplication, demo: bool):
        self.app = app
        self.cfg = Config.load()
        first_run = not Config.exists()

        self.icon = make_icon()
        app.setWindowIcon(self.icon)

        self.viz = AudioVisualizer(self.cfg.sensitivity)
        if self.cfg.visualizer and not demo:
            self.viz.start()

        if demo or sys.platform != "win32":
            self.media = DemoMedia()
        else:
            self.media = MediaWatcher(self.cfg.preferred_apps, self.cfg.only_preferred)

        self.menu = QMenu()
        self.overlay = IslandOverlay(self.cfg, self.viz, self.menu)
        self.overlay.play_pause_clicked.connect(self.media.play_pause)
        self.overlay.next_clicked.connect(self.media.next)
        self.overlay.prev_clicked.connect(self.media.previous)
        self.media.changed.connect(self.overlay.set_track)

        self.hotkeys = HotkeyManager()
        self.settings_dialog: SettingsDialog | None = None

        self._build_menu()
        self.tray = QSystemTrayIcon(self.icon)
        self.tray.setToolTip(APP_NAME)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

        errors = self._register_hotkeys()
        self.media.start()
        autostart.refresh_path()

        if first_run:
            self.cfg.save()
            self.tray.showMessage(APP_NAME, "Остров запущен. Настройки — по иконке в трее.",
                                  self.icon, 5000)
            self.open_settings()
        elif errors:
            self.tray.showMessage(APP_NAME, "Не удалось назначить горячие клавиши:\n" + "\n".join(errors),
                                  QSystemTrayIcon.MessageIcon.Warning, 7000)

    # ------------------------------------------------------------ меню
    def _build_menu(self) -> None:
        m = self.menu
        m.clear()
        title = m.addAction(APP_NAME)
        title.setEnabled(False)
        m.addSeparator()

        self.act_enabled = QAction("Показывать остров", m, checkable=True)
        self.act_enabled.setChecked(self.cfg.enabled)
        self.act_enabled.toggled.connect(self._set_enabled)
        m.addAction(self.act_enabled)

        pos = m.addMenu("Положение")
        group = QActionGroup(pos)
        for key, name in CORNERS.items():
            a = QAction(name, pos, checkable=True)
            a.setChecked(self.cfg.corner == key)
            a.triggered.connect(lambda _=False, k=key: self._set_corner(k))
            group.addAction(a)
            pos.addAction(a)

        _add(m, "Настройки…", self.open_settings)
        m.addSeparator()
        _add(m, "⏯  Пауза / воспроизведение", self.media.play_pause)
        _add(m, "⏭  Следующий", self.media.next)
        _add(m, "⏮  Предыдущий", self.media.previous)
        m.addSeparator()
        _add(m, "Выход", self.quit)

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:   # левый клик
            self.open_settings()

    def _set_enabled(self, value: bool) -> None:
        if self.cfg.enabled != value:
            self.overlay.toggle_enabled()
            self.cfg.save()

    def toggle_enabled(self) -> None:
        self.act_enabled.setChecked(not self.cfg.enabled)

    def _set_corner(self, key: str) -> None:
        self.cfg.corner = key
        self.cfg.save()
        self.overlay.apply_config()

    # ------------------------------------------------------------ горячие клавиши
    def _register_hotkeys(self) -> list[str]:
        actions = {
            "toggle": self.toggle_enabled,
            "play_pause": self.media.play_pause,
            "next": self.media.next,
            "prev": self.media.previous,
            "expand": self.overlay.toggle_expanded,
        }
        assert set(actions) == set(HOTKEY_ACTIONS)
        return self.hotkeys.register_all(self.cfg.hotkeys, actions)

    # ------------------------------------------------------------ настройки
    def _apply_live(self) -> None:
        self.overlay.apply_config()
        self.viz.sensitivity = self.cfg.sensitivity
        if self.cfg.visualizer:
            self.viz.start()
        if isinstance(self.media, MediaWatcher):
            self.media.preferred_apps = [p.lower() for p in self.cfg.preferred_apps]
            self.media.only_preferred = self.cfg.only_preferred
        self.act_enabled.blockSignals(True)
        self.act_enabled.setChecked(self.cfg.enabled)
        self.act_enabled.blockSignals(False)

    def _save_settings(self) -> list[str]:
        self.cfg.sanitize()
        self.cfg.save()
        self._apply_live()
        self._build_menu()
        errors = self._register_hotkeys()
        if self.settings_dialog is not None:
            # Пока открыты настройки, сочетания должны доходить до полей ввода
            self.hotkeys.unregister_all()
        return errors

    def open_settings(self) -> None:
        if self.settings_dialog is not None:
            self.settings_dialog.show()
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return
        track_app = lambda: (self.overlay.track.app_id if self.overlay.track else "")  # noqa: E731
        dlg = SettingsDialog(self.cfg, self._apply_live, self._save_settings, track_app)
        dlg.setWindowIcon(self.icon)
        self.settings_dialog = dlg
        self.overlay.preview = True
        self.hotkeys.unregister_all()   # иначе новое сочетание нельзя «записать» в поле

        def closed(_result: int) -> None:
            self.overlay.preview = False
            self.settings_dialog = None
            self._build_menu()
            self._register_hotkeys()
            dlg.deleteLater()

        dlg.finished.connect(closed)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def quit(self) -> None:
        self.hotkeys.unregister_all()
        self.media.stop()
        self.viz.stop()
        self.tray.hide()
        self.app.quit()


def main() -> None:
    parser = argparse.ArgumentParser(prog="MusicIsland", description=APP_NAME)
    parser.add_argument("--demo", action="store_true", help="фейковый трек для проверки внешнего вида")
    parser.add_argument("--settings", action="store_true", help="сразу открыть настройки")
    parser.add_argument("--debug", action="store_true", help="подробный лог")
    args, _ = parser.parse_known_args()

    setup_logging(args.debug)
    if sys.platform == "win32":
        try:  # своя иконка в панели задач вместо иконки Python
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    dark_palette(app)

    # Одна копия: повторный запуск просто открывает настройки уже работающей
    server_name = f"{APP_ID}-{os.environ.get('USERNAME') or os.environ.get('USER') or 'user'}"
    sock = QLocalSocket()
    sock.connectToServer(server_name)
    if sock.waitForConnected(300):
        sock.write(b"settings")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return
    QLocalServer.removeServer(server_name)
    server = QLocalServer()
    server.listen(server_name)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("Системный трей недоступен")

    try:
        island = IslandApp(app, demo=args.demo)
    except Exception as e:
        log.exception("Не удалось запустить")
        QMessageBox.critical(None, APP_NAME, f"Не удалось запустить:\n{e}\n\nЛог: {LOG_PATH}")
        sys.exit(1)

    def on_connection() -> None:
        c = server.nextPendingConnection()
        if c is not None:
            c.readyRead.connect(lambda: (c.readAll(), island.open_settings()))

    server.newConnection.connect(on_connection)
    if args.settings:
        island.open_settings()
    log.info("Запущено")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
