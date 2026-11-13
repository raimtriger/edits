"""Реестр форматов и стилей анимации обложки — единое место, откуда берут данные и сервер, и аппка.

Аппка читает его через GET /api/cover/styles: что показывать, какие поля спрашивать, что уже готово, а что «скоро».
Сервер по нему проверяет заявку и выбирает, какой функцией рендерить.

КАК ДОБАВИТЬ НОВУЮ АНИМАЦИЮ
1. Написать функцию в своём модуле (например server/engine/cover_premiere_karaoke.py) с таким контрактом:

       def render(*, cover, audio, out, start, dur, font_path, fmt, fields, lines) -> None

   cover, audio, out — пути к файлам; start, dur — отрывок трека в секундах;
   font_path — шрифт артиста или None (тогда фирменный); fmt — {"id","w","h","fps"} из FORMATS;
   fields — уже очищенные поля стиля ({"caption": "ПРЕМЬЕРА", ...});
   lines — строки песни [{"start","end","text"}] в секундах от начала трека (для караоке-стилей), иначе пусто.
   Видео пишется в out: h264 + aac, размер fmt["w"] x fmt["h"].
2. Зарегистрировать функцию в RENDERERS (ниже), ключ — id стиля.
3. В STYLES у стиля поставить "ready" у форматов, которые функция умеет. Аппка и проверка заявок подхватят сами.
4. Превью для карточки: положить видео/гифку в previews/ рядом с index.html и вписать имя файла в "preview".
"""
from __future__ import annotations

# ───────────────────────────── форматы ─────────────────────────────
FORMATS = [
    {"id": "9x16", "title": "Reels · Shorts", "ratio": "9:16", "w": 1080, "h": 1920, "fps": 30,
     "hint": "Instagram Reels, YouTube Shorts, TikTok, VK Клипы"},
    {"id": "16x9", "title": "YouTube", "ratio": "16:9", "w": 1920, "h": 1080, "fps": 30,
     "hint": "Обычное видео и караоке на YouTube"},
    {"id": "1x1", "title": "Квадрат", "ratio": "1:1", "w": 1080, "h": 1080, "fps": 30,
     "hint": "Пост в ленту, Telegram, VK"},
]
FORMAT_BY_ID = {f["id"]: f for f in FORMATS}

# ───────────────────────────── поля ─────────────────────────────
# какие поля бывают у стилей; "upper" — набираем заглавными, "from_track" — подставить название трека, если пусто
FIELDS = {
    "caption": {"label": "Надпись", "default": "ПРЕМЬЕРА", "max": 24, "upper": True},
    "title": {"label": "Название трека", "default": "", "max": 40, "upper": False, "from_track": True},
    "artist": {"label": "Артист", "default": "", "max": 32, "upper": False},
    "date": {"label": "Дата релиза", "default": "", "placeholder": "18 НОЯБРЯ", "max": 16, "upper": True},
}

# ───────────────────────────── стили ─────────────────────────────
# status у формата: "ready" — рендерится, "soon" — заготовка, аппка показывает «скоро», сервер такую заявку не берёт.
# Названия и описания — рабочие, меняй свободно (аппка берёт их отсюда).
STYLES = [
    {"id": "premiere_classic", "title": "Классика", "tag": "Премьера",
     "desc": "Обложка влетает с 3D-поворотом, пульсирует под бит, надпись набирается по буквам.",
     "fields": ["caption"], "needs_lines": False, "preview": None,
     "formats": {"9x16": "ready", "16x9": "soon", "1x1": "soon"}},
    {"id": "premiere_karaoke", "title": "Караоке", "tag": "Премьера · YouTube",
     "desc": "Обложка и строки песни в такт: текущая строка подсвечивается. Для караоке-видео на YouTube.",
     "fields": ["caption", "artist", "title"], "needs_lines": True, "preview": None,
     "formats": {"16x9": "soon", "9x16": "soon", "1x1": "soon"}},
    {"id": "premiere_countdown", "title": "Отсчёт", "tag": "Премьера",
     "desc": "Обратный отсчёт до релиза: дата крупно, обложка проявляется к нулю.",
     "fields": ["caption", "date"], "needs_lines": False, "preview": None,
     "formats": {"9x16": "soon", "16x9": "soon", "1x1": "soon"}},
    {"id": "premiere_minimal", "title": "Минимал", "tag": "Премьера",
     "desc": "Спокойный медленный наезд на обложку, название и артист тонкой строкой.",
     "fields": ["title", "artist"], "needs_lines": False, "preview": None,
     "formats": {"16x9": "soon", "9x16": "soon", "1x1": "soon"}},
]
STYLE_BY_ID = {s["id"]: s for s in STYLES}

DEFAULT_STYLE, DEFAULT_FORMAT = "premiere_classic", "9x16"


# ───────────────────────────── рендер ─────────────────────────────
def _render_classic(*, cover, audio, out, start, dur, font_path, fmt, fields, lines):
    """Существующая анимация «Премьера · классика», только вертикаль 1080x1920."""
    from .cover_anim import render_cover_anim
    render_cover_anim(cover, audio, out, start, dur, title=fields.get("caption") or "ПРЕМЬЕРА", font_path=font_path)


# id стиля -> функция рендера по контракту из шапки файла. Новые анимации добавлять сюда.
RENDERERS = {
    "premiere_classic": _render_classic,
}


# ───────────────────────────── API для сервера ─────────────────────────────
def public_registry() -> dict:
    """То, что отдаём аппке."""
    return {"formats": FORMATS, "fields": FIELDS, "styles": STYLES, "default": {"style": DEFAULT_STYLE, "format": DEFAULT_FORMAT}}


def status_of(style_id: str, fmt_id: str) -> str | None:
    """'ready' / 'soon' / None (такого сочетания нет)."""
    s = STYLE_BY_ID.get(style_id)
    return s["formats"].get(fmt_id) if s and fmt_id in FORMAT_BY_ID else None


def check_request(style_id: str, fmt_id: str) -> str | None:
    """Текст ошибки для пользователя или None, если заявку можно брать в работу."""
    s = STYLE_BY_ID.get(style_id)
    if not s:
        return "неизвестный стиль анимации"
    if fmt_id not in FORMAT_BY_ID:
        return "неизвестный формат"
    st = s["formats"].get(fmt_id)
    if st != "ready" or style_id not in RENDERERS:
        return f"«{s['title']}» для {FORMAT_BY_ID[fmt_id]['ratio']} пока в разработке"
    return None


def clean_fields(style_id: str, raw: dict | None, track_title: str = "") -> dict:
    """Оставляет только поля, которые нужны стилю, обрезает по длине и приводит регистр."""
    out = {}
    raw = raw if isinstance(raw, dict) else {}
    for key in STYLE_BY_ID[style_id]["fields"]:
        spec = FIELDS[key]
        v = str(raw.get(key) or "").strip()[: spec["max"]]
        if not v and spec.get("from_track"):
            v = track_title.strip()[: spec["max"]]
        if not v:
            v = spec.get("default", "")
        out[key] = v.upper() if spec.get("upper") else v
    return out


def render_style(*, style: str, format: str, fields: dict, cover: str, audio: str, out: str,
                 start: float, dur: float, font_path: str | None = None, lines: list | None = None) -> None:
    """Вызывается из отдельного процесса (engine/job.py). Выбирает функцию по стилю."""
    err = check_request(style, format)
    if err:
        raise ValueError(err)
    RENDERERS[style](cover=cover, audio=audio, out=out, start=start, dur=dur, font_path=font_path,
                     fmt=FORMAT_BY_ID[format], fields=fields, lines=lines or [])
