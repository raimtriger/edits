"""Голос-гайд, записанный БЕЗ наушников: в записи кроме голоса слышна музыка из динамика.
Мы знаем сам трек, поэтому вычитаем его из записи (спектральное вычитание по эталону) и распознаём очищенный голос.
Лёгкое: numpy + ffmpeg, около секунд на трек."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import numpy as np

SR = 16000
N, HOP = 1024, 256


def _load(path: str, limit: float = 420.0) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-t", f"{limit}", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                       capture_output=True, timeout=300)
    return np.frombuffer(r.stdout, np.float32).copy()


def _stft(x: np.ndarray) -> np.ndarray:
    w = np.hanning(N).astype(np.float32)
    if len(x) < N:
        x = np.pad(x, (0, N - len(x)))
    n = 1 + (len(x) - N) // HOP
    idx = np.arange(N)[None, :] + HOP * np.arange(n)[:, None]
    return np.fft.rfft(x[idx] * w, axis=1)


def _istft(S: np.ndarray, length: int) -> np.ndarray:
    w = np.hanning(N).astype(np.float32)
    out = np.zeros(HOP * (len(S) - 1) + N, np.float32)
    norm = np.zeros_like(out)
    fr = np.fft.irfft(S, n=N, axis=1) * w
    for i in range(len(S)):
        out[i * HOP:i * HOP + N] += fr[i]
        norm[i * HOP:i * HOP + N] += w * w
    out /= np.maximum(norm, 1e-6)
    return out[:length]


def find_delay(g: np.ndarray, m: np.ndarray, max_s: float = 2.0) -> int:
    """Сдвиг (в отсчётах) трека относительно записи: кросс-корреляция огибающих (быстро и устойчиво к комнате)."""
    hop = 160                                              # 10 мс
    def env(x):
        k = len(x) // hop
        return np.abs(x[:k * hop]).reshape(k, hop).mean(1)
    eg, em = env(g[:SR * 90]), env(m[:SR * 90])
    eg, em = eg - eg.mean(), em - em.mean()
    n = 1 << int(np.ceil(np.log2(len(eg) + len(em))))
    c = np.fft.irfft(np.fft.rfft(eg, n) * np.conj(np.fft.rfft(em, n)), n)
    mx = int(max_s * 100)
    lags = np.concatenate([np.arange(0, mx + 1), np.arange(-mx, 0)])
    cc = np.concatenate([c[:mx + 1], c[-mx:]])
    return int(lags[int(np.argmax(cc))]) * hop


def suppress_music(guide: str, track: str, out_wav: str, alpha: float = 1.2) -> str:
    g, m = _load(guide), _load(track)
    d = find_delay(g, m)
    m = np.roll(m, d) if d >= 0 else np.roll(m, d)
    if d > 0:
        m[:d] = 0
    L = min(len(g), len(m))
    g, m = g[:L], m[:L]
    G, M = _stft(g), _stft(m)
    Ga, Ma = np.abs(G), np.abs(M)
    # усиление утечки по каждой частоте: сколько музыки попало в запись (МНК по всем кадрам; голос с музыкой не коррелирует)
    gain = (Ga * Ma).sum(0) / np.maximum((Ma * Ma).sum(0), 1e-9)
    gain = np.convolve(gain, np.ones(5) / 5, mode="same")
    V = np.maximum(Ga - alpha * gain[None, :] * Ma, 0.12 * Ga)
    clean = _istft(V * np.exp(1j * np.angle(G)), L)
    clean /= max(1e-6, np.abs(clean).max()) / 0.9
    import wave
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((np.clip(clean, -1, 1) * 32767).astype(np.int16).tobytes())
    return out_wav
