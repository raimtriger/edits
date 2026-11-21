"""Анимация обложки «Монета» — вертикаль 720x1280, до 15 с.

По разбору референса (story со «Слушать»):
  • 0–0,25 с: обложка на весь кадр, размытая, крупная; быстро садится в карточку;
  • карточка крутится «монеткой» вокруг вертикальной оси: ребром на ~0,6 с, обратной стороной (картинка зеркальная)
    до ~1,4 с, дальше замедляется и ложится в плоскость к ~2,4 с;
  • у карточки стеклянный край: тонкая светлая кайма и внутреннее свечение;
  • надпись сверху проявляется по буквам, название собирается со «скрэмблом» (обрывки букв), потом артист;
  • световые вспышки цвета обложки на сильных ударах и на переворотах;
  • второй переворот примерно на середине ролика (в референсе цикл повторялся).
Удары в звуке толкают карточку так же, как в «Сиянии» (пружины), но крен идёт вокруг вертикальной оси — карточка «качается»,
как стекло. Звука нет — спокойное дыхание без толчков и вспышек.
"""
from __future__ import annotations

import math
import os
import subprocess

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .cover_glow import (FONTS, FPS, H, W, TAU, _Line, _project, _rounded_mask, analyze, ease_io, ease_out, lerp,
                         simulate)

NICE = int(os.environ.get("EDITS_NICE", "0"))
SPIN_T0, SPIN_DUR = 0.25, 2.2        # первый переворот
FLIP_DUR = 1.7                       # второй переворот


def _accent(cov: np.ndarray) -> np.ndarray:
    """Цвет вспышек из обложки: среднее самых насыщенных светлых пикселей (RGB 0..1). Нет цвета — тёплый розовый."""
    small = cv2.resize(cov, (64, 64), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV).reshape(-1, 3).astype(np.float32)
    ok = hsv[(hsv[:, 1] > 90) & (hsv[:, 2] > 110)]
    if len(ok) < 20:
        return np.float32([1.0, 0.45, 0.75])
    ang = ok[:, 0] * 2 * math.pi / 180
    hue = (math.atan2(np.sin(ang).mean(), np.cos(ang).mean()) % TAU) * 180 / math.pi / 2
    rgb = cv2.cvtColor(np.uint8([[[hue, 190, 255]]]), cv2.COLOR_HSV2RGB)[0, 0]
    return rgb.astype(np.float32) / 255


def render(*, cover, audio, out, start, dur, font_path, fmt, fields, lines):
    dur = float(max(6.0, min(15.0, dur)))
    N = int(FPS * dur)
    rng = np.random.default_rng(5)

    an = analyze(audio, start, dur)
    SC, ANG, YO = simulate(an, N)
    calm = not an["sound"]

    # ---------- обложка ----------
    img = cv2.imread(cover)
    if img is None:
        raise ValueError("не удалось прочитать обложку — нужна картинка JPG, PNG или WebP")
    ih, iw = img.shape[:2]
    side = min(ih, iw)
    y0, x0 = (ih - side) // 2, (iw - side) // 2
    cov = cv2.cvtColor(img[y0:y0 + side, x0:x0 + side], cv2.COLOR_BGR2RGB)
    accent = _accent(cov)

    CARD = int(W * 0.84)
    card_px = cv2.resize(cov, (CARD, CARD), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    rad = int(CARD * 0.06)
    mask = _rounded_mask(CARD, rad)
    # стеклянный край: тонкая светлая кайма + мягкое свечение внутрь, сверху ярче
    e = Image.new("L", (CARD * 4, CARD * 4), 0)
    ImageDraw.Draw(e).rounded_rectangle([0, 0, CARD * 4 - 1, CARD * 4 - 1], radius=rad * 4, outline=255, width=10)
    edge = np.asarray(e.resize((CARD, CARD), Image.LANCZOS), np.float32)[..., None] / 255
    inner = cv2.GaussianBlur(edge, (0, 0), 9)[..., None] if edge.ndim == 3 else cv2.GaussianBlur(edge, (0, 0), 9)
    inner = inner.reshape(CARD, CARD, 1)
    grad = np.linspace(1.0, 0.45, CARD, dtype=np.float32)[:, None, None]
    card_px = np.clip(card_px + edge * 0.42 * grad + inner * 0.20 * grad, 0, 1)
    pad = 12
    PAD = np.zeros((CARD + pad * 2, CARD + pad * 2, 4), np.float32)
    PAD[pad:pad + CARD, pad:pad + CARD] = np.concatenate([card_px * mask, mask], 2)
    CP = PAD.shape[0]
    src_pts = np.float32([[0, 0], [CP, 0], [CP, CP], [0, CP]])

    # ---------- фон: тёмная размытая копия обложки, цвет сохраняем ----------
    BW, BH = int(W * 1.2), int(H * 1.2)
    bgs = max(BW, BH)
    bg = cv2.resize(cov, (bgs, bgs), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    bg = bg[(bgs - BH) // 2:(bgs - BH) // 2 + BH, (bgs - BW) // 2:(bgs - BW) // 2 + BW]
    bg = cv2.GaussianBlur(bg, (0, 0), 50)
    grey = bg.mean(2, keepdims=True)
    bg = grey + (bg - grey) * 0.9
    bg = np.clip((bg - 0.45) * 1.2 + 0.45, 0, 1) * 0.5
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    vign = np.clip(1 - 0.7 * (np.sqrt(((xx - W / 2) / (W * 0.8)) ** 2 + ((yy - H / 2) / (H * 0.65)) ** 2)) ** 2.2, 0.2, 1)[..., None]
    del yy, xx

    # ---------- вспышки ----------
    flare_base = np.zeros((H // 4, W // 4), np.float32)
    cv2.ellipse(flare_base, (W // 8, H // 8), (W // 5, H // 4), 25, 0, 360, 1.0, -1)
    flare_base = cv2.GaussianBlur(flare_base, (0, 0), 28)
    flare_base = cv2.resize(flare_base, (W, H))[..., None]

    # когда что происходит
    hits = [(t, s) for t, s in an["hits"] if s > 0.78]
    flash_t, last = [SPIN_T0 + 0.1], SPIN_T0
    for t, s in hits:
        if t > 2.6 and t - last > 1.3:
            flash_t.append(t); last = t
    strong = [(t, s) for t, s in an["hits"] if dur * 0.42 <= t <= dur * 0.62]
    flip2 = max(strong, key=lambda x: x[1])[0] if strong else dur * 0.5
    flip2 = min(flip2, dur - FLIP_DUR - 1.0)
    flash_t.append(flip2)
    if calm:
        flash_t = [SPIN_T0 + 0.1, flip2]

    # ---------- текст ----------
    caption = (fields.get("caption") or "").strip()
    title = (fields.get("title") or "").strip()
    artist = (fields.get("artist") or "").strip()
    f_black = font_path or str(FONTS / "unbounded-black.ttf")
    f_cap = str(FONTS / "inter.ttf")
    maxw = W * 0.9

    def title_lines(text):
        one = _Line(text, f_black, 96, 2, maxw)
        if one.size >= 70 or " " not in text:
            return [one]
        words = text.split()
        cut = min(range(1, len(words)), key=lambda k: abs(len(" ".join(words[:k])) - len(" ".join(words[k:]))))
        a, b = " ".join(words[:cut]), " ".join(words[cut:])
        size = min(_Line(a, f_black, 96, 2, maxw).size, _Line(b, f_black, 96, 2, maxw).size)
        if size <= one.size:
            return [one]
        return [_Line(a, f_black, size, 2, maxw, False), _Line(b, f_black, size, 2, maxw, False)]

    card_cy0 = int(H * 0.455)
    card_bottom = card_cy0 + CARD // 2
    cap_ln = _Line(caption, f_cap, 30, 9, maxw) if caption else None
    cap_y = int(card_cy0 - CARD / 2 - 70)
    tblocks: list[tuple[_Line, float, float]] = []      # (строка, отступ, время старта)
    if title:
        for j, ln in enumerate(title_lines(title)):
            tblocks.append((ln, 0.0 if j == 0 else 1.14, 1.35 + j * 0.25))
    if artist:
        tblocks.append((_Line(artist, f_black, 30, 6, maxw), 1.7, 2.3))
    ypos, y = [], float(card_bottom + 44)
    for k, (ln, gap, _t0) in enumerate(tblocks):
        y += gap * tblocks[k - 1][0].size if k else 0
        ypos.append(y)

    def blit(tcol, tal, a, x, yy, color, alpha):
        h, w = a.shape[:2]
        x, yy = int(round(x)), int(round(yy))
        xa, ya, xb, yb = max(x, 0), max(yy, 0), min(x + w, W), min(yy + h, H)
        if xb <= xa or yb <= ya:
            return
        s = a[ya - yy:yb - yy, xa - x:xb - x] * alpha
        c = tcol[ya:yb, xa:xb]
        c[:] = c * (1 - s) + s * color
        al = tal[ya:yb, xa:xb]
        al[:] = al * (1 - s) + s

    def draw_text(tcol, tal, t, lift, i_frame):
        white = np.float32([1, 1, 1])
        if cap_ln:
            x0c = W / 2 - cap_ln.total / 2
            for i, (a, gx) in enumerate(cap_ln.items):
                k = ease_out((t - 1.7 - i * 0.05) / 0.5)
                if k > 0:
                    sg = (1 - k) * 6
                    a2 = cv2.GaussianBlur(a, (0, 0), sg)[..., None] if sg > 0.4 else a
                    blit(tcol, tal, a2, x0c + gx, cap_y, white * 0.92, k)
        for li, ((ln, _g, t0), yy) in enumerate(zip(tblocks, ypos)):
            x0 = W / 2 - ln.total / 2
            for i, (a, gx) in enumerate(ln.items):
                r = t0 + i * 0.055                      # когда буква «встаёт» на место
                if t < r - 0.38:
                    continue
                if t < r:                               # скрэмбл: чужая буква, размытая, мерцает
                    pick = ln.items[int(rng_hash(li, i, int(t * 14)) % ln.n)][0]
                    k = 0.25 + 0.5 * (1 - (r - t) / 0.38)
                    blit(tcol, tal, cv2.GaussianBlur(pick, (0, 0), 3)[..., None], x0 + gx, yy, white * 0.7, k * 0.8)
                    continue
                k = ease_out((t - r) / 0.3)
                ph = ((t * 0.36 - li * 0.23) % 1.0) * 1.5 - 0.25
                dip = math.exp(-(((i / ln.n) - ph) / 0.11) ** 2)
                shade = min(1.0, (1.0 - 0.42 * dip) * (1 + 0.1 * lift))
                a2 = a if k >= 1 else cv2.GaussianBlur(a, (0, 0), (1 - k) * 4)[..., None]
                blit(tcol, tal, a2, x0 + gx, yy + (1 - k) * 10, white * shade, 0.4 + 0.6 * k)

    def rng_hash(a, b, c):
        return (a * 73856093) ^ (b * 19349663) ^ (c * 83492791)

    # ---------- угол монеты ----------
    def spin_angle(t):
        a = 0.0
        if t >= SPIN_T0:
            x = min((t - SPIN_T0) / SPIN_DUR, 1.0)
            a += 360.0 * (1 - (1 - x) ** 2)
        if t >= flip2:
            x = min((t - flip2) / FLIP_DUR, 1.0)
            a += 360.0 * (1 - (1 - x) ** 2)
        return a

    def frame(i):
        t = i / FPS
        if calm:
            sc_, ang_, yo_ = 0.012 * math.sin(TAU * t / 5.0 + 1.0), 1.2 * math.sin(TAU * t / 7.0), 4 * math.sin(TAU * t / 6.0)
        else:
            sc_, ang_, yo_ = SC[i], ANG[i], YO[i]
        lift = max(0.0, sc_) / 0.05

        z = 1.0 + 0.05 * t / dur + 0.5 * sc_
        M = np.float32([[z, 0, W / 2 - BW / 2 * z + 12 * math.sin(t * 0.3)], [0, z, H / 2 - BH / 2 * z + 9 * math.cos(t * 0.23)]])
        canvas = cv2.warpAffine(bg, M, (W, H), borderMode=cv2.BORDER_REFLECT)
        canvas *= 1 + 0.9 * max(sc_, 0)

        # вспышки: короткий всплеск цвета обложки, угол меняется от вспышки к вспышке
        fl = 0.0
        for j, ft in enumerate(flash_t):
            d = t - ft
            if 0 <= d < 1.2:
                fl_j = (1 - math.exp(-d / 0.05)) * math.exp(-d / 0.28)
                fl = max(fl, fl_j)
                fx = (j % 2) * 220 - 110
        if fl > 0.01:
            Mf = np.float32([[1, 0, fx], [0, 1, 0]])
            fb = cv2.warpAffine(flare_base, Mf, (W, H)) if abs(fx) > 1 else flare_base
            canvas += fb.reshape(H, W, 1) * accent * (0.5 * fl)
            canvas += accent * (0.10 * fl)

        # карточка
        u = ease_out(t / 0.9)
        size = CP * lerp(1.95, 1.0, u) * (1 + sc_) * (1 + 0.012 * math.sin(TAU * t / 3.6))
        cy = lerp(H * 0.52, card_cy0, u) + yo_
        ry = spin_angle(t) + ang_
        rx = 6 * math.sin(TAU * t / 5.2) * ease_io((t - 2.4) / 1.0) + lerp(10, 0, u)
        rz = lerp(-12, 0, u) + 0.3 * ang_
        Mp = cv2.getPerspectiveTransform(src_pts, _project(W / 2, cy, size, rx, ry, rz, f=1700.0))
        layer = cv2.warpPerspective(PAD, Mp, (W, H), flags=cv2.INTER_LINEAR, borderValue=0)
        bl = 14 * (1 - ease_out(t / 0.7)) + 2.5 * abs(math.sin(math.radians(ry))) * (1 - ease_out((t - 0.3) / 2.2)) * (t < SPIN_T0 + SPIN_DUR)
        if bl > 0.4:
            layer = cv2.GaussianBlur(layer, (0, 0), bl)
        a = layer[..., 3:4]
        sh = cv2.GaussianBlur(cv2.resize(a[..., 0], (W // 4, H // 4), interpolation=cv2.INTER_AREA), (0, 0), 8)
        sh = cv2.warpAffine(sh, np.float32([[1, 0, 0], [0, 1, 7]]), (W // 4, H // 4))
        canvas *= 1 - 0.6 * cv2.resize(sh, (W, H))[..., None]
        # мягкое свечение вокруг карточки цветом обложки
        glow = cv2.GaussianBlur(cv2.resize(layer[..., :3], (W // 4, H // 4)), (0, 0), 16)
        canvas += cv2.resize(glow, (W, H)) * (0.35 + 0.25 * lift)
        canvas = canvas * (1 - a) + layer[..., :3]

        tal = np.zeros((H, W, 1), np.float32)
        tcol = np.zeros((H, W, 3), np.float32)
        draw_text(tcol, tal, t, lift, i)
        g = cv2.GaussianBlur(cv2.resize(tcol, (W // 2, H // 2)), (0, 0), 5)
        canvas += cv2.resize(g, (W, H)) * (0.32 + 0.12 * lift)
        canvas = canvas * (1 - tal) + tcol

        canvas *= vign
        canvas *= min(1, t / 0.2) * (1 - ease_io((t - (dur - 0.7)) / 0.7))
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


if __name__ == "__main__":   # python -m server.engine.cover_coin cover.jpg track.mp3 out.mp4 0 15 "Надпись" "Название" "Артист"
    import sys
    a = sys.argv
    render(cover=a[1], audio=a[2], out=a[3], start=float(a[4]), dur=float(a[5]), font_path=None,
           fmt={"w": W, "h": H, "fps": FPS},
           fields={"caption": a[6] if len(a) > 6 else "", "title": a[7] if len(a) > 7 else "", "artist": a[8] if len(a) > 8 else ""},
           lines=[])
