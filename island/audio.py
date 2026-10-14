"""Визуализатор: захват системного звука (WASAPI loopback) и спектр по полосам.

Loopback-захват слышит весь звук, который идёт на колонки/наушники по умолчанию,
поэтому бары двигаются в такт реальной музыке. Если захват недоступен,
используется «синтетическая» анимация.
"""
from __future__ import annotations

import logging
import math
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

FFT_SIZE = 2048
NUM_BANDS = 24
F_MIN, F_MAX = 45.0, 14000.0


def make_band_edges(rate: int, n_fft: int = FFT_SIZE, bands: int = NUM_BANDS) -> np.ndarray:
    """Логарифмические границы полос в индексах бинов FFT (каждая полоса ≥ 1 бина)."""
    freqs = np.geomspace(F_MIN, min(F_MAX, rate / 2 - 1), bands + 1)
    idx = np.round(freqs * n_fft / rate).astype(int)
    idx[0] = max(idx[0], 1)
    for i in range(1, len(idx)):
        if idx[i] <= idx[i - 1]:
            idx[i] = idx[i - 1] + 1
    return np.minimum(idx, n_fft // 2)


def spectrum_levels(samples: np.ndarray, edges: np.ndarray, window: np.ndarray,
                    sensitivity: float = 1.0) -> np.ndarray:
    """Моно-сэмплы (float32, -1..1) -> уровни полос 0..1."""
    spec = np.abs(np.fft.rfft(samples * window)) / (len(samples) / 4)
    out = np.empty(len(edges) - 1, dtype=np.float32)
    for i in range(len(out)):
        lo, hi = edges[i], max(edges[i + 1], edges[i] + 1)
        out[i] = spec[lo:hi].max()
    # Небольшой подъём верхов, иначе бас «перетягивает» картинку
    tilt = np.linspace(1.0, 2.2, len(out), dtype=np.float32)
    db = 20.0 * np.log10(out * tilt * max(sensitivity, 0.05) + 1e-9)
    return np.clip((db + 52.0) / 46.0, 0.0, 1.0)


class AudioVisualizer:
    def __init__(self, sensitivity: float = 1.0):
        self.sensitivity = sensitivity
        self.expect_audio = False          # UI сообщает, что сейчас что-то играет
        self.available = False
        self._levels = np.zeros(NUM_BANDS, dtype=np.float32)
        self._lock = threading.Lock()
        self._last_data = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self._ring = np.zeros(FFT_SIZE, dtype=np.float32)
        self._window = np.hanning(FFT_SIZE).astype(np.float32)
        self._edges = make_band_edges(48000)
        self._channels = 2

    # ---------- публичное API ----------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="loopback", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def get_levels(self, n: int) -> np.ndarray:
        """Уровни для n баров (0..1)."""
        now = time.monotonic()
        if self.available:
            with self._lock:
                lv = self._levels.copy()
            if now - self._last_data > 0.25:   # тишина: WASAPI не присылает пакеты
                lv[:] = 0.0
        else:
            lv = self._fake(now) if self.expect_audio else np.zeros(NUM_BANDS, np.float32)
        return _regroup(lv, n)

    # ---------- захват ----------
    def _fake(self, t: float) -> np.ndarray:
        i = np.arange(NUM_BANDS, dtype=np.float32)
        beat = 0.55 + 0.45 * max(0.0, math.sin(t * 2 * math.pi * 2.0)) ** 4
        v = 0.35 + 0.3 * np.sin(t * (3.1 + i * 0.37) + i * 1.7) * np.cos(t * 1.3 + i)
        return np.clip(v * beat + 0.15, 0.05, 1.0).astype(np.float32)

    def _callback(self, in_data, frame_count, time_info, status):
        import pyaudiowpatch as pyaudio
        try:
            x = np.frombuffer(in_data, dtype=np.float32)
            if self._channels > 1:
                x = x[: len(x) // self._channels * self._channels]
                x = x.reshape(-1, self._channels).mean(axis=1)
            n = len(x)
            if n >= FFT_SIZE:
                self._ring[:] = x[-FFT_SIZE:]
            elif n:
                self._ring[:-n] = self._ring[n:]
                self._ring[-n:] = x
            lv = spectrum_levels(self._ring, self._edges, self._window, self.sensitivity)
            with self._lock:
                self._levels = lv
            self._last_data = time.monotonic()
        except Exception:
            log.debug("Ошибка обработки звука", exc_info=True)
        return (None, pyaudio.paContinue)

    def _run(self) -> None:
        try:
            import pyaudiowpatch as pyaudio
        except Exception:
            log.warning("PyAudioWPatch не установлен — визуализатор будет синтетическим")
            return

        while self._running:
            p = stream = None
            try:
                p = pyaudio.PyAudio()
                dev = p.get_default_wasapi_loopback()
                rate = int(dev["defaultSampleRate"])
                self._channels = max(1, int(dev["maxInputChannels"]))
                self._edges = make_band_edges(rate)
                stream = p.open(
                    format=pyaudio.paFloat32,
                    channels=self._channels,
                    rate=rate,
                    input=True,
                    input_device_index=dev["index"],
                    frames_per_buffer=512,
                    stream_callback=self._callback,
                )
                log.info("Захват звука: %s (%d Гц, %d кан.)", dev.get("name"), rate, self._channels)
                self.available = True
                opened = time.monotonic()
                # Если что-то играет, а данных нет 4 сек — скорее всего сменилось
                # устройство вывода (наушники/колонки). Переоткрываем захват.
                while self._running:
                    time.sleep(0.5)
                    now = time.monotonic()
                    silent_for = now - max(self._last_data, opened)
                    if self.expect_audio and silent_for > 4.0:
                        log.info("Нет звука при воспроизведении — переоткрываю устройство")
                        break
            except Exception:
                log.warning("Не удалось открыть loopback-устройство", exc_info=True)
                self.available = False
                time.sleep(3.0)
            finally:
                try:
                    if stream is not None:
                        stream.stop_stream()
                        stream.close()
                except Exception:
                    pass
                try:
                    if p is not None:
                        p.terminate()
                except Exception:
                    pass
            time.sleep(0.3)


def _regroup(levels: np.ndarray, n: int) -> np.ndarray:
    """Сжать NUM_BANDS полос в n баров (берём максимум внутри группы)."""
    if n >= len(levels):
        return levels
    groups = np.array_split(levels, n)
    return np.array([g.max() for g in groups], dtype=np.float32)
