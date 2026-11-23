"""Анимация обложки «Монета» — вертикаль 720x1280, до 15 с.

По разбору референса (story со «Слушать»):
  • 0–0,25 с: обложка на весь кадр, размытая, крупная; быстро садится в карточку;
  • карточка крутится «монеткой» вокруг вертикальной оси: ребром на ~0,6 с, обратной стороной (картинка зеркальная)
    до ~1,4 с, дальше замедляется и ложится в плоскость к ~2,4 с; второй переворот около середины ролика;
  • по контуру карточки бежит светящаяся комета (цвет берётся из обложки), на сильных ударах контур вспыхивает;
  • по обложке проходит блик-«молния», по кадру — горизонтальный анаморфный луч и мягкие световые пятна;
  • фон дышит: медленно пульсирует масштаб и яркость, на ударах — сильнее;
  • название собирается со «скрэмблом» (обрывки букв), потом артист; надпись сверху — тем же шрифтом, что и название.

Все надписи и карточка вписаны в безопасную рамку для Reels/TikTok (см. SAFE_*): сверху и снизу свободное место под
интерфейс, в правом нижнем углу — вырез под кнопки.
Удары в звуке толкают карточку пружинами (см. cover_glow.simulate). Звука нет — спокойное дыхание без толчков и вспышек.
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
SPIN_T0, SPIN_DUR = 0.25, 3.0        # первый переворот (в начале)
FIN_DUR = 2.6                        # финал: переворот и «залёт» карточки на зрителя в последние секунды

# Безопасная рамка (из шаблона 1080x1920, переведена в 720x1280): всё важное внутри неё.
SAFE_L, SAFE_R, SAFE_T, SAFE_B = 52, 669, 122, 1053
NOTCH_X, NOTCH_Y = 617, 860          # ниже NOTCH_Y правее NOTCH_X — вырез под кнопки (лайк/шер)
TEXT_W = 2 * min(W / 2 - SAFE_L, NOTCH_X - W / 2)     # ширина строк, которые могут попасть в зону выреза (центрированный текст)


def _accent(cov: np.ndarray) -> np.ndarray:
    """Цвет свечения из обложки: среднее самых насыщенных светлых пикселей (RGB 0..1). Нет цвета — тёплый розовый."""
    small = cv2.resize(cov, (64, 64), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV).reshape(-1, 3).astype(np.float32)
    ok = hsv[(hsv[:, 1] > 90) & (hsv[:, 2] > 110)]
    if len(ok) < 20:
        return np.float32([1.0, 0.45, 0.75])
    ang = ok[:, 0] * 2 * math.pi / 180
    hue = (math.atan2(np.sin(ang).mean(), np.cos(ang).mean()) % TAU) * 180 / math.pi / 2
    rgb = cv2.cvtColor(np.uint8([[[hue, 190, 255]]]), cv2.COLOR_HSV2RGB)[0, 0]
    return rgb.astype(np.float32) / 255


def _ink(ln: _Line) -> tuple[int, int]:
    """Где у строки реально начинаются и кончаются чернила по вертикали (в координатах картинки буквы)."""
    top, bot = 10 ** 6, 0
    for a, _gx in ln.items:
        rows = np.where(a[..., 0].max(1) > 0.3)[0]
        if len(rows):
            top, bot = min(top, int(rows[0])), max(bot, int(rows[-1]))
    return (0, 0) if top > bot else (top, bot + 1)


def _load_icon(path: str, h_px: int):
    """Иконка/знак с прозрачностью -> (rgb, alpha) float32, высота h_px. PNG/WebP напрямую, SVG через rsvg-convert или cairosvg."""
    import shutil
    import tempfile
    pth = str(path)
    if pth.lower().endswith(".svg"):
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
        if shutil.which("rsvg-convert"):
            subprocess.run(["rsvg-convert", "-h", str(h_px * 3), "-o", tmp, pth], check=True)
        else:
            import cairosvg  # noqa
            cairosvg.svg2png(url=pth, write_to=tmp, output_height=h_px * 3)
        pth = tmp
    im = cv2.imread(pth, cv2.IMREAD_UNCHANGED)
    if im is None:
        raise ValueError("не удалось прочитать иконку")
    if im.ndim == 2:
        im = cv2.cvtColor(im, cv2.COLOR_GRAY2BGRA)
    elif im.shape[2] == 3:
        im = np.dstack([im, np.full(im.shape[:2], 255, np.uint8)])
    ys, xs = np.where(im[..., 3] > 8)                       # обрезаем пустые поля
    if len(ys):
        im = im[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    k = h_px / im.shape[0]
    im = cv2.resize(im, (max(1, int(im.shape[1] * k)), h_px), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(im[..., :3], cv2.COLOR_BGR2RGB).astype(np.float32) / 255
    return rgb, im[..., 3:4].astype(np.float32) / 255


def render(*, cover, audio, out, start, dur, font_path, fmt, fields, lines, icon=None):
    dur = float(max(6.0, min(15.0, dur)))
    N = int(FPS * dur)
    rng = np.random.default_rng(5)

    an = analyze(audio, start, dur)
    # быстрый бит: карточка отвечает только на самые сильные удары, не чаще раза в 0,7 с
    keep: list[tuple[float, float]] = []
    for t_, s_ in sorted(an["hits"], key=lambda x: -x[1]):
        if all(abs(t_ - k[0]) >= 0.7 for k in keep):
            keep.append((t_, s_))
    an = dict(an, hits=sorted(keep))
    SC, ANG, YO = simulate(an, N)
    # удары мягче на ~60% и без резких фронтов
    k_ = cv2.getGaussianKernel(15, 3.5)[:, 0]
    SC, ANG, YO = (np.convolve(np.pad(a_ * 0.4, (7, 7), mode="edge"), k_, mode="valid") for a_ in (SC, ANG, YO))
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
    glow_col = (0.65 * accent + 0.35).astype(np.float32)

    # ---------- текст: размеры и раскладка внутри безопасной рамки ----------
    caption = (fields.get("caption") or "").strip()
    title = (fields.get("title") or "").strip()
    artist = (fields.get("artist") or "").strip()
    f_black = font_path or str(FONTS / "unbounded-black.ttf")

    def title_lines(text):
        one = _Line(text, f_black, 88, 2, TEXT_W)
        if one.size >= 62 or " " not in text:
            return [one]
        words = text.split()
        cut = min(range(1, len(words)), key=lambda k: abs(len(" ".join(words[:k])) - len(" ".join(words[k:]))))
        a, b = " ".join(words[:cut]), " ".join(words[cut:])
        size = min(_Line(a, f_black, 88, 2, TEXT_W).size, _Line(b, f_black, 88, 2, TEXT_W).size)
        if size <= one.size:
            return [one]
        return [_Line(a, f_black, size, 2, TEXT_W, False), _Line(b, f_black, size, 2, TEXT_W, False)]

    cap_ln = _Line(caption, f_black, 26, 7, SAFE_R - SAFE_L) if caption else None
    t_lines = title_lines(title) if title else []
    art_ln = _Line(artist, f_black, 30, 6, TEXT_W) if artist else None

    GAP_CAP, GAP_CARD, GAP_ART = 66, 46, 34         # запас под «подпрыгивание», крен и перспективу карточки
    ICON_H, GAP_ICON = 92, 44
    icon_px = _load_icon(icon, ICON_H) if icon else None

    def block_h(card):
        h = card
        if icon_px is not None:
            h += ICON_H + GAP_ICON
        if cap_ln:
            c0, c1 = _ink(cap_ln); h += (c1 - c0) + GAP_CAP
        if t_lines:
            h += GAP_CARD
            for k, ln in enumerate(t_lines):
                a0, a1 = _ink(ln); h += (a1 - a0) + (int(ln.size * 0.22) if k else 0)
        if art_ln:
            a0, a1 = _ink(art_ln); h += GAP_ART + (a1 - a0)
        return h

    avail = SAFE_B - SAFE_T
    CARD = 548
    while block_h(CARD) > avail and CARD > 420:
        CARD -= 8
    CARD = min(CARD, int((SAFE_R - SAFE_L) * 0.93))   # с запасом на «подпрыгивание» масштаба
    total = block_h(CARD)
    y = SAFE_T + (avail - total) / 2.0
    items_y: list[tuple[_Line, float, float]] = []     # (строка, y верхнего края чернил, время старта)
    icon_y = y
    if icon_px is not None:
        y += ICON_H + GAP_ICON
    if cap_ln:
        c0, c1 = _ink(cap_ln); items_y.append((cap_ln, y, 3.6)); y += (c1 - c0) + GAP_CAP
    card_top = y
    card_cy0 = int(y + CARD / 2)
    y += CARD
    if t_lines:
        y += GAP_CARD
        for k, ln in enumerate(t_lines):
            a0, a1 = _ink(ln)
            if k:
                y += int(ln.size * 0.22)
            items_y.append((ln, y, 2.7 + k * 0.25)); y += a1 - a0
    if art_ln:
        a0, a1 = _ink(art_ln); y += GAP_ART; items_y.append((art_ln, y, 3.4)); y += a1 - a0

    # ---------- карточка со стеклянным краем ----------
    card_px = cv2.resize(cov, (CARD, CARD), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    rad = int(CARD * 0.06)
    mask = _rounded_mask(CARD, rad)
    e = Image.new("L", (CARD * 4, CARD * 4), 0)
    ImageDraw.Draw(e).rounded_rectangle([0, 0, CARD * 4 - 1, CARD * 4 - 1], radius=rad * 4, outline=255, width=10)
    edge = np.asarray(e.resize((CARD, CARD), Image.LANCZOS), np.float32)[..., None] / 255
    inner = cv2.GaussianBlur(edge, (0, 0), 9).reshape(CARD, CARD, 1)
    grad = np.linspace(1.0, 0.45, CARD, dtype=np.float32)[:, None, None]
    pad = 40                                          # поля вокруг карточки под свечение по контуру
    CP = CARD + pad * 2
    PAD0 = np.zeros((CP, CP, 4), np.float32)
    PAD0[pad:pad + CARD, pad:pad + CARD] = np.concatenate([card_px * mask, mask], 2)
    # тонкая рамка СНАРУЖИ карточки (на картинку ничего не заезжает); по ней бежит свет
    FW = 3
    outer = Image.new("L", (CP * 4, CP * 4), 0)
    ImageDraw.Draw(outer).rounded_rectangle([(pad - FW - 1) * 4, (pad - FW - 1) * 4, (pad + CARD + FW + 1) * 4 - 1, (pad + CARD + FW + 1) * 4 - 1], radius=(rad + FW + 1) * 4, fill=255)
    FRAME_A = np.asarray(outer.resize((CP, CP), Image.LANCZOS), np.float32) / 255
    inner_m = Image.new("L", (CP * 4, CP * 4), 0)
    ImageDraw.Draw(inner_m).rounded_rectangle([(pad - 1) * 4, (pad - 1) * 4, (pad + CARD + 1) * 4 - 1, (pad + CARD + 1) * 4 - 1], radius=(rad + 1) * 4, fill=255)
    FRAME_A = np.clip(FRAME_A - np.asarray(inner_m.resize((CP, CP), Image.LANCZOS), np.float32) / 255, 0, 1)
    src_pts = np.float32([[0, 0], [CP, 0], [CP, CP], [0, CP]])
    MASK_FULL = PAD0[..., 3:4]

    # контур: тонкая яркая линия + мягкое гало; угол точки вокруг центра нужен для бегущей кометы
    rr = Image.new("L", (CP * 2, CP * 2), 0)
    ImageDraw.Draw(rr).rounded_rectangle([pad * 2, pad * 2, (pad + CARD) * 2 - 1, (pad + CARD) * 2 - 1], radius=rad * 2, outline=255, width=7)
    ring_line = np.asarray(rr.resize((CP, CP), Image.LANCZOS), np.float32)
    RING = np.clip(cv2.GaussianBlur(ring_line, (0, 0), 6) * 0.9, 0, 1.0) * (1 - PAD0[..., 3])      # мягкое гало только снаружи
    yy_, xx_ = np.mgrid[0:CP, 0:CP].astype(np.float32)
    THETA = (np.arctan2(yy_ - CP / 2, xx_ - CP / 2) % TAU).astype(np.float32)
    S_DIAG = (((xx_ - pad) / CARD) + 0.6 * ((yy_ - pad) / CARD)) / 1.6          # координата для блика-«молнии»
    del yy_, xx_

    # ---------- фон: тёмная размытая копия обложки, цвет сохраняем ----------
    BW, BH = int(W * 1.25), int(H * 1.25)
    bgs = max(BW, BH)
    bg = cv2.resize(cov, (bgs, bgs), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    bg = bg[(bgs - BH) // 2:(bgs - BH) // 2 + BH, (bgs - BW) // 2:(bgs - BW) // 2 + BW]
    bg_sharp = cv2.GaussianBlur(bg, (0, 0), 10)
    bg = cv2.GaussianBlur(bg, (0, 0), 50)
    grey = bg.mean(2, keepdims=True)
    bg = grey + (bg - grey) * 0.9
    bg = np.clip((bg - 0.45) * 1.2 + 0.45, 0, 1) * 0.5
    bg_sharp = np.clip((bg_sharp - 0.45) * 1.2 + 0.45, 0, 1) * 0.5
    nr = np.random.default_rng(11)
    NF = [cv2.GaussianBlur(nr.random((H // 16, W // 16)).astype(np.float32), (0, 0), 3) for _ in range(9)]
    NF = [(f - f.min()) / (f.max() - f.min() + 1e-6) for f in NF]
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    vign = np.clip(1 - 0.7 * (np.sqrt(((xx - W / 2) / (W * 0.8)) ** 2 + ((yy - H / 2) / (H * 0.65)) ** 2)) ** 2.2, 0.2, 1)[..., None]
    del yy, xx

    # когда что происходит
    fin_t = dur - FIN_DUR - 0.2
    mid = [(t, s_) for t, s_ in an["hits"] if dur * 0.38 <= t <= dur * 0.55]
    mid_t = max(mid, key=lambda x: x[1])[0] if (mid and not calm) else dur * 0.46
    flash_t = [SPIN_T0 + SPIN_DUR + 0.1, mid_t, fin_t]        # блики редко: после посадки, раз в середине и перед финалом
    FLASH_D = 1.9

    def blob(cx, cy, sx, sy):
        """Мягкое световое пятно без краёв: гауссиана на четверти размера, растянутая до кадра."""
        qh, qw = H // 4, W // 4
        gx = np.exp(-(((np.arange(qw, dtype=np.float32) - cx / 4) / (sx / 4)) ** 2))
        gy = np.exp(-(((np.arange(qh, dtype=np.float32) - cy / 4) / (sy / 4)) ** 2))
        return cv2.resize(np.outer(gy, gx), (W, H), interpolation=cv2.INTER_LINEAR)[..., None]

    # ---------- текст ----------
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

    def rng_hash(a, b, c):
        return (a * 73856093) ^ (b * 19349663) ^ (c * 83492791)

    white = np.float32([1, 1, 1])

    def draw_text(tcol, tal, t, lift, sx=0.0, sy=0.0):
        for li, (ln, ytop, t0) in enumerate(items_y):
            ink_top, _ = _ink(ln)
            yy = ytop - ink_top + sy * (1 + 0.25 * li)
            x0 = W / 2 - ln.total / 2 + sx * (1 - 0.15 * li)
            is_cap = ln is cap_ln
            for i, (a, gx) in enumerate(ln.items):
                r = t0 + i * (0.05 if is_cap else 0.055)         # когда буква «встаёт» на место
                if is_cap:
                    k = ease_out((t - r) / 0.5)
                    if k > 0:
                        sg = (1 - k) * 6
                        a2 = cv2.GaussianBlur(a, (0, 0), sg)[..., None] if sg > 0.4 else a
                        blit(tcol, tal, a2, x0 + gx, yy, white * 0.92, k)
                    continue
                if t < r - 0.38:
                    continue
                if t < r:                                       # скрэмбл: чужая буква, размытая, мерцает
                    pick = ln.items[int(rng_hash(li, i, int(t * 14)) % ln.n)][0]
                    k = 0.25 + 0.5 * (1 - (r - t) / 0.38)
                    blit(tcol, tal, cv2.GaussianBlur(pick, (0, 0), 3)[..., None], x0 + gx, yy, white * 0.7, k * 0.8)
                    continue
                k = ease_out((t - r) / 0.3)
                ph = ((t * 0.36 - li * 0.23) % 1.0) * 1.5 - 0.25
                dip = math.exp(-(((i / ln.n) - ph) / 0.11) ** 2)
                shade = min(1.0, (1.0 - 0.42 * dip) * (1 + 0.1 * lift))
                a2 = a if k >= 1 else cv2.GaussianBlur(a, (0, 0), (1 - k) * 4)[..., None]
                blit(tcol, tal, a2, x0 + gx, yy + (1 - k) * 10 + 1.6 * math.sin(t * 1.5 + i * 0.45 + li), white * shade, 0.4 + 0.6 * k)

    def spin_angle(t):
        a = 0.0
        if t >= SPIN_T0:
            x = min((t - SPIN_T0) / SPIN_DUR, 1.0)
            a += 360.0 * (1 - (1 - x) ** 2)
        if t >= fin_t:
            a += 360.0 * ease_io((t - fin_t) / FIN_DUR)
        return a

    debug_safe = bool(os.environ.get("COIN_SAFE_DEBUG"))
    safe_poly = np.array([[SAFE_L, SAFE_T], [SAFE_R, SAFE_T], [SAFE_R, NOTCH_Y], [NOTCH_X, NOTCH_Y], [NOTCH_X, SAFE_B], [SAFE_L, SAFE_B]], np.int32)

    def frame(i):
        t = i / FPS
        if calm:
            sc_, ang_, yo_ = 0.012 * math.sin(TAU * t / 5.0 + 1.0), 1.2 * math.sin(TAU * t / 7.0), 4 * math.sin(TAU * t / 6.0)
        else:
            sc_, ang_, yo_ = SC[i], ANG[i], YO[i]
        lift = max(0.0, sc_) / 0.05
        breath = math.sin(TAU * t / 5.0)                      # общий «вдох» — и у фона, и у карточки

        # фон дышит: масштаб и яркость пульсируют, на ударах сильнее
        z = 1.0 + 0.03 * t / dur + 0.10 * breath + 0.6 * sc_
        M = np.float32([[z, 0, W / 2 - BW / 2 * z + 4 * math.sin(t * 0.3)], [0, z, H / 2 - BH / 2 * z + 9 * math.cos(t * 0.23)]])
        sway_x, sway_y = 5 * math.sin(t * 0.9) + 16 * math.sin(TAU * t / 6.8), 4 * math.cos(t * 0.7) + 7 * math.sin(TAU * t / 5.3 + 0.8)     # общее лёгкое покачивание: фон, карточка и надписи плывут вместе
        canvas = cv2.warpAffine(bg, M, (W, H), borderMode=cv2.BORDER_REFLECT)
        # фон хаотично проявляется: пятна резкости плывут по кадру, но полностью не проявляются (макс. ~45%)
        ph_ = t / 2.4
        k0 = int(ph_) % len(NF); fr_ = ph_ - int(ph_); fr_ = fr_ * fr_ * (3 - 2 * fr_)
        nz = NF[k0] * (1 - fr_) + NF[(k0 + 1) % len(NF)] * fr_
        mrev = np.clip((cv2.resize(nz, (W, H), interpolation=cv2.INTER_CUBIC) - 0.45) * 2.2, 0, 1) * 0.45 * min(1.0, t / 1.5)
        sharp = cv2.warpAffine(bg_sharp, M, (W, H), borderMode=cv2.BORDER_REFLECT)
        canvas = canvas * (1 - mrev[..., None]) + sharp * mrev[..., None]
        canvas *= 1 + 0.34 * breath + 1.0 * max(sc_, 0)
        # светящееся «дыхание» за карточкой: пульсирует с тем же вдохом
        canvas += blob(W / 2, card_cy0, 330, 430) * accent * (0.16 + 0.16 * breath)

        # вспышки: короткий всплеск цвета обложки; пятно без краёв, меняет сторону от вспышки к вспышке
        fl, fj = 0.0, 0
        for j, ft in enumerate(flash_t):
            d = t - ft
            if 0 <= d < 1.5:
                v = (1 - math.exp(-d / 0.15)) * math.exp(-d / 0.45) * (0.6 if j == 1 else 1.0)
                if v > fl:
                    fl, fj = v, j
        if fl > 0.01:
            side = -1 if fj % 2 == 0 else 1
            canvas += blob(W / 2 + side * 220, H * (0.34 if fj % 2 == 0 else 0.62), 330, 420) * accent * (0.4 * fl)

        # карточка: залёт -> дыхание + удары; контур светится, по нему бежит комета, по картинке проходит блик
        u = ease_out(t / 0.9)
        zf = 1.0 + 1.15 * min(1.0, max(0.0, (t - fin_t) / FIN_DUR)) ** 2.3          # финал: наезжает на зрителя
        size = CP * lerp(1.95, 1.0, u) * (1 + sc_) * (1 + 0.012 * breath) * zf
        cy = lerp(H * 0.52, card_cy0, u) + yo_
        fl_on = ease_io((t - 1.5) / 1.5) * (1 - ease_io((t - fin_t) / 1.0))      # парение включается после посадки, в финале не мешает
        ry = spin_angle(t) + ang_ + 5.5 * math.sin(TAU * t / 6.3) * fl_on
        rx = 5.5 * math.sin(TAU * t / 7.7 + 1.0) * fl_on + lerp(10, 0, u)
        rz = lerp(-12, 0, u) + 0.3 * ang_ + 1.2 * math.sin(TAU * t / 8.9) * fl_on

        gain = ease_out((t - 0.5) / 1.2)
        head = (t * TAU / 5.5) % TAU
        d1 = (head - THETA) % TAU
        d2 = (head + math.pi - THETA) % TAU
        lead1 = np.clip(d1 / 0.35, 0, 1) ** 2 * (3 - 2 * np.clip(d1 / 0.35, 0, 1))
        lead2 = np.clip(d2 / 0.35, 0, 1) ** 2 * (3 - 2 * np.clip(d2 / 0.35, 0, 1))
        comet = np.exp(-d1 / 1.25) * lead1 * 0.85 + np.exp(-d2 / 1.6) * lead2 * 0.45
        glow_i = (0.12 + 0.70 * comet) * (1 + 0.7 * lift + 0.9 * fl) * gain
        layer_src = PAD0.copy()
        rgb, al = layer_src[..., :3], layer_src[..., 3:4]
        # тонкая рамка: тусклый металл, по которому бежит свет (комета) и играет блик на вспышках
        shine = 0.30 + 1.15 * comet + 0.9 * fl + 0.5 * lift
        mcol = np.float32([1.0, 0.95, 0.88]) * (0.45 + 0.55 * glow_col) * shine[..., None]
        fa = FRAME_A[..., None]
        rgb[:] = rgb * (1 - fa) + np.clip(mcol, 0, 1.6) * fa
        al[:] = np.maximum(al, fa)
        for ft in flash_t:                                      # блик-«молния» по обложке
            d = t - ft
            if 0 <= d < FLASH_D:
                pos = -0.25 + 1.5 * ease_io(d / FLASH_D)
                band = np.exp(-((S_DIAG - pos) / 0.07) ** 2) * 0.30 + np.exp(-((S_DIAG - pos) / 0.22) ** 2) * 0.08
                rgb += (band[..., None] * MASK_FULL) * np.float32([1.0, 0.97, 0.92])
        layer_src[..., 3:4] = al

        Mp = cv2.getPerspectiveTransform(src_pts, _project(W / 2 + sway_x, cy + sway_y, size, rx, ry, rz, f=1700.0))
        layer = cv2.warpPerspective(layer_src, Mp, (W, H), flags=cv2.INTER_LINEAR, borderValue=0)
        bl = 14 * (1 - ease_out(t / 0.7)) + 2.5 * abs(math.sin(math.radians(ry))) * (1 - ease_out((t - 0.3) / 2.2)) * (t < SPIN_T0 + SPIN_DUR)
        if bl > 0.4:
            layer = cv2.GaussianBlur(layer, (0, 0), bl)
        a = layer[..., 3:4]
        sh = cv2.GaussianBlur(cv2.resize(a[..., 0], (W // 4, H // 4), interpolation=cv2.INTER_AREA), (0, 0), 8)
        sh = cv2.warpAffine(sh, np.float32([[1, 0, 0], [0, 1, 7]]), (W // 4, H // 4))
        canvas *= 1 - 0.5 * cv2.resize(sh, (W, H))[..., None]
        glow = cv2.GaussianBlur(cv2.resize(layer[..., :3], (W // 4, H // 4)), (0, 0), 16)
        canvas += cv2.resize(glow, (W, H)) * 0.06
        canvas = canvas * (1 - a) + layer[..., :3]

        # анаморфный луч через центр карточки на вспышке
        if fl > 0.02 and fj != 1:
            gy = np.exp(-(((np.arange(H, dtype=np.float32) - cy) / 5.0) ** 2))
            gx = np.exp(-(((np.arange(W, dtype=np.float32) - W / 2) / 300.0) ** 4))
            canvas += np.outer(gy, gx)[..., None] * glow_col * (0.7 * fl)

        if icon_px is not None:                                 # иконка «дышит»: масштаб и свечение пульсируют
            irgb, ia = icon_px
            ik = ease_out((t - 0.9) / 0.8)
            if ik > 0:
                br = math.sin(TAU * t / 3.4)
                sc_i = (0.6 + 0.4 * ik) * (1 + 0.07 * br)
                iw_, ih_ = max(2, int(irgb.shape[1] * sc_i)), max(2, int(irgb.shape[0] * sc_i))
                r_rgb = cv2.resize(irgb, (iw_, ih_), interpolation=cv2.INTER_AREA)
                r_a = cv2.resize(ia[..., 0], (iw_, ih_), interpolation=cv2.INTER_AREA)[..., None] * ik * (1 - ease_io((t - fin_t) / 0.9))
                cx_i, cy_i = W / 2 + sway_x * 0.8, icon_y + ICON_H / 2 + sway_y * 0.8
                x0_i, y0_i = int(cx_i - iw_ / 2), int(cy_i - ih_ / 2)
                pad_g = 30                                     # мягкое свечение вокруг иконки
                gl = cv2.GaussianBlur(np.pad(r_a, ((pad_g, pad_g), (pad_g, pad_g), (0, 0)))[..., 0], (0, 0), 14)[..., None]
                for arr, col, k_, xx0, yy0 in ((gl, r_rgb.reshape(-1, 3).mean(0), 0.30 + 0.20 * br, x0_i - pad_g, y0_i - pad_g), (r_a, None, 1.0, x0_i, y0_i)):
                    h_, w_ = arr.shape[:2]
                    xa, ya, xb, yb = max(xx0, 0), max(yy0, 0), min(xx0 + w_, W), min(yy0 + h_, H)
                    if xb <= xa or yb <= ya:
                        continue
                    a_ = arr[ya - yy0:yb - yy0, xa - xx0:xb - xx0]
                    reg = canvas[ya:yb, xa:xb]
                    if col is None:
                        reg[:] = reg * (1 - a_) + r_rgb[ya - yy0:yb - yy0, xa - xx0:xb - xx0] * a_
                    else:
                        reg += a_ * col * k_

        tal = np.zeros((H, W, 1), np.float32)
        tcol = np.zeros((H, W, 3), np.float32)
        draw_text(tcol, tal, t, lift, sway_x * 0.8, sway_y * 0.8)
        g = cv2.GaussianBlur(cv2.resize(tcol, (W // 2, H // 2)), (0, 0), 5)
        canvas += cv2.resize(g, (W, H)) * (0.32 + 0.12 * lift)
        tf = 1 - ease_io((t - fin_t) / 0.9)
        canvas = canvas * (1 - tal * tf) + tcol * tf

        canvas *= vign
        canvas *= (0.4 + 0.6 * min(1, t / 0.2)) * (1 - ease_io((t - (dur - 0.7)) / 0.7))
        canvas += rng.normal(0, 0.012, (H, W, 1)).astype(np.float32)
        out_img = (np.clip(canvas, 0, 1) * 255).astype(np.uint8)
        if debug_safe:
            cv2.polylines(out_img, [safe_poly], True, (255, 230, 170), 2)
        return out_img

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


if __name__ == "__main__":
    # python -m server.engine.cover_coin cover.jpg out.mp4 --title "Название" [--artist ..] [--caption ПРЕМЬЕРА] [--icon mark.svg] [--font f.ttf] [--audio track.mp3 --start 0] [--dur 15]
    import argparse
    import tempfile
    ap = argparse.ArgumentParser()
    ap.add_argument("cover"); ap.add_argument("out")
    ap.add_argument("--audio"); ap.add_argument("--start", type=float, default=0); ap.add_argument("--dur", type=float, default=15)
    ap.add_argument("--caption", default=""); ap.add_argument("--title", default=""); ap.add_argument("--artist", default="")
    ap.add_argument("--icon"); ap.add_argument("--font")
    a = ap.parse_args()
    audio = a.audio
    if not audio:                                     # без звука — тишина: спокойная анимация
        audio = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", str(a.dur + 1), audio], check=True)
    render(cover=a.cover, audio=audio, out=a.out, start=a.start, dur=a.dur, font_path=a.font, fmt={"w": W, "h": H, "fps": FPS},
           fields={"caption": a.caption, "title": a.title, "artist": a.artist}, lines=[], icon=a.icon)
