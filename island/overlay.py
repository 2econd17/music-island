"""Окно-«остров»: рисуется поверх всех окон, разворачивается при наведении."""
from __future__ import annotations

import math
import sys
import time
from typing import Optional

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QImage, QPainter,
    QPainterPath, QPen, QPixmap, QRegion,
)
from PySide6.QtWidgets import QMenu, QWidget

from .config import Config
from .media import TrackInfo

EXPANDED_H = 148   # высота развёрнутого острова (до масштабирования)
PAD = 4            # запас вокруг острова внутри окна

FONT_FALLBACKS = ["Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue", "Arial"]

PREVIEW_TRACK = TrackInfo(title="Ничего не играет", artist="Так будет выглядеть остров",
                          playing=True, duration=200, position=74)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _fmt(sec: float) -> str:
    sec = max(0, int(sec + 0.5))
    if sec >= 3600:
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60}:{sec % 60:02d}"


def _color(value: str, fallback: str) -> QColor:
    c = QColor(value)
    return c if c.isValid() else QColor(fallback)


def accent_from_image(img: QImage) -> QColor:
    """Доминирующий «яркий» цвет обложки — им красятся бары, как в iOS."""
    if img.isNull():
        return QColor("#f2f2f2")
    small = img.scaled(24, 24, Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    bins = [[0.0, 0.0, 0.0, 0.0] for _ in range(12)]  # вес, r, g, b
    total = 0.0
    for y in range(small.height()):
        for x in range(small.width()):
            c = small.pixelColor(x, y)
            h, s, v = c.hsvHueF(), c.hsvSaturationF(), c.valueF()
            if h < 0 or v < 0.22 or s < 0.18:
                continue
            w = s * s * v
            b = bins[min(11, int(h * 12))]
            b[0] += w
            b[1] += c.redF() * w
            b[2] += c.greenF() * w
            b[3] += c.blueF() * w
            total += w
    best = max(bins, key=lambda b: b[0])
    if total < 2.0 or best[0] <= 0:
        return QColor("#f2f2f2")
    c = QColor.fromRgbF(best[1] / best[0], best[2] / best[0], best[3] / best[0])
    h, s, v = c.hsvHueF(), c.hsvSaturationF(), c.valueF()
    return QColor.fromHsvF(max(h, 0.0), _clamp(max(s, 0.5), 0, 0.85), max(v, 0.85))


class Spring:
    """Пружинная анимация — даёт лёгкий «отскок» как у Dynamic Island."""

    def __init__(self, value: float = 0.0, stiffness: float = 230.0, damping: float = 0.74):
        self.value = value
        self.target = value
        self.vel = 0.0
        self.k = stiffness
        self.c = 2 * math.sqrt(stiffness) * damping

    def step(self, dt: float) -> None:
        n = max(1, int(dt / 0.008) + 1)   # подшаги для стабильности при просадках FPS
        h = dt / n
        for _ in range(n):
            a = self.k * (self.target - self.value) - self.c * self.vel
            self.vel += a * h
            self.value += self.vel * h


class IslandOverlay(QWidget):
    play_pause_clicked = Signal()
    next_clicked = Signal()
    prev_clicked = Signal()

    def __init__(self, config: Config, visualizer=None, menu: Optional[QMenu] = None):
        super().__init__(None)
        self.cfg = config
        self.viz = visualizer
        self.menu = menu
        self.preview = False          # режим предпросмотра (пока открыты настройки)

        self._click_through = None
        self._apply_flags()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setWindowTitle("Music Island")

        self.track: Optional[TrackInfo] = None
        self._last_playing = 0.0
        self._cover_key = ""
        self._cover: Optional[QPixmap] = None
        self._cover_accent = QColor("#f2f2f2")
        self._accent_shown = QColor(self._cover_accent)

        self.expand = Spring(0.0)
        self.appear = Spring(0.0, stiffness=180.0, damping=0.82)
        self._hovered = False         # «подтверждённое» наведение (после задержки)
        self._mouse_inside = False
        self._pinned = False
        self._bars_compact: list[float] = []
        self._bars_expanded = [0.0] * 6
        self._last_tick = time.monotonic()
        self._mask_rect = QRect()
        self._buttons: dict[str, QRectF] = {}

        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.timeout.connect(self._apply_hover)
        self._tick = QTimer(self)
        self._tick.setTimerType(Qt.TimerType.PreciseTimer)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(16)
        self._keeper = QTimer(self)
        self._keeper.timeout.connect(self._keep_on_top)
        self._keeper.start(2000)

        self.apply_config()

    # ------------------------------------------------------------ данные
    def set_track(self, info: Optional[TrackInfo]) -> None:
        self.track = info
        if info is not None and info.playing:
            self._last_playing = time.monotonic()
        if info is None:
            self._cover_key = ""
            self._cover = None
        elif info.cover_key != self._cover_key:
            self._cover_key = info.cover_key
            self._cover = None
            self._cover_accent = QColor("#f2f2f2")
            if info.cover:
                img = QImage.fromData(info.cover)
                if not img.isNull():
                    side = min(img.width(), img.height())
                    img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
                    self._cover = QPixmap.fromImage(img.scaled(
                        256, 256, Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
                    self._cover_accent = accent_from_image(img)
        if self.viz is not None:
            self.viz.expect_audio = bool(info and info.playing)

    def toggle_enabled(self) -> bool:
        self.cfg.enabled = not self.cfg.enabled
        if self.cfg.enabled:
            self._last_playing = time.monotonic()   # показать сразу, даже на паузе
        return self.cfg.enabled

    def toggle_expanded(self) -> None:
        self._pinned = not self._pinned
        if self._pinned:
            self._last_playing = time.monotonic()

    def apply_config(self) -> None:
        self.cfg.sanitize()
        self._apply_flags()
        s = self.cfg.scale
        self._win_w = int(math.ceil(max(self.cfg.expanded_width, self.cfg.compact_width) * s * 1.05)) + 2 * PAD
        self._win_h = int(math.ceil(max(EXPANDED_H, self.cfg.compact_height) * s * 1.08)) + 2 * PAD
        self.setFixedSize(self._win_w, self._win_h)
        if len(self._bars_compact) != self.cfg.bars:
            self._bars_compact = [0.0] * self.cfg.bars
        self._mask_rect = QRect()
        self.reposition()
        self.update()

    def _apply_flags(self) -> None:
        ct = bool(self.cfg.click_through)
        if ct == self._click_through:
            return
        was_visible = self.isVisible()
        flags = (Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.Tool                    # нет в панели задач и Alt+Tab
                 | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.NoDropShadowWindowHint)
        if ct:
            flags |= Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)
        self._click_through = ct
        if ct:
            self._hovered = self._mouse_inside = self._pinned = False
        if was_visible:
            self.show()

    @property
    def accent(self) -> QColor:
        if self.cfg.accent_mode == "custom":
            return _color(self.cfg.accent_color, "#ffffff")
        return self._cover_accent

    # ------------------------------------------------------------ геометрия
    def _screen(self):
        screens = QGuiApplication.screens()
        if 0 <= self.cfg.screen < len(screens):
            return screens[self.cfg.screen]
        return QGuiApplication.primaryScreen()

    def reposition(self) -> None:
        scr = self._screen()
        if scr is None:
            return
        g = scr.availableGeometry()
        corner = self.cfg.corner
        mx, my = self.cfg.margin_x, self.cfg.margin_y
        W, H = self._win_w, self._win_h
        if corner.endswith("right"):
            x = g.x() + g.width() - mx - W + PAD
        elif corner.endswith("left"):
            x = g.x() + mx - PAD
        else:
            x = g.x() + (g.width() - W) // 2
        if corner.startswith("bottom"):
            y = g.y() + g.height() - my - H + PAD
        else:
            y = g.y() + my - PAD
        if self.pos().x() != x or self.pos().y() != y:
            self.move(x, y)

    def _pill_rect(self) -> QRectF:
        c, s = self.cfg, self.cfg.scale
        e = self.expand.value
        a = _clamp(self.appear.value, 0.0, 1.2)
        h = _lerp(c.compact_height, EXPANDED_H, _clamp(e, -0.05, 1.06)) * s
        w = _lerp(c.compact_width, c.expanded_width, _clamp(e, -0.05, 1.04)) * s
        w = _lerp(h, w, _clamp(a, 0.0, 1.04))          # появление: из «капли» в полную ширину
        h = h * _lerp(0.7, 1.0, _clamp(a))
        W, H = self._win_w, self._win_h
        if c.corner.endswith("right"):
            x = W - PAD - w
        elif c.corner.endswith("left"):
            x = PAD
        else:
            x = (W - w) / 2
        y = H - PAD - h if c.corner.startswith("bottom") else PAD
        return QRectF(x, y, w, h)

    # ------------------------------------------------------------ логика кадра
    def _shown_track(self) -> Optional[TrackInfo]:
        if self.track is not None:
            return self.track
        return PREVIEW_TRACK if self.preview else None

    def _should_show(self) -> bool:
        if self.preview:
            return True
        t = self.track
        if t is None or not self.cfg.enabled:
            return False
        if t.playing or self._hovered or self._pinned:
            return True
        hide_after = self.cfg.hide_when_paused_after
        return hide_after <= 0 or (time.monotonic() - self._last_playing) < hide_after

    def _on_tick(self) -> None:
        now = time.monotonic()
        dt = min(0.05, now - self._last_tick)
        self._last_tick = now

        show = self._should_show()
        self.appear.target = 1.0 if show else 0.0
        want_expand = self._pinned or (self._hovered and self.cfg.expand_on_hover)
        self.expand.target = 1.0 if (want_expand and show) else 0.0

        if show and not self.isVisible():
            self.reposition()
            self.show()
            self._keep_on_top()

        self.expand.step(dt)
        self.appear.step(dt)
        self._update_bars(dt)
        self._accent_shown = _mix(self._accent_shown, self.accent, 1 - math.exp(-dt * 6))

        if not show and self.appear.value < 0.02 and self.isVisible():
            self.appear.value, self.appear.vel = 0.0, 0.0
            self.expand.value, self.expand.vel = 0.0, 0.0
            self.hide()

        if self.isVisible():
            self._update_mask()
            self.update()
            self._tick.setInterval(16)
        else:
            self._tick.setInterval(200)

    def _update_bars(self, dt: float) -> None:
        t = self._shown_track()
        playing = bool(t and t.playing)
        for bars in (self._bars_compact, self._bars_expanded):
            n = len(bars)
            if n == 0:
                continue
            if playing and self.cfg.visualizer and self.viz is not None and self.track is not None:
                target = list(self.viz.get_levels(n))
            elif playing:
                tm = time.monotonic()
                target = [0.4 + 0.3 * math.sin(tm * 5 + i * 1.3) * math.cos(tm * 2.1 + i) for i in range(n)]
            else:
                target = [0.0] * n
            for i in range(n):
                cur, tg = bars[i], float(target[i])
                speed = 28.0 if tg > cur else 7.0
                bars[i] = cur + (tg - cur) * (1 - math.exp(-dt * speed))

    def _update_mask(self) -> None:
        r = self._pill_rect().adjusted(-2, -2, 2, 2).toAlignedRect()
        if r != self._mask_rect:
            self._mask_rect = r
            self.setMask(QRegion(r))

    def _keep_on_top(self) -> None:
        """Windows иногда «роняет» topmost после полноэкранных приложений."""
        self.reposition()
        if sys.platform == "win32" and self.isVisible():
            try:
                import ctypes
                flags = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
                ctypes.windll.user32.SetWindowPos(int(self.winId()), -1, 0, 0, 0, 0, flags)
            except Exception:
                pass

    # ------------------------------------------------------------ мышь
    def enterEvent(self, e) -> None:
        self._mouse_inside = True
        self._hover_timer.start(160)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._mouse_inside = False
        self._hover_timer.start(320)
        self.unsetCursor()
        super().leaveEvent(e)

    def _apply_hover(self) -> None:
        # Задержка, чтобы остров не дёргался при случайном пролёте курсора
        inside = self._pill_rect().contains(self.mapFromGlobal(QCursor.pos()).toPointF())
        self._hovered = self._mouse_inside and inside

    def _button_at(self, pos: QPointF) -> Optional[str]:
        if self.expand.value < 0.8:
            return None
        for name, r in self._buttons.items():
            if r.contains(pos):
                return name
        return None

    def mouseMoveEvent(self, e) -> None:
        if self._button_at(e.position()):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.RightButton:
            if self.menu is not None:
                self.menu.popup(e.globalPosition().toPoint())
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        btn = self._button_at(e.position())
        if btn == "prev":
            self.prev_clicked.emit()
        elif btn == "play":
            self.play_pause_clicked.emit()
        elif btn == "next":
            self.next_clicked.emit()
        elif self._pill_rect().contains(e.position()):
            self._pinned = not self._pinned   # клик закрепляет/открепляет развёрнутый вид

    # ------------------------------------------------------------ отрисовка
    def _font(self, px: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
        f = QFont()
        fams = ([self.cfg.font_family] if self.cfg.font_family else []) + FONT_FALLBACKS
        f.setFamilies(fams)
        f.setPixelSize(max(6, int(round(px * self.cfg.scale))))
        f.setWeight(weight)
        f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        return f

    def _title_weight(self) -> QFont.Weight:
        return QFont.Weight.DemiBold if self.cfg.bold_title else QFont.Weight.Normal

    def paintEvent(self, _event) -> None:
        t = self._shown_track()
        if t is None and self.appear.value < 0.02:
            return
        c = self.cfg
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing
                         | QPainter.RenderHint.TextAntialiasing
                         | QPainter.RenderHint.SmoothPixmapTransform)
        rect = self._pill_rect()
        e = _clamp(self.expand.value)
        alpha = _clamp(self.appear.value * 1.6) * c.opacity
        s = c.scale

        compact_r = c.compact_height / 2
        radius = min(rect.height() / 2, _lerp(compact_r, c.radius, e) * s)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        p.setOpacity(alpha)
        bg = _color(c.bg_color, "#000000")
        # Полностью прозрачный фон Windows пропускает клики насквозь —
        # оставляем минимальную альфу, чтобы наведение работало.
        bg.setAlphaF(max(c.bg_opacity, 0.004))
        p.fillPath(path, bg)
        if c.border:
            p.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
            p.drawPath(path)

        content = _clamp((self.appear.value - 0.45) / 0.5)
        if t is not None and content > 0:
            p.setClipPath(path)
            ca = content * _clamp(1 - e * 2.2)
            ea = content * _clamp((e - 0.45) / 0.5)
            if ca > 0.01:
                p.setOpacity(alpha * ca)
                self._draw_compact(p, rect, t)
            if ea > 0.01:
                p.setOpacity(alpha * ea)
                self._draw_expanded(p, rect, t)
            else:
                self._buttons = {}
        p.end()

    def _text_color(self, a: int = 255) -> QColor:
        col = _color(self.cfg.text_color, "#ffffff")
        col.setAlpha(a)
        return col

    def _draw_cover(self, p: QPainter, r: QRectF, radius: float) -> None:
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        p.save()
        p.setClipPath(path, Qt.ClipOperation.IntersectClip)
        if self._cover is not None and self.track is not None:
            p.drawPixmap(r, self._cover, QRectF(self._cover.rect()))
        else:
            p.fillRect(r, QColor(60, 60, 66))
            p.setPen(self._text_color(150))
            p.setFont(self._font(r.height() / self.cfg.scale * 0.55))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "♪")
        p.restore()

    def _draw_bars(self, p: QPainter, area: QRectF, levels: list[float], bar_w: float,
                   playing: bool) -> None:
        n = len(levels)
        if n == 0:
            return
        gap = (area.width() - n * bar_w) / max(1, n - 1)
        color = QColor(self._accent_shown) if playing else self._text_color(90)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        cy = area.center().y()
        for i, lv in enumerate(levels):
            h = max(bar_w, _clamp(lv) * area.height())
            x = area.left() + i * (bar_w + gap)
            p.drawRoundedRect(QRectF(x, cy - h / 2, bar_w, h), bar_w / 2, bar_w / 2)

    def _draw_compact(self, p: QPainter, rect: QRectF, t: TrackInfo) -> None:
        c, s = self.cfg, self.cfg.scale
        h = rect.height()
        inset = h * 0.17
        x0 = rect.left() + h * 0.42
        if c.show_cover:
            cover = QRectF(rect.left() + inset, rect.top() + inset, h - 2 * inset, h - 2 * inset)
            self._draw_cover(p, cover, cover.height() * 0.3)
            x0 = cover.right() + 9 * s

        x1 = rect.right() - h * 0.42
        n = len(self._bars_compact)
        if n:
            bar_w = max(2.0, h * 0.083)
            gap = bar_w * 0.85
            bars_w = n * bar_w + (n - 1) * gap
            bars = QRectF(rect.right() - h * 0.42 - bars_w, rect.top() + h * 0.27, bars_w, h * 0.46)
            self._draw_bars(p, bars, self._bars_compact, bar_w, t.playing)
            x1 = bars.left() - 10 * s

        avail = x1 - x0
        if avail <= 10:
            return
        f_title = self._font(c.font_size, self._title_weight())
        f_artist = self._font(c.font_size)
        fm_t, fm_a = QFontMetricsF(f_title), QFontMetricsF(f_artist)
        base = rect.center().y() + (fm_t.ascent() - fm_t.descent()) / 2

        title = t.title or t.artist or "—"
        tw = fm_t.horizontalAdvance(title)
        p.setFont(f_title)
        p.setPen(self._text_color())
        if tw >= avail:
            p.drawText(QPointF(x0, base), fm_t.elidedText(title, Qt.TextElideMode.ElideRight, avail))
            return
        p.drawText(QPointF(x0, base), title)
        rest = avail - tw - 6 * s
        if c.show_artist and t.title and t.artist and rest > 24 * s:
            p.setFont(f_artist)
            p.setPen(self._text_color(120))
            p.drawText(QPointF(x0 + tw + 6 * s, base),
                       fm_a.elidedText(t.artist, Qt.TextElideMode.ElideRight, rest))

    def _draw_expanded(self, p: QPainter, rect: QRectF, t: TrackInfo) -> None:
        c, s = self.cfg, self.cfg.scale
        pad = 16 * s
        top = rect.top()
        # Раскладка привязана к финальному размеру, чтобы ничего не «плыло»
        W = c.expanded_width * s
        if c.corner.endswith("right"):
            left = rect.right() - W
        elif c.corner.endswith("left"):
            left = rect.left()
        else:
            left = rect.center().x() - W / 2
        right = left + W
        fs = c.font_size

        tx = left + pad
        cover = QRectF(left + pad, top + pad, 64 * s, 64 * s)
        if c.show_cover:
            self._draw_cover(p, cover, 14 * s)
            tx = cover.right() + 14 * s

        bar_w, n = 3.6 * s, len(self._bars_expanded)
        bars_w = n * bar_w + (n - 1) * 3 * s
        bars = QRectF(right - pad - 4 * s - bars_w, cover.center().y() - 13 * s, bars_w, 26 * s)
        if c.bars:
            self._draw_bars(p, bars, self._bars_expanded, bar_w, t.playing)
        tw = (bars.left() if c.bars else right - pad) - 14 * s - tx

        f_title = self._font(fs * 1.24, self._title_weight())
        f_artist = self._font(fs * 1.08)
        fm_t, fm_a = QFontMetricsF(f_title), QFontMetricsF(f_artist)
        mid = cover.center().y()
        p.setFont(f_title)
        p.setPen(self._text_color())
        p.drawText(QPointF(tx, mid - 4 * s),
                   fm_t.elidedText(t.title or "—", Qt.TextElideMode.ElideRight, tw))
        p.setFont(f_artist)
        p.setPen(self._text_color(140))
        p.drawText(QPointF(tx, mid + fm_a.ascent() + 2 * s),
                   fm_a.elidedText(t.artist or t.album or "", Qt.TextElideMode.ElideRight, tw))

        # Прогресс
        if t.duration > 0:
            pos = t.current_position()
            f_time = self._font(fs * 0.88, QFont.Weight.Medium)
            fm = QFontMetricsF(f_time)
            y = cover.bottom() + 20 * s
            lbl_w = max(42 * s, fm.horizontalAdvance("-00:00") + 10 * s)
            bar = QRectF(left + pad + lbl_w, y - 2.5 * s, W - 2 * pad - 2 * lbl_w, 5 * s)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._text_color(50))
            p.drawRoundedRect(bar, bar.height() / 2, bar.height() / 2)
            done = QRectF(bar.left(), bar.top(), max(bar.height(), bar.width() * pos / t.duration), bar.height())
            p.setBrush(self._text_color(235))
            p.drawRoundedRect(done, bar.height() / 2, bar.height() / 2)
            p.setFont(f_time)
            p.setPen(self._text_color(140))
            base = y + (fm.ascent() - fm.descent()) / 2
            p.drawText(QPointF(left + pad, base), _fmt(pos))
            rem = "-" + _fmt(t.duration - pos)
            p.drawText(QPointF(right - pad - fm.horizontalAdvance(rem), base), rem)

        # Кнопки
        cy = rect.top() + EXPANDED_H * s - 24 * s
        cx = left + W / 2
        step = 66 * s
        self._buttons = {
            "prev": QRectF(cx - step - 22 * s, cy - 17 * s, 44 * s, 34 * s),
            "play": QRectF(cx - 24 * s, cy - 19 * s, 48 * s, 38 * s),
            "next": QRectF(cx + step - 22 * s, cy - 17 * s, 44 * s, 34 * s),
        }
        hover = None
        if self.underMouse():
            hover = self._button_at(self.mapFromGlobal(QCursor.pos()).toPointF())
        p.setPen(Qt.PenStyle.NoPen)
        for name, r in self._buttons.items():
            p.setBrush(self._text_color(255 if hover == name else 215))
            if name == "play":
                _icon_play_pause(p, r.center(), 11 * s, bool(t.playing))
            else:
                _icon_skip(p, r.center(), 8.5 * s, forward=(name == "next"))


def _icon_play_pause(p: QPainter, c: QPointF, size: float, playing: bool) -> None:
    if playing:
        w, h = size * 0.62, size * 2
        gap = size * 0.55
        for dx in (-gap / 2 - w, gap / 2):
            p.drawRoundedRect(QRectF(c.x() + dx, c.y() - h / 2, w, h), w * 0.3, w * 0.3)
    else:
        path = QPainterPath()
        path.moveTo(c.x() - size * 0.7, c.y() - size)
        path.lineTo(c.x() + size * 1.05, c.y())
        path.lineTo(c.x() - size * 0.7, c.y() + size)
        path.closeSubpath()
        p.drawPath(path)


def _icon_skip(p: QPainter, c: QPointF, size: float, forward: bool) -> None:
    d = 1 if forward else -1
    path = QPainterPath()
    for off in (-size * 0.95, size * 0.05):
        x = c.x() + off * d
        path.moveTo(x, c.y() - size * 0.8)
        path.lineTo(x + size * 0.95 * d, c.y())
        path.lineTo(x, c.y() + size * 0.8)
        path.closeSubpath()
    p.drawPath(path)


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor.fromRgbF(
        _lerp(a.redF(), b.redF(), t),
        _lerp(a.greenF(), b.greenF(), t),
        _lerp(a.blueF(), b.blueF(), t),
        _lerp(a.alphaF(), b.alphaF(), t),
    )
