"""Источник «что сейчас играет».

На Windows берём данные из System Media Transport Controls (SMTC) — того же
механизма, который показывает плеер во всплывающем окне громкости Windows.
Яндекс Музыка (приложение и сайт в браузере), Spotify, браузеры и т.д.
публикуют туда название, исполнителя, обложку и позицию трека.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QBuffer, QIODevice, QObject, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter

log = logging.getLogger(__name__)


@dataclass
class TrackInfo:
    app_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    playing: bool = False
    position: float = 0.0     # позиция (сек) на момент stamp
    duration: float = 0.0     # длительность (сек), 0 = неизвестна
    stamp: float = 0.0        # time.monotonic(), когда position была актуальна
    cover: Optional[bytes] = None
    cover_key: str = ""

    @property
    def key(self) -> str:
        return f"{self.app_id}\x00{self.title}\x00{self.artist}\x00{self.album}"

    def current_position(self) -> float:
        if self.duration <= 0:
            return 0.0
        p = self.position
        if self.playing:
            p += time.monotonic() - self.stamp
        return max(0.0, min(p, self.duration))


def _seconds(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    try:
        return float(value) / 10_000_000  # TimeSpan в тиках по 100 нс
    except Exception:
        return 0.0


class MediaWatcher(QObject):
    """Опрашивает SMTC в отдельном потоке и шлёт TrackInfo (или None) в UI."""

    changed = Signal(object)

    POLL_INTERVAL = 0.5

    def __init__(self, preferred_apps: list[str], only_preferred: bool = False):
        super().__init__()
        self.preferred_apps = [p.lower() for p in preferred_apps if p]
        self.only_preferred = only_preferred
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._session = None
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="smtc", daemon=True)

    # ---------- публичное API ----------
    def start(self) -> None:
        if sys.platform != "win32":
            log.error("SMTC доступен только на Windows. Запустите с флагом --demo.")
            return
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def play_pause(self) -> None:
        self._command("try_toggle_play_pause_async")

    def next(self) -> None:
        self._command("try_skip_next_async")

    def previous(self) -> None:
        self._command("try_skip_previous_async")

    # ---------- внутренности ----------
    def _command(self, method: str) -> None:
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._do_command(method), self._loop)

    async def _do_command(self, method: str) -> None:
        s = self._session
        if s is None:
            return
        try:
            await getattr(s, method)()
        except Exception:
            log.exception("Команда %s не выполнена", method)
        if self._wake:
            self._wake.set()

    def _run(self) -> None:
        try:
            from winrt.runtime import ApartmentType, init_apartment
            init_apartment(ApartmentType.MULTI_THREADED)
        except Exception:
            pass  # поток уже инициализирован — это нормально
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception:
            log.exception("Поток SMTC упал")

    async def _main(self) -> None:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )

        self._wake = asyncio.Event()
        manager = None
        covers: dict[str, bytes] = {}
        cover_attempts: dict[str, int] = {}

        while not self._stop:
            info: Optional[TrackInfo] = None
            try:
                if manager is None:
                    manager = await Manager.request_async()
                session = self._pick_session(manager)
                self._session = session
                if session is not None:
                    info = await self._read_session(session)
                    if info is not None and (info.title or info.artist):
                        key = info.key
                        if key in covers:
                            info.cover = covers[key]
                        elif cover_attempts.get(key, 0) < 6:
                            # Приложения иногда выставляют обложку чуть позже названия
                            cover_attempts[key] = cover_attempts.get(key, 0) + 1
                            data = await self._read_cover(session)
                            if data:
                                covers[key] = data
                                info.cover = data
                        info.cover_key = key if info.cover else ""
                        if len(covers) > 20:
                            covers.pop(next(iter(covers)))
                        if len(cover_attempts) > 200:
                            cover_attempts.clear()
                    elif info is not None:
                        info = None
            except Exception:
                log.exception("Ошибка чтения SMTC")
                manager = None
                self._session = None
                info = None

            self.changed.emit(info)

            try:
                await asyncio.wait_for(self._wake.wait(), self.POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def _is_preferred(self, app_id: str) -> bool:
        a = (app_id or "").lower()
        return any(p in a for p in self.preferred_apps)

    def _pick_session(self, manager):
        try:
            sessions = list(manager.get_sessions())
        except Exception:
            sessions = []
        if not sessions:
            return None

        current = manager.get_current_session()
        current_id = current.source_app_user_model_id if current is not None else None

        def playing(s) -> bool:
            try:
                return int(s.get_playback_info().playback_status) == 4  # PLAYING
            except Exception:
                return False

        if self.only_preferred:
            sessions = [s for s in sessions if self._is_preferred(s.source_app_user_model_id)]
            if not sessions:
                return None

        # Сначала играющие, среди них — Яндекс Музыка, потом текущая сессия Windows
        sessions.sort(key=lambda s: (
            not playing(s),
            not self._is_preferred(s.source_app_user_model_id),
            s.source_app_user_model_id != current_id,
        ))
        return sessions[0]

    async def _read_session(self, session) -> Optional[TrackInfo]:
        props = await session.try_get_media_properties_async()
        if props is None:
            return None
        pb = session.get_playback_info()
        tl = session.get_timeline_properties()

        playing = int(pb.playback_status) == 4
        start = _seconds(tl.start_time)
        end = _seconds(tl.end_time)
        pos = _seconds(tl.position) - start
        duration = max(0.0, end - start)

        # Многие плееры обновляют позицию редко — досчитываем по last_updated_time
        if playing and duration > 0:
            try:
                last = tl.last_updated_time
                if isinstance(last, dt.datetime) and last.year > 2000:
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=dt.timezone.utc)
                    age = (dt.datetime.now(dt.timezone.utc) - last).total_seconds()
                    if 0 < age < duration:
                        pos += age
            except Exception:
                pass

        return TrackInfo(
            app_id=session.source_app_user_model_id or "",
            title=props.title or "",
            artist=props.artist or props.album_artist or "",
            album=props.album_title or "",
            playing=playing,
            position=max(0.0, pos),
            duration=duration,
            stamp=time.monotonic(),
        )

    async def _read_cover(self, session) -> Optional[bytes]:
        try:
            props = await session.try_get_media_properties_async()
            ref = props.thumbnail if props is not None else None
            if ref is None:
                return None
            from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions

            stream = await ref.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > 16 * 1024 * 1024:
                return None
            buf = Buffer(size)
            result = await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
            try:
                return bytes(memoryview(result))[: result.length]
            except TypeError:
                reader = DataReader.from_buffer(result)
                out = bytearray(result.length)
                reader.read_bytes(out)
                return bytes(out)
        except Exception:
            log.debug("Не удалось прочитать обложку", exc_info=True)
            return None


class DemoMedia(QObject):
    """Фейковый плеер для проверки внешнего вида: python -m island --demo"""

    changed = Signal(object)

    TRACKS = [
        ("Ночной город", "Синтетика", 214, ("#ff4d6d", "#6a00f4")),
        ("Очень длинное название трека, которое не влезет в остров", "Кто-то & Кто-то ещё", 187, ("#ffb703", "#fb5607")),
        ("Lo-fi beats to relax", "Chill Room", 162, ("#2ec4b6", "#3a86ff")),
    ]

    def __init__(self, *_, **__):
        super().__init__()
        self._i = 0
        self._playing = True
        self._pos = 23.0
        self._stamp = time.monotonic()
        self._covers = {}
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._emit)

    def start(self) -> None:
        self._timer.start(500)
        self._emit()

    def stop(self) -> None:
        self._timer.stop()

    def _cover(self, colors) -> bytes:
        if colors in self._covers:
            return self._covers[colors]
        img = QImage(300, 300, QImage.Format.Format_ARGB32)
        p = QPainter(img)
        g = QLinearGradient(0, 0, 300, 300)
        g.setColorAt(0, QColor(colors[0]))
        g.setColorAt(1, QColor(colors[1]))
        p.fillRect(img.rect(), g)
        p.setPen(QColor(255, 255, 255, 60))
        for r in range(40, 300, 40):
            p.drawEllipse(150 - r, 150 - r, 2 * r, 2 * r)
        p.end()
        b = QBuffer()
        b.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(b, "PNG")
        data = bytes(b.data())
        self._covers[colors] = data
        return data

    def _current_pos(self) -> float:
        p = self._pos + (time.monotonic() - self._stamp if self._playing else 0)
        return p

    def _emit(self) -> None:
        title, artist, dur, colors = self.TRACKS[self._i]
        if self._current_pos() >= dur:
            self.next()
            return
        info = TrackInfo(
            app_id="Demo.YandexMusic", title=title, artist=artist, playing=self._playing,
            position=self._current_pos(), duration=dur, stamp=time.monotonic(),
            cover=self._cover(colors),
        )
        info.cover_key = info.key
        self.changed.emit(info)

    def play_pause(self) -> None:
        self._pos = self._current_pos()
        self._stamp = time.monotonic()
        self._playing = not self._playing
        self._emit()

    def next(self) -> None:
        self._i = (self._i + 1) % len(self.TRACKS)
        self._pos, self._stamp = 0.0, time.monotonic()
        self._emit()

    def previous(self) -> None:
        if self._current_pos() > 3:
            self._pos = 0.0
        else:
            self._i = (self._i - 1) % len(self.TRACKS)
            self._pos = 0.0
        self._stamp = time.monotonic()
        self._emit()

