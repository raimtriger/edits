"""Анимация обложки «Сияние» — вертикаль 720x1280, до 15 с.

Раскладка по референсу: звезда ✦ сверху, карточка-обложка (скруглённые углы), под ней три строки текста
(название, подзаголовок, артист). Фон — та же обложка, огромная, размытая, обесцвеченная, внизу уходит в чёрный.

Движение:
  • карточка качается как маятник на ±3° — взмах на каждый «такт» (каждый N-й бит трека, чтобы взмахи шли не чаще раза в ~1,3 с),
    стороны чередуются, на взмахе масштаб чуть подпрыгивает; между взмахами карточка слегка «дышит»;
  • по буквам текста бежит волна яркости (несколько букв на 1–2 кадра гаснут и возвращаются);
  • звезда мерцает и пульсирует на ударах;
  • интро: проявление из чёрного + карточка чуть увеличена и садится на место; финал: затухание вместе со звуком.
Всё лёгкое (numpy + cv2 + Pillow): фон размывается один раз, на кадр — только карточка и текст.
Зависимости те же, что у cover_anim.
"""
from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .cover_anim import detect_beats

W, H, FPS = 720, 1280, 30
TAU = 2 * math.pi
NICE = int(os.environ.get("EDITS_NICE", "0"))
FONTS = Path(__file__).resolve().parents[2] / "fonts"


def ease_out(x): x = min(max(x, 0.0), 1.0); return 1 - (1 - x) ** 3
def ease_io(x): x = min(max(x, 0.0), 1.0); return 3 * x * x - 2 * x ** 3
def lerp(a, b, k): return a + (b - a) * k


def _rounded_mask(size: int, radius: int) -> np.ndarray:
    m = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size * 4 - 1, size * 4 - 1], radius=radius * 4, fill=255)
    return np.asarray(m.resize((size, size), Image.LANCZOS), np.float32)[..., None] / 255


def _star(size: int) -> np.ndarray:
    """Четырёхлучевая звезда ✦ с мягким свечением. Возвращает float32 RGBA size x size."""
    ss = 4
    n = size * ss
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    x, y = (xx - n / 2) / (n / 2), (yy - n / 2) / (n / 2)
    # астроида |x|^p + |y|^p = 1 с p<1 даёт тонкие лучи
    p = 0.42
    d = np.abs(x) ** p + np.abs(y) ** p
    core = np.clip((1.0 - d) * 6.0, 0, 1)
    ring = np.exp(-((np.hypot(x, y)) / 0.07) ** 2) * 1.2            # яркая точка в центре
    glow = np.exp(-(np.hypot(x, y) / 0.22) ** 2) * 0.28
    a = np.clip(core + ring + glow, 0, 1)
    a = cv2.resize(a, (size, size), interpolation=cv2.INTER_AREA)
    return np.dstack([np.ones_like(a), np.ones_like(a), np.ones_like(a), a]).astype(np.float32)


class _Line:
    """Строка текста, разобранная на буквы (для волны яркости и появления по буквам)."""

    def __init__(self, text: str, font_path: str, size: int, track: float, max_w: float):
        self.items: list[tuple[np.ndarray, float]] = []
        pad = 24
        while True:
            f = ImageFont.truetype(font_path, size)
            x, items = 0.0, []
            for ch in text:
                adv = f.getlength(ch)
                if ch != " ":
                    l, _t, r, _b = f.getbbox(ch)
                    im = Image.new("L", (int(r - l) + pad * 2, int(size * 1.35) + pad * 2), 0)
                    ImageDraw.Draw(im).text((pad - l, pad), ch, font=f, fill=255)
                    items.append((np.asarray(im, np.float32)[..., None] / 255, x + l - pad))
                x += adv + track
            total = x - track
            if total <= max_w or size <= 18:
                break
            size = int(size * 0.92)
        self.items, self.total, self.size = items, total, size
        self.n = max(1, len(items))


def render(*, cover, audio, out, start, dur, font_path, fmt, fields, lines):
    dur = float(max(6.0, min(15.0, dur)))
    N = int(FPS * dur)
    rng = np.random.default_rng(11)

    # ---------- такт ----------
    beats = detect_beats(audio, start, dur)
    per = float(np.median(np.diff(beats))) if len(beats) > 2 else 0.5
    stride = max(1, math.ceil(1.3 / max(per, 0.2)))
    hits = [float(b) for b in beats[::stride] if 0.6 <= b <= dur - 0.9]
    if len(hits) < 2:                      # бит не нашёлся — равномерная сетка
        hits = list(np.arange(1.2, dur - 0.9, 1.4))

    # ---------- обложка ----------
    img = cv2.imread(cover)
    if img is None:
        raise ValueError("не удалось прочитать обложку — нужна картинка JPG, PNG или WebP")
    ih, iw = img.shape[:2]
    side = min(ih, iw)
    y0, x0 = (ih - side) // 2, (iw - side) // 2
    cov = cv2.cvtColor(img[y0:y0 + side, x0:x0 + side], cv2.COLOR_BGR2RGB)

    CARD = int(W * 0.87)
    card_px = cv2.resize(cov, (CARD, CARD), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    mask = _rounded_mask(CARD, int(CARD * 0.062))
    card_rgba = np.concatenate([card_px * mask, mask], 2)
    pad = 12                                 # поля вокруг карточки под поворот и тень
    PAD = np.zeros((CARD + pad * 2, CARD + pad * 2, 4), np.float32)
    PAD[pad:pad + CARD, pad:pad + CARD] = card_rgba
    CP = PAD.shape[0]

    # ---------- фон ----------
    BW, BH = int(W * 1.18), int(H * 1.18)
    bgs = max(BW, BH)
    bg = cv2.resize(cov, (bgs, bgs), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    bg = bg[(bgs - BH) // 2:(bgs - BH) // 2 + BH, (bgs - BW) // 2:(bgs - BW) // 2 + BW]
    bg = cv2.GaussianBlur(bg, (0, 0), 46)
    grey = bg.mean(2, keepdims=True)
    bg = grey + (bg - grey) * 0.55                           # обесцвечиваем, но красные/синие акценты остаются
    bg = np.clip((bg - 0.5) * 1.15 + 0.5, 0, 1) * 0.6
    ys = np.linspace(0, 1, BH, dtype=np.float32)[:, None, None]
    bg = bg * (1.0 - 0.9 * np.clip((ys - 0.52) / 0.42, 0, 1) ** 1.3) * (0.92 - 0.12 * ys)

    # ---------- звезда ----------
    STAR = _star(150)
    star_cy = int(H * 0.098)

    # ---------- текст ----------
    title = (fields.get("title") or "").strip()
    sub = (fields.get("subtitle") or "").strip()
    artist = (fields.get("artist") or "").strip()
    f_black = font_path or str(FONTS / "unbounded-black.ttf")
    f_sub = font_path or str(FONTS / "unbounded-black.ttf")
    maxw = W * 0.9
    L_title = _Line(title, f_black, 104, 2, maxw) if title else None
    L_sub = _Line(sub, f_sub, 34, 0, maxw) if sub else None
    L_art = _Line(artist, f_black, 36, 1, maxw) if artist else None
    blocks = [(L_title, 0.0), (L_sub, 0.0), (L_art, 0.0)]
    blocks = [b for b in blocks if b[0]]

    card_top = int(H * 0.158)
    card_cy = card_top + CARD // 2
    text_y = card_top + CARD + 28                     # верх первой строки

    def blit_mask(canvas, alpha_buf, a, x, y, color, alpha):
        """Кладёт букву в слой текста: canvas — премультиплицированный цвет, alpha_buf — накопленная прозрачность."""
        h, w = a.shape[:2]
        x, y = int(round(x)), int(round(y))
        xa, ya, xb, yb = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
        if xb <= xa or yb <= ya:
            return
        s = a[ya - y:yb - y, xa - x:xb - x] * alpha
        c = canvas[ya:yb, xa:xb]
        c[:] = c * (1 - s) + s * color
        al = alpha_buf[ya:yb, xa:xb]
        al[:] = al * (1 - s) + s

    def draw_lines(canvas, alpha_buf, t):
        y = text_y
        for li, (ln, _) in enumerate(blocks):
            x0 = W / 2 - ln.total / 2
            t0 = 0.55 + li * 0.35
            for i, (a, gx) in enumerate(ln.items):
                k = ease_out((t - t0 - i * 0.035) / 0.45)
                if k <= 0:
                    continue
                # волна яркости: «провал» бежит слева направо раз в ~1,4 с (со сдвигом по строкам)
                ph = ((t * 0.72 - li * 0.23) % 1.0) * 1.5 - 0.25
                pos = i / ln.n
                dip = math.exp(-((pos - ph) / 0.11) ** 2)
                shade = 1.0 - 0.58 * dip
                # лёгкий градиент белый -> светло-серый по высоте строки
                a2 = a
                if k < 1:
                    sg = (1 - k) * 7
                    a2 = cv2.GaussianBlur(a, (0, 0), sg)[..., None] if sg > 0.4 else a
                blit_mask(canvas, alpha_buf, a2, x0 + gx, y + (1 - k) * 18, np.float32([1, 1, 1]) * shade, k)
            y += int(ln.size * 1.55) + (6 if li == 0 else 0)

    # ---------- кадр ----------
    def swing(t):
        """Сумма затухающих взмахов: стороны чередуются, амплитуда 2,5–3,2°."""
        ang, pop = 0.0, 0.0
        for j, h in enumerate(hits):
            dt = t - h
            if dt < 0 or dt > 3.0:
                continue
            s = 1 if j % 2 == 0 else -1
            amp = 2.6 + 0.6 * ((j * 7) % 3) / 2
            ang += s * amp * (1 - math.exp(-dt / 0.06)) * math.exp(-dt / 0.55) * math.cos(TAU * dt / 1.9)
            pop += math.exp(-dt / 0.22) * (1 - math.exp(-dt / 0.04))
        return ang, min(pop, 1.4)

    def frame(t):
        ang, pop = swing(t)
        # фон: медленный дрейф и вдох на ударах
        z = 1.0 + 0.05 * t / dur + 0.012 * pop
        sc = z
        M = np.float32([[sc, 0, W / 2 - BW / 2 * sc + 10 * math.sin(t * 0.35)],
                        [0, sc, H / 2 - BH / 2 * sc + 8 * math.cos(t * 0.27)]])
        canvas = cv2.warpAffine(bg, M, (W, H), borderMode=cv2.BORDER_REFLECT)
        canvas *= 1 + 0.05 * pop

        # карточка
        intro = ease_out(t / 1.0)
        breathe = 1 + 0.022 * math.sin(TAU * t / 2.8)
        scale = lerp(1.07, 1.0, intro) * breathe * (1 + 0.032 * pop)
        R = cv2.getRotationMatrix2D((CP / 2, CP / 2), ang, scale)
        R[0, 2] += W / 2 - CP / 2
        R[1, 2] += card_cy - CP / 2 + 5 * math.sin(TAU * t / 3.3)
        layer = cv2.warpAffine(PAD, R, (W, H), flags=cv2.INTER_LINEAR, borderValue=0)
        a = layer[..., 3:4]
        sh = cv2.GaussianBlur(cv2.resize(a[..., 0], (W // 4, H // 4), interpolation=cv2.INTER_AREA), (0, 0), 7)
        sh = cv2.warpAffine(sh, np.float32([[1, 0, 0], [0, 1, 6]]), (W // 4, H // 4))
        canvas *= 1 - 0.55 * cv2.resize(sh, (W, H))[..., None]
        canvas = canvas * (1 - a) + layer[..., :3]

        # звезда
        sk = (1 + 0.16 * pop) * (0.94 + 0.06 * math.sin(t * 5.1))
        ssz = int(STAR.shape[0] * sk)
        st = cv2.resize(STAR, (ssz, ssz), interpolation=cv2.INTER_LINEAR)
        sa = st[..., 3:4] * (0.85 + 0.15 * math.sin(t * 3.3 + 1)) * ease_out(t / 0.6)
        sx, sy = W // 2 - ssz // 2, star_cy - ssz // 2
        canvas[sy:sy + ssz, sx:sx + ssz] = canvas[sy:sy + ssz, sx:sx + ssz] * (1 - sa) + sa

        # текст + мягкий ореол
        tal = np.zeros((H, W, 1), np.float32)
        tcol = np.zeros((H, W, 3), np.float32)
        draw_lines(tcol, tal, t)
        glow = cv2.GaussianBlur(cv2.resize(tcol, (W // 2, H // 2)), (0, 0), 5)
        canvas += cv2.resize(glow, (W, H)) * 0.30
        canvas = canvas * (1 - tal) + tcol

        canvas *= min(1, t / 0.45) * (1 - ease_io((t - (dur - 0.7)) / 0.7))
        canvas += rng.normal(0, 0.012, (H, W, 1)).astype(np.float32)
        return (np.clip(canvas, 0, 1) * 255).astype(np.uint8)

    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", audio, "-map", "0:v", "-map", "1:a",
           "-af", f"afade=t=in:d=0.15,afade=t=out:st={dur - 0.8:.3f}:d=0.8", "-c:a", "aac", "-b:a", "192k",
           "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "6M", "-bufsize", "12M",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
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


if __name__ == "__main__":   # python -m server.engine.cover_glow cover.jpg track.mp3 out.mp4 0 15 "Маяк" "(prod. by X)" "X"
    import sys
    a = sys.argv
    render(cover=a[1], audio=a[2], out=a[3], start=float(a[4]), dur=float(a[5]), font_path=None,
           fmt={"w": W, "h": H, "fps": FPS},
           fields={"title": a[6] if len(a) > 6 else "", "subtitle": a[7] if len(a) > 7 else "", "artist": a[8] if len(a) > 8 else ""},
           lines=[])
