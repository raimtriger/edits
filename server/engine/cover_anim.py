"""Анимация обложки для рилса 1080x1920 (по мотивам anim_v4, RAIM TRIGER — «30»).

Фон — та же обложка, размытая и тёмная, с параллаксом, виньеткой, зерном, пылинками и боке.
Интро: обложка на весь экран, размыта и повёрнута в 3D -> одним движением становится карточкой.
Весь ролик: мягкое 3D-«дыхание», пульс под бит, два мягких расфокуса, текст по буквам, финал в чёрное.
Зависимости: numpy, opencv-python-headless, Pillow, ffmpeg.
"""
from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30
TAU = 2 * math.pi
NICE = int(os.environ.get("EDITS_NICE", "0"))


# ---------------- бит без librosa ----------------
def detect_beats(audio: str, start: float, dur: float) -> np.ndarray:
    sr, hop, nfft = 11025, 256, 1024
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur + 0.5:.3f}", "-i", audio,
                          "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True).stdout
    y = np.frombuffer(raw, np.float32)
    if len(y) < nfft * 4:
        return np.arange(0.9, dur, 0.6)
    frames = np.lib.stride_tricks.sliding_window_view(y, nfft)[::hop] * np.hanning(nfft)
    spec = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(nfft, 1 / sr)
    low = np.log1p(spec[:, freqs < 200]).sum(1)
    flux = np.maximum(0, np.diff(np.log1p(spec), axis=0)).sum(1)
    onset = np.concatenate([[0], flux]) + 0.5 * np.concatenate([[0], np.maximum(0, np.diff(low))])
    onset = (onset - onset.mean()) / (onset.std() + 1e-6)
    fps_o = sr / hop
    # темп через автокорреляцию, 70–180 BPM
    ac = np.correlate(onset, onset, "full")[len(onset) - 1:]
    lags = np.arange(len(ac))
    lo, hi = int(fps_o * 60 / 180), int(fps_o * 60 / 70)
    weights = np.exp(-0.5 * (np.log2((60 * fps_o / np.maximum(lags[lo:hi], 1)) / 110)) ** 2)  # мягкий приоритет ~110 BPM
    period = lo + int(np.argmax(ac[lo:hi] * weights))
    # фаза: сетка с максимальной суммой онсетов
    best = max(range(period), key=lambda ph: onset[ph::period].sum())
    beats = np.arange(best, len(onset), period) / fps_o
    return beats[beats < dur] if len(beats) > 3 else np.arange(0.9, dur, 0.6)


# ---------------- утилиты ----------------
def ease_out(x): x = min(max(x, 0), 1); return 1 - (1 - x) ** 3
def ease_io(x): x = min(max(x, 0), 1); return 3 * x * x - 2 * x * x * x
def ease_in(x): x = min(max(x, 0), 1); return x ** 3
def lerp(a, b, k): return a + (b - a) * k
def bump(t, c, w): return math.exp(-((t - c) / w) ** 2)


def render_cover_anim(cover: str, audio: str, out: str, start: float, dur: float,
                      title: str = "ПРЕМЬЕРА", font_path: str | None = None, title_size: int = 92,
                      seed: int = 7) -> str:
    dur = float(max(8.0, min(30.0, dur)))
    N = int(FPS * dur)
    rng = np.random.default_rng(seed)

    BEATS = detect_beats(audio, start, dur)
    BSTR = np.ones(len(BEATS)) * 0.8

    def pulse(t, tau=0.32, st=0.9):
        v = 0.0
        for b, a in zip(BEATS, BSTR):
            if st <= b <= t:
                v += a * (1 - math.exp(-(t - b) / 0.06)) * math.exp(-(t - b) / tau)
        return min(v, 1.3)

    def near(x):
        return float(BEATS[np.argmin(abs(BEATS - x))]) if len(BEATS) else x

    # ---------- исходники ----------
    img = cv2.imread(cover)
    if img is None:
        raise ValueError("не удалось прочитать обложку — нужна картинка JPG, PNG или WebP")
    ih, iw = img.shape[:2]
    if ih != iw:                       # не квадратная — берём центральный квадрат, а не растягиваем
        side = min(ih, iw)
        y0, x0 = (ih - side) // 2, (iw - side) // 2
        img = img[y0:y0 + side, x0:x0 + side]
    cov = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    S = 1200
    card = cv2.resize(cov, (S, S), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    m = Image.new("L", (S * 4, S * 4), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, S * 4 - 1, S * 4 - 1], radius=34 * 4, fill=255)
    mask = np.asarray(m.resize((S, S), Image.LANCZOS), np.float32)[..., None] / 255
    e = Image.new("L", (S * 4, S * 4), 0)
    ImageDraw.Draw(e).rounded_rectangle([0, 0, S * 4 - 1, S * 4 - 1], radius=34 * 4, outline=255, width=9)
    edge = np.asarray(e.resize((S, S), Image.LANCZOS), np.float32)[..., None] / 255
    grad = np.linspace(1.0, 0.35, S, dtype=np.float32)[:, None, None]
    card = np.clip(card + edge * 0.28 * grad, 0, 1)
    card_rgba = np.concatenate([card * mask, mask], 2)
    del card, m, e, edge
    src_pts = np.float32([[0, 0], [S, 0], [S, S], [0, S]])

    BG = 1800
    bg = cv2.resize(cov, (BG, BG), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    bg = cv2.GaussianBlur(bg, (0, 0), 38) * 0.8
    del cov

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    r = np.sqrt(((xx - W / 2) / (W * 0.75)) ** 2 + ((yy - H / 2) / (H * 0.62)) ** 2)
    vign = np.clip(1 - 0.75 * r ** 2.2, 0.15, 1)[..., None]
    del yy, xx, r

    # ---------- текст ----------
    fpath = font_path or str(Path(__file__).resolve().parents[2] / "fonts" / "unbounded-black.ttf")

    def glyphs(text, size, track, color):
        f = ImageFont.truetype(fpath, size)
        pad, items, x = 30, [], 0
        for ch in text:
            adv = f.getlength(ch)
            if ch != " ":
                l, t_, rr, b = f.getbbox(ch)
                im = Image.new("RGBA", (int(rr - l) + pad * 2, int(size * 1.3) + pad * 2), (0, 0, 0, 0))
                ImageDraw.Draw(im).text((pad - l, pad), ch, font=f, fill=color)
                a = np.asarray(im, np.float32) / 255
                a[..., :3] *= a[..., 3:4]
                items.append([a, x + l - pad])
            x += adv + track
        return items, x - track

    title = (title or "").strip()[:24]
    size = title_size
    g1, w1 = glyphs(title, size, 6, (236, 236, 236, 255)) if title else ([], 0)
    while w1 > W * 0.86 and size > 40:          # длинный текст — уменьшаем, чтобы влез
        size -= 6
        g1, w1 = glyphs(title, size, 6, (236, 236, 236, 255))

    def blit(canvas, rgba, x, y, alpha):
        h, w = rgba.shape[:2]
        x, y = int(round(x)), int(round(y))
        x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
        if x1 <= x0 or y1 <= y0:
            return
        s = rgba[y0 - y:y1 - y, x0 - x:x1 - x] * alpha
        c = canvas[y0:y1, x0:x1]
        c[:] = c * (1 - s[..., 3:4]) + s[..., :3]

    def draw_text(canvas, items, total, cy, t0, stagger, d, t, out_k):
        x0 = W / 2 - total / 2
        for i, (a, gx) in enumerate(items):
            k = ease_out((t - t0 - i * stagger) / d)
            if k <= 0:
                continue
            sig = (1 - k) * 10 + out_k * 12
            img = cv2.GaussianBlur(a, (0, 0), sig) if sig > 0.4 else a
            blit(canvas, img, x0 + gx, cy + (1 - k) * 28 - out_k * 20, k * (1 - out_k))

    # ---------- частицы ----------
    NP, BOK = 70, 7
    P = dict(x=rng.uniform(0, W, NP), y=rng.uniform(0, H, NP), v=rng.uniform(8, 40, NP),
             s=rng.uniform(1.0, 3.2, NP), ph=rng.uniform(0, 6.28, NP), a=rng.uniform(0.25, 0.8, NP),
             dx=rng.uniform(-10, 10, NP))
    B = dict(x=rng.uniform(0, W, BOK), y=rng.uniform(0, H, BOK), r=rng.uniform(25, 60, BOK),
             v=rng.uniform(5, 15, BOK), ph=rng.uniform(0, 6.28, BOK))

    def particles(t):
        L = np.zeros((H // 2, W // 2), np.float32)
        for i in range(NP):
            x = (P["x"][i] + P["dx"][i] * t + 12 * math.sin(t * 0.8 + P["ph"][i])) % W
            y = (P["y"][i] - P["v"][i] * t) % H
            a = P["a"][i] * (0.5 + 0.5 * math.sin(t * 1.7 + P["ph"][i]))
            cv2.circle(L, (int(x / 2), int(y / 2)), max(1, int(P["s"][i] / 2)), a, -1, cv2.LINE_AA)
        L = cv2.GaussianBlur(L, (0, 0), 0.8)
        Bk = np.zeros((H // 4, W // 4), np.float32)
        for i in range(BOK):
            x = (B["x"][i] + 6 * t) % W
            y = (B["y"][i] - B["v"][i] * t) % H
            a = 0.10 * (0.6 + 0.4 * math.sin(t + B["ph"][i]))
            cv2.circle(Bk, (int(x / 4), int(y / 4)), int(B["r"][i] / 4), a, -1, cv2.LINE_AA)
        Bk = cv2.GaussianBlur(Bk, (0, 0), 3)
        L = cv2.resize(L, (W, H)) + cv2.resize(Bk, (W, H))
        return L[..., None] * np.float32([1.0, 0.95, 0.8])

    def project(cx, cy, size, rx, ry, rz, f=2000.0):
        h = size / 2
        pts = np.array([[-h, -h, 0], [h, -h, 0], [h, h, 0], [-h, h, 0]], np.float64)
        rx, ry, rz = map(math.radians, (rx, ry, rz))
        Rx = np.array([[1, 0, 0], [0, math.cos(rx), -math.sin(rx)], [0, math.sin(rx), math.cos(rx)]])
        Ry = np.array([[math.cos(ry), 0, math.sin(ry)], [0, 1, 0], [-math.sin(ry), 0, math.cos(ry)]])
        Rz = np.array([[math.cos(rz), -math.sin(rz), 0], [math.sin(rz), math.cos(rz), 0], [0, 0, 1]])
        p = pts @ (Rz @ Ry @ Rx).T
        z = f + p[:, 2]
        return np.float32(np.stack([cx + p[:, 0] * f / z, cy + p[:, 1] * f / z], 1))

    FOCUS = [near(dur * 0.43), near(dur * 0.70)]
    after = [b for b in BEATS if b < dur - 1.2]
    TO = max(after) if after else dur - 1.3      # начало финала — на бит
    TEXT_T0 = near(4.2)
    stagger = max(0.06, min(0.16, (np.median(np.diff(BEATS)) if len(BEATS) > 2 else 0.6) / 4))

    def state(t):
        u = min(max(t / 3.8, 0), 1)
        k = 1 - (1 - u) ** 2.2
        cx, cy, size = 540, lerp(990, 790, k), lerp(1750, 860, k)
        rx, ry, rz = lerp(16, 0, k), lerp(-24, 0, k), lerp(-7, 0, k)
        bl = 16 * (1 - ease_out(t / 1.3))
        kb = ease_io((t - 1.0) / 3.0)
        size *= 1 + 0.045 * kb * math.sin(TAU * t / 4.2)
        cx += 24 * kb * math.sin(TAU * t / 5.5 + 0.5)
        cy += 16 * kb * math.sin(TAU * t / 3.7)
        rx += 6.0 * kb * math.sin(TAU * t / 4.8 + 1.0)
        ry += 8.0 * kb * math.sin(TAU * t / 6.1)
        rz += 1.5 * kb * math.sin(TAU * t / 7.0 + 2.0)
        for c in FOCUS:
            bl += 5 * bump(t, c, 0.45)
        size *= 1 + 0.014 * pulse(t)
        if t > TO:
            k = ease_in((t - TO) / (dur - TO))
            size *= lerp(1, 2.3, k); rx += 22 * k; ry -= 18 * k; rz -= 14 * k; cy += 120 * k; bl += 20 * k
        return cx, cy, size, rx, ry, rz, bl

    def frame(t):
        z = 1.0 + 0.06 * t / dur + 0.008 * pulse(t, 0.45)
        if t > dur - 1.5:
            z += 0.25 * ease_in((t - (dur - 1.5)) / 1.5)
        ox = -55 * math.sin(TAU * t / 5.5 + 0.5) + 30 * math.sin(t * 0.25)
        oy = -35 * math.sin(TAU * t / 3.7) + 20 * math.cos(t * 0.2)
        sc = (H / BG) * 1.15 * z
        M = np.float32([[sc, 0, W / 2 - BG / 2 * sc + ox], [0, sc, H / 2 - BG / 2 * sc + oy]])
        canvas = cv2.warpAffine(bg, M, (W, H), borderMode=cv2.BORDER_REFLECT)
        pp = pulse(t)
        fl = 1 + 0.06 * pp + 0.02 * math.sin(t * 7.3) * math.sin(t * 2.1) + 0.03 * math.sin(t * 13.7)

        cx, cy, size, rx, ry, rz, bl = state(t)
        dst = project(cx, cy, size, rx, ry, rz)
        Mp = cv2.getPerspectiveTransform(src_pts, dst)
        layer = cv2.warpPerspective(card_rgba, Mp, (W, H), flags=cv2.INTER_LINEAR, borderValue=0)
        if bl > 0.3:
            layer = cv2.GaussianBlur(layer, (0, 0), bl)
        a = layer[..., 3:4]
        small = cv2.resize(layer, (W // 4, H // 4), interpolation=cv2.INTER_AREA)
        sh = cv2.GaussianBlur(small[..., 3], (0, 0), 12)
        sh = cv2.warpAffine(sh, np.float32([[1, 0, 0], [0, 1, 8]]), (W // 4, H // 4))
        canvas *= (1 - 0.85 * cv2.resize(sh, (W, H))[..., None])
        gl = cv2.GaussianBlur(small[..., :3], (0, 0), 22)
        canvas += cv2.resize(gl, (W, H)) * 0.55 * fl
        canvas = canvas * (1 - a) + layer[..., :3] * fl
        del layer, a, small

        out_k = ease_io((t - TO + 0.1) / 0.5)
        if g1:
            ty = cy + size / 2 + 170          # ниже, чем в v4
            draw_text(canvas, g1, w1, ty, TEXT_T0 - 0.05, stagger, 0.3, t, out_k)

        lk = sum(bump(t, c, 0.5) for c in FOCUS)
        if lk > 0.01:
            canvas = 1 - (1 - canvas) * (1 - lk * 0.28 * np.float32([0.85, 0.95, 0.75]))
        canvas += particles(t) * (1 - ease_in((t - (dur - 1.2)) / 1.0))
        canvas *= vign
        canvas *= min(1, t / 0.25) * (1 - ease_io((t - (dur - 0.7)) / 0.7))
        canvas += rng.normal(0, 0.018, (H, W, 1)).astype(np.float32)
        return (np.clip(canvas, 0, 1) * 255).astype(np.uint8)

    # до ~40 МБ, чтобы бот мог отправить (лимит Telegram 50 МБ)
    vb = min(13.0, 40 * 8 / dur)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", audio, "-map", "0:v", "-map", "1:a",
           "-af", f"afade=t=in:d=0.15,afade=t=out:st={dur - 0.8:.3f}:d=0.8", "-c:a", "aac", "-b:a", "256k",
           "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{vb:.1f}M", "-maxrate", f"{vb * 1.15:.1f}M",
           "-bufsize", f"{vb * 2:.1f}M", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    if NICE:
        cmd = ["nice", "-n", str(NICE)] + cmd
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i in range(N):
            ff.stdin.write(frame(i / FPS).tobytes())
        ff.stdin.close()
    except BrokenPipeError:
        pass
    if ff.wait() != 0:
        raise RuntimeError("ffmpeg: " + ff.stderr.read().decode()[-800:])
    return out


if __name__ == "__main__":   # тест: python -m server.engine.cover_anim cover.jpg track.mp3 out.mp4 56 15
    import sys
    a = sys.argv
    render_cover_anim(a[1], a[2], a[3], float(a[4]), float(a[5]))
