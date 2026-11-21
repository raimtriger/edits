"""Анимация обложки «Сияние» — вертикаль 720x1280, до 15 с.

Раскладка: карточка-обложка (скруглённые углы), под ней строки текста (название, подзаголовок, артист).
Фон — та же обложка, огромная, размытая, обесцвеченная, внизу уходит в чёрный.

Движение (всё плавное — пружины, а не рывки):
  • интро: обложка влетает в кадр крупно, размытая и развёрнутая в 3D, и за ~1,4 с садится карточкой;
  • каждый реальный удар в звуке толкает карточку: масштаб подпрыгивает и пружинит назад, она слегка кренится
    (стороны чередуются) и подпрыгивает вверх; сила толчка зависит от силы удара и от того, насколько
    громкий и плотный звук в этот момент — динамичный трек = заметнее, тихое место = едва-едва;
  • звука нет (тишина) — только медленное спокойное покачивание и «дыхание»;
  • по буквам текста бежит волна яркости; на ударах текст чуть светлее.
Лёгкое (numpy + cv2 + Pillow): фон размывается один раз, на кадр — карточка и текст.
"""
from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 720, 1280, 30
TAU = 2 * math.pi
NICE = int(os.environ.get("EDITS_NICE", "0"))
FONTS = Path(__file__).resolve().parents[2] / "fonts"
INTRO = 1.4          # секунд на залёт; удары в это время карточку не толкают


def ease_out(x): x = min(max(x, 0.0), 1.0); return 1 - (1 - x) ** 3
def ease_io(x): x = min(max(x, 0.0), 1.0); return 3 * x * x - 2 * x ** 3
def lerp(a, b, k): return a + (b - a) * k


# ───────────────────────── звук ─────────────────────────
def analyze(audio: str, start: float, dur: float) -> dict:
    """Реальные удары и «динамичность» звука.
    Возвращает {"sound": bool, "hits": [(t, сила 0.3..1)], "dyn": массив 0..1 на каждый кадр видео}."""
    n_fr = int(FPS * dur)
    silent = {"sound": False, "hits": [], "dyn": np.zeros(n_fr, np.float32)}
    sr, hop, nfft = 11025, 256, 1024
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur + 0.3:.3f}", "-i", audio,
                          "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True).stdout
    y = np.frombuffer(raw, np.float32)
    if len(y) < nfft * 4 or float(np.sqrt(np.mean(y ** 2))) < 0.004:
        return silent

    # огибающая громкости -> динамичность
    c = np.concatenate([[0.0], np.cumsum(y.astype(np.float64) ** 2)])
    rms = np.empty(n_fr)
    for i in range(n_fr):
        lo = int(max(0, (i / FPS - 0.1) * sr)); hi = int(min(len(y), (i / FPS + 0.1) * sr))
        rms[i] = math.sqrt(max(c[hi] - c[lo], 0.0) / max(hi - lo, 1))
    rms = np.convolve(rms, np.ones(24) / 24, "same")
    p10, p95 = np.percentile(rms, 10), np.percentile(rms, 95)
    rel = np.clip((rms - p10) / (p95 - p10 + 1e-6), 0, 1)
    dyn = np.clip(0.55 * rel + 0.45 * np.clip(rms / 0.15, 0, 1), 0, 1).astype(np.float32)

    # удары: всплески энергии (низ — кик, весь спектр — снейр/хет)
    frames = np.lib.stride_tricks.sliding_window_view(y, nfft)[::hop] * np.hanning(nfft)
    spec = np.log1p(np.abs(np.fft.rfft(frames, axis=1)))
    freqs = np.fft.rfftfreq(nfft, 1 / sr)
    d = np.maximum(0, np.diff(spec, axis=0))
    fl, fa = d[:, freqs < 250].sum(1), d.sum(1)
    z = lambda v: (v - np.median(v)) / (v.std() + 1e-6)
    o = np.concatenate([[0], z(fl) + 0.5 * z(fa)])
    o = np.convolve(o, [0.25, 0.5, 0.25], "same")
    thr = max(float(np.percentile(o, 82)), 0.5)
    top = float(np.percentile(o, 98))
    fps_o = sr / hop
    hits, last = [], -1.0
    for i in range(2, len(o) - 2):
        if o[i] > thr and o[i] == o[i - 2:i + 3].max():
            t = (i * hop + nfft / 2) / sr - 0.01
            if t - last >= 0.2 and 0 <= t < dur:
                hits.append((t, 0.3 + 0.7 * min(1.0, (o[i] - thr) / (top - thr + 1e-6))))
                last = t
    return {"sound": True, "hits": hits, "dyn": dyn}


def simulate(an: dict, N: int):
    """Пружинная модель карточки: на каждый удар — импульс скорости, дальше мягкое затухание.
    Возвращает массивы на кадр: масштаб (доля), угол (°), смещение по y (px)."""
    sc, ang, yo = np.zeros(N), np.zeros(N), np.zeros(N)
    if not an["sound"]:
        return sc, ang, yo
    ws, zs = TAU * 2.0, 0.5       # масштаб: ~2 Гц, умеренно пружинит
    wa, za = TAU * 1.1, 0.45      # крен: медленнее
    wy, zy = TAU * 2.4, 0.6       # подпрыгивание
    xs = vs = xa = va = xy = vy = 0.0
    hits, hi, sign = an["hits"], 0, 1
    sub = 4
    dt = 1.0 / FPS / sub
    for i in range(N):
        for k in range(sub):
            tt = i / FPS + k * dt
            while hi < len(hits) and hits[hi][0] <= tt:
                th, st = hits[hi]; hi += 1
                if th < INTRO:
                    continue
                power = st * (0.35 + 0.9 * float(an["dyn"][min(i, N - 1)]))      # ~0.1 … 1.25
                vs += 0.09 * ws * power
                sign = -sign
                va += sign * 2.6 * wa * power
                vy += -9.0 * wy * power
            vs += (-ws * ws * xs - 2 * zs * ws * vs) * dt; xs += vs * dt
            va += (-wa * wa * xa - 2 * za * wa * va) * dt; xa += va * dt
            vy += (-wy * wy * xy - 2 * zy * wy * vy) * dt; xy += vy * dt
        sc[i], ang[i], yo[i] = min(xs, 0.09), max(-4.0, min(4.0, xa)), max(-14.0, min(14.0, xy))
    return sc, ang, yo


# ───────────────────────── графика ─────────────────────────
def _rounded_mask(size: int, radius: int) -> np.ndarray:
    m = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size * 4 - 1, size * 4 - 1], radius=radius * 4, fill=255)
    return np.asarray(m.resize((size, size), Image.LANCZOS), np.float32)[..., None] / 255


def _project(cx, cy, size, rx, ry, rz, f=2000.0):
    """Углы квадрата после 3D-поворота и перспективы -> 4 точки на экране."""
    h = size / 2
    pts = np.array([[-h, -h, 0], [h, -h, 0], [h, h, 0], [-h, h, 0]], np.float64)
    rx, ry, rz = map(math.radians, (rx, ry, rz))
    Rx = np.array([[1, 0, 0], [0, math.cos(rx), -math.sin(rx)], [0, math.sin(rx), math.cos(rx)]])
    Ry = np.array([[math.cos(ry), 0, math.sin(ry)], [0, 1, 0], [-math.sin(ry), 0, math.cos(ry)]])
    Rz = np.array([[math.cos(rz), -math.sin(rz), 0], [math.sin(rz), math.cos(rz), 0], [0, 0, 1]])
    p = pts @ (Rz @ Ry @ Rx).T
    z = f + p[:, 2]
    return np.float32(np.stack([cx + p[:, 0] * f / z, cy + p[:, 1] * f / z], 1))


class _Line:
    """Строка текста, разобранная на буквы (для волны яркости и появления по буквам)."""

    def __init__(self, text: str, font_path: str, size: int, track: float, max_w: float, shrink: bool = True):
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
            if not shrink or total <= max_w or size <= 18:
                break
            size = int(size * 0.92)
        self.items, self.total, self.size = items, total, size
        self.n = max(1, len(items))


def render(*, cover, audio, out, start, dur, font_path, fmt, fields, lines):
    dur = float(max(6.0, min(15.0, dur)))
    N = int(FPS * dur)
    rng = np.random.default_rng(11)

    an = analyze(audio, start, dur)
    SC, ANG, YO = simulate(an, N)
    DYN = an["dyn"]

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
    pad = 12
    PAD = np.zeros((CARD + pad * 2, CARD + pad * 2, 4), np.float32)
    PAD[pad:pad + CARD, pad:pad + CARD] = np.concatenate([card_px * mask, mask], 2)
    CP = PAD.shape[0]
    src_pts = np.float32([[0, 0], [CP, 0], [CP, CP], [0, CP]])

    # ---------- фон ----------
    BW, BH = int(W * 1.18), int(H * 1.18)
    bgs = max(BW, BH)
    bg = cv2.resize(cov, (bgs, bgs), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    bg = bg[(bgs - BH) // 2:(bgs - BH) // 2 + BH, (bgs - BW) // 2:(bgs - BW) // 2 + BW]
    bg = cv2.GaussianBlur(bg, (0, 0), 46)
    grey = bg.mean(2, keepdims=True)
    bg = grey + (bg - grey) * 0.55
    bg = np.clip((bg - 0.5) * 1.15 + 0.5, 0, 1) * 0.6
    ys = np.linspace(0, 1, BH, dtype=np.float32)[:, None, None]
    bg = bg * (1.0 - 0.9 * np.clip((ys - 0.52) / 0.42, 0, 1) ** 1.3) * (0.92 - 0.12 * ys)

    # ---------- текст ----------
    title = (fields.get("title") or "").strip()
    sub = (fields.get("subtitle") or "").strip()
    artist = (fields.get("artist") or "").strip()
    f_black = font_path or str(FONTS / "unbounded-black.ttf")
    maxw = W * 0.9

    def title_lines(text):
        one = _Line(text, f_black, 104, 2, maxw)
        if one.size >= 76 or " " not in text:
            return [one]
        words = text.split()
        cut = min(range(1, len(words)), key=lambda k: abs(len(" ".join(words[:k])) - len(" ".join(words[k:]))))
        a, b = " ".join(words[:cut]), " ".join(words[cut:])
        size = min(_Line(a, f_black, 104, 2, maxw).size, _Line(b, f_black, 104, 2, maxw).size)
        if size <= one.size:
            return [one]
        return [_Line(a, f_black, size, 2, maxw, False), _Line(b, f_black, size, 2, maxw, False)]

    # (строка, отступ от предыдущей в долях её размера)
    blocks: list[tuple[_Line, float]] = []
    if title:
        tl = title_lines(title)
        for j, ln in enumerate(tl):
            blocks.append((ln, 0.0 if j == 0 else 1.14))
    if sub:
        blocks.append((_Line(sub, f_black, 34, 0, maxw), 1.6))
    if artist:
        blocks.append((_Line(artist, f_black, 36, 1, maxw), 1.55))

    card_top = int(H * 0.158)
    card_cy = card_top + CARD // 2 + 8
    text_y0 = card_top + CARD + 36
    ypos, y = [], float(text_y0)
    for k, (ln, gap) in enumerate(blocks):
        y += gap * blocks[k - 1][0].size if k else 0
        ypos.append(y)

    def blit_mask(canvas, alpha_buf, a, x, yy, color, alpha):
        """Кладёт букву в слой текста: canvas — премультиплицированный цвет, alpha_buf — накопленная прозрачность."""
        h, w = a.shape[:2]
        x, yy = int(round(x)), int(round(yy))
        xa, ya, xb, yb = max(x, 0), max(yy, 0), min(x + w, W), min(yy + h, H)
        if xb <= xa or yb <= ya:
            return
        s = a[ya - yy:yb - yy, xa - x:xb - x] * alpha
        c = canvas[ya:yb, xa:xb]
        c[:] = c * (1 - s) + s * color
        al = alpha_buf[ya:yb, xa:xb]
        al[:] = al * (1 - s) + s

    def draw_lines(canvas, alpha_buf, t, lift):
        for li, ((ln, _), yy) in enumerate(zip(blocks, ypos)):
            x0 = W / 2 - ln.total / 2
            t0 = 0.95 + li * 0.3
            for i, (a, gx) in enumerate(ln.items):
                k = ease_out((t - t0 - i * 0.035) / 0.45)
                if k <= 0:
                    continue
                ph = ((t * 0.36 - li * 0.23) % 1.0) * 1.5 - 0.25
                dip = math.exp(-(((i / ln.n) - ph) / 0.11) ** 2)
                shade = min(1.0, (1.0 - 0.42 * dip) * (1 + 0.1 * lift))
                a2 = a
                if k < 1:
                    sg = (1 - k) * 7
                    a2 = cv2.GaussianBlur(a, (0, 0), sg)[..., None] if sg > 0.4 else a
                blit_mask(canvas, alpha_buf, a2, x0 + gx, yy + (1 - k) * 18, np.float32([1, 1, 1]) * shade, k)

    # ---------- кадр ----------
    calm = not an["sound"]

    def frame(i):
        t = i / FPS
        if calm:       # тишина: медленное спокойное покачивание
            sc_, ang_, yo_ = 0.012 * math.sin(TAU * t / 5.0 + 1.0), 1.0 * math.sin(TAU * t / 7.0), 4 * math.sin(TAU * t / 6.0)
        else:
            sc_, ang_, yo_ = SC[i], ANG[i], YO[i]
        lift = max(0.0, sc_) / 0.05            # 0..~1 — «сила толчка» для подсветки

        # фон: медленный дрейф + лёгкий вдох на толчках
        z = 1.0 + 0.05 * t / dur + 0.5 * sc_
        M = np.float32([[z, 0, W / 2 - BW / 2 * z + 10 * math.sin(t * 0.35)],
                        [0, z, H / 2 - BH / 2 * z + 8 * math.cos(t * 0.27)]])
        canvas = cv2.warpAffine(bg, M, (W, H), borderMode=cv2.BORDER_REFLECT)
        canvas *= 1 + 0.9 * max(sc_, 0)

        # карточка: залёт -> дыхание + удары
        u = ease_out(t / INTRO)
        breathe = 1 + 0.012 * math.sin(TAU * t / 3.6)
        size = CP * lerp(1.95, 1.0, u) * breathe * (1 + sc_)
        cy = lerp(H * 0.56, card_cy, u) + yo_ + 4 * math.sin(TAU * t / 3.3) * u
        rx, ry, rz = lerp(13, 0, u), lerp(-58, 0, ease_out(t / (INTRO * 0.95))), lerp(-9, 0, u) + ang_
        Mp = cv2.getPerspectiveTransform(src_pts, _project(W / 2, cy, size, rx, ry, rz))
        layer = cv2.warpPerspective(PAD, Mp, (W, H), flags=cv2.INTER_LINEAR, borderValue=0)
        bl = 15 * (1 - ease_out(t / 1.0))
        if bl > 0.4:
            layer = cv2.GaussianBlur(layer, (0, 0), bl)
        a = layer[..., 3:4]
        sh = cv2.GaussianBlur(cv2.resize(a[..., 0], (W // 4, H // 4), interpolation=cv2.INTER_AREA), (0, 0), 7)
        sh = cv2.warpAffine(sh, np.float32([[1, 0, 0], [0, 1, 6]]), (W // 4, H // 4))
        canvas *= 1 - 0.55 * cv2.resize(sh, (W, H))[..., None]
        canvas = canvas * (1 - a) + layer[..., :3]

        # текст + мягкий ореол
        tal = np.zeros((H, W, 1), np.float32)
        tcol = np.zeros((H, W, 3), np.float32)
        draw_lines(tcol, tal, t, lift)
        glow = cv2.GaussianBlur(cv2.resize(tcol, (W // 2, H // 2)), (0, 0), 5)
        canvas += cv2.resize(glow, (W, H)) * (0.30 + 0.12 * lift)
        canvas = canvas * (1 - tal) + tcol

        canvas *= min(1, t / 0.25) * (1 - ease_io((t - (dur - 0.7)) / 0.7))
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
            ff.stdin.write(frame(i).tobytes())
        ff.stdin.close()
    except BrokenPipeError:
        pass
    if ff.wait() != 0:
        raise RuntimeError("ffmpeg: " + ff.stderr.read().decode()[-800:])


if __name__ == "__main__":   # python -m server.engine.cover_glow cover.jpg track.mp3 out.mp4 0 15 "Название" "(prod. by X)" "Артист"
    import sys
    a = sys.argv
    render(cover=a[1], audio=a[2], out=a[3], start=float(a[4]), dur=float(a[5]), font_path=None,
           fmt={"w": W, "h": H, "fps": FPS},
           fields={"title": a[6] if len(a) > 6 else "", "subtitle": a[7] if len(a) > 7 else "", "artist": a[8] if len(a) > 8 else ""},
           lines=[])
