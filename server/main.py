"""LUDUS EDITS — сервер мини-аппки.

Запуск: uvicorn server.main:app --host 0.0.0.0 --port 8444 --ssl-keyfile ... --ssl-certfile ...
Настройки — в /opt/edits/.env (см. deploy/env.example).
"""
from __future__ import annotations

import json
import logging
import tempfile
import os
import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .engine import align, render
from .tg import Bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("edits")
logging.getLogger("httpx").setLevel(logging.WARNING)  # не писать токен бота в лог

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("EDITS_DATA", ROOT / "data"))
FOOTAGE = Path(os.environ.get("EDITS_FOOTAGE", ROOT / "footage"))
WEB = Path(os.environ.get("EDITS_WEB", ROOT))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
OWNER_IDS = {int(x) for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",") if x}
FREE_EDITS = int(os.environ.get("FREE_EDITS", "3"))
DEV = os.environ.get("EDITS_DEV") == "1"            # локальные тесты без Telegram
FAKE_ASR = os.environ.get("EDITS_FAKE_ASR") == "1"  # тесты без модели распознавания
MAX_UPLOAD = 80 * 1024 * 1024
MAX_TRACK_SEC = 8 * 60

FONTS_DIR = ROOT / "fonts"
# ключ: (семейство в файле, множитель размера, подпись)
FONTS = {"inter": ("LE Inter", 1.0, "Inter Light"), "unbounded": ("LE Unbounded", 0.78, "Unbounded"),
         "cormorant": ("LE Cormorant", 1.22, "Cormorant"), "oswald": ("LE Oswald", 1.08, "Oswald"),
         "caveat": ("LE Caveat", 1.3, "Caveat"), "tenor": ("LE Tenor", 0.95, "Tenor Sans")}
FONT_EXT = {".ttf", ".otf"}
MAX_FONT = 8 * 1024 * 1024
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
MAX_COVER = 15 * 1024 * 1024
for d in (DATA / "tracks", DATA / "edits", DATA / "covers", DATA / "fonts"):
    d.mkdir(parents=True, exist_ok=True)
FOOTAGE.mkdir(parents=True, exist_ok=True)

FOOTAGE_NAMES = {"night": "Ночной город", "love": "Девушки и пары", "nature": "Природа",
                 "bokeh": "Боке и огни", "cinema": "Кино · чистые права", "stock": "Стоки",
                 "mix": "Микс (всё)"}

# ---------------- база ----------------
db = sqlite3.connect(DATA / "edits.db", check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.Lock()
with lock:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, tg_id INTEGER UNIQUE, name TEXT,
        is_owner INTEGER DEFAULT 0, free_left INTEGER, created REAL);
    CREATE TABLE IF NOT EXISTS tracks(id TEXT PRIMARY KEY, user_id INTEGER, title TEXT, audio TEXT, lyrics TEXT,
        lines TEXT, frag_start REAL, frag_end REAL, duration REAL, status TEXT, error TEXT, created REAL);
    CREATE TABLE IF NOT EXISTS fonts(id TEXT PRIMARY KEY, user_id INTEGER, family TEXT, path TEXT, created REAL);
    CREATE TABLE IF NOT EXISTS edits(id TEXT PRIMARY KEY, user_id INTEGER, track_id TEXT, params TEXT,
        status TEXT, video TEXT, error TEXT, created REAL);
    """)
    cols = [r[1] for r in db.execute("PRAGMA table_info(tracks)")]
    if "cover" not in cols:
        db.execute("ALTER TABLE tracks ADD COLUMN cover TEXT")  # обложка трека
    if "instrumental" not in cols:
        db.execute("ALTER TABLE tracks ADD COLUMN instrumental INTEGER DEFAULT 0")  # композиция без слов
    db.commit()


def q(sql: str, args=(), one=False):
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def upsert_user(tg: dict) -> sqlite3.Row:
    name = " ".join(x for x in (tg.get("first_name"), tg.get("last_name")) if x) or tg.get("username") or "Артист"
    u = q("SELECT * FROM users WHERE tg_id=?", (tg["id"],), one=True)
    if u:
        return u
    first = q("SELECT COUNT(*) c FROM users", one=True)["c"] == 0
    owner = 1 if (tg["id"] in OWNER_IDS or (not OWNER_IDS and first)) else 0
    q("INSERT INTO users(tg_id,name,is_owner,free_left,created) VALUES(?,?,?,?,?)",
      (tg["id"], name, owner, FREE_EDITS, time.time()))
    return q("SELECT * FROM users WHERE tg_id=?", (tg["id"],), one=True)


# ---------------- Telegram ----------------
# метка версии в адресе аппки: после обновления Telegram обязан загрузить свежий index.html, а не копию из кэша
BUILD = time.strftime("%m%d-%H%M", time.localtime((WEB / "index.html").stat().st_mtime)) if (WEB / "index.html").exists() else "dev"
APP_URL = (WEBAPP_URL + ("&" if "?" in WEBAPP_URL else "?") + "v=" + BUILD) if WEBAPP_URL else ""
bot = Bot(BOT_TOKEN, APP_URL) if BOT_TOKEN else None


def _on_start(tg_user: dict):
    u = upsert_user(tg_user)
    owner = " У тебя доступ «Основатель» — всё бесплатно." if u["is_owner"] else ""
    bot.send_message(tg_user["id"], "Привет! Здесь эдиты на твои треки: грузишь трек — получаешь готовые видео." + owner,
                     with_app_button=True)


if bot:
    bot.on_start = _on_start


def current_user(x_init_data: str = Header(default=""), x_dev_user: str = Header(default="")) -> sqlite3.Row:
    if DEV and x_dev_user:
        return upsert_user({"id": int(x_dev_user), "first_name": "Dev"})
    if not bot:
        raise HTTPException(401, "бот не настроен")
    tg = bot.verify_init_data(x_init_data)
    if not tg:
        raise HTTPException(401, "открой аппку из Telegram")
    return upsert_user(tg)


# ---------------- API ----------------
app = FastAPI(title="LUDUS EDITS")


def track_json(t: sqlite3.Row) -> dict:
    return {"id": t["id"], "title": t["title"], "status": t["status"], "error": t["error"],
            "duration": t["duration"], "frag_start": t["frag_start"], "frag_end": t["frag_end"],
            "lines": json.loads(t["lines"]) if t["lines"] else [],
            "instrumental": bool(t["instrumental"]),
            "cover_url": f"api/tracks/{t['id']}/cover?v={int(Path(t['cover']).stat().st_mtime)}"
            if t["cover"] and Path(t["cover"]).exists() else None}


def edit_json(e: sqlite3.Row) -> dict:
    return {"id": e["id"], "track_id": e["track_id"], "status": e["status"], "error": e["error"],
            "params": json.loads(e["params"]), "url": f"media/{e['id']}.mp4" if e["status"] == "done" else None,
            "created": e["created"]}


@app.get("/api/me")
def me(u=Depends(current_user)):
    return {"name": u["name"], "owner": bool(u["is_owner"]), "free_left": u["free_left"]}


@app.get("/api/footage")
def footage_list(u=Depends(current_user)):
    out = []
    for key, name in FOOTAGE_NAMES.items():
        n = len(render.list_footage(FOOTAGE / key)) if key != "mix" else len(render.list_footage(FOOTAGE))
        if n:
            out.append({"key": key, "name": name, "count": n})
    return out


def user_font(u_id: int, key: str):
    if not key.startswith("u:"):
        return None
    return q("SELECT * FROM fonts WHERE id=? AND user_id=?", (key[2:], u_id), one=True)


@app.get("/api/fonts")
def fonts(u=Depends(current_user)):
    out = [{"key": k, "name": v[2], "url": f"fonts/{k}.ttf", "own": False} for k, v in FONTS.items()]
    out += [{"key": "u:" + f["id"], "name": f["family"], "url": f"api/fonts/{f['id']}/file", "own": True}
            for f in q("SELECT * FROM fonts WHERE user_id=? ORDER BY created", (u["id"],))]
    return out


@app.post("/api/fonts")
async def upload_font(font: UploadFile = File(...), u=Depends(current_user)):
    ext = (Path(font.filename or "f.ttf").suffix or "").lower()
    if ext not in FONT_EXT:
        raise HTTPException(400, "нужен файл .ttf или .otf")
    data = await font.read(MAX_FONT + 1)
    if len(data) > MAX_FONT:
        raise HTTPException(413, "шрифт больше 8 МБ")
    try:
        import io
        from fontTools.ttLib import TTFont
        tt = TTFont(io.BytesIO(data))
        nm = tt["name"]
        family = (nm.getDebugName(16) or nm.getDebugName(1) or "").strip()
        has_cyr = any(0x0410 <= cp <= 0x044F for cp in tt.getBestCmap() or {})
    except Exception:
        raise HTTPException(400, "файл не похож на шрифт")
    if not family:
        raise HTTPException(400, "в шрифте нет названия")
    old = q("SELECT * FROM fonts WHERE user_id=? AND family=?", (u["id"], family), one=True)
    fid = old["id"] if old else uuid.uuid4().hex[:12]
    path = DATA / "fonts" / f"{fid}{ext}"
    path.write_bytes(data)
    if not old:
        q("INSERT INTO fonts(id,user_id,family,path,created) VALUES(?,?,?,?,?)", (fid, u["id"], family, str(path), time.time()))
    return {"key": "u:" + fid, "name": family, "url": f"api/fonts/{fid}/file", "own": True, "cyrillic": has_cyr}


@app.delete("/api/fonts/{fid}")
def delete_font(fid: str, u=Depends(current_user)):
    f = q("SELECT * FROM fonts WHERE id=? AND user_id=?", (fid, u["id"]), one=True)
    if not f:
        raise HTTPException(404)
    Path(f["path"]).unlink(missing_ok=True)
    q("DELETE FROM fonts WHERE id=?", (fid,))
    return {"ok": True}


@app.get("/api/fonts/{fid}/file")
def font_file(fid: str):
    f = q("SELECT path FROM fonts WHERE id=?", (fid,), one=True)
    if not f or not Path(f["path"]).exists():
        raise HTTPException(404)
    return FileResponse(f["path"], media_type="font/ttf", headers={"Cache-Control": "max-age=604800"})


@app.get("/fonts/{key}.ttf")
def bundled_font(key: str):
    if key not in FONTS:
        raise HTTPException(404)
    return FileResponse(FONTS_DIR / f"{key}.ttf", media_type="font/ttf", headers={"Cache-Control": "max-age=604800"})


@app.get("/api/tracks")
def tracks(u=Depends(current_user)):
    return [track_json(t) for t in q("SELECT * FROM tracks WHERE user_id=? ORDER BY created DESC", (u["id"],))]


async def save_cover(tid: str, cover: UploadFile) -> str:
    ext = (Path(cover.filename or "c.jpg").suffix or ".jpg").lower()
    if ext not in IMG_EXT and not (cover.content_type or "").startswith("image/"):
        raise HTTPException(400, "обложка должна быть картинкой")
    if ext not in IMG_EXT:
        ext = ".jpg"
    for old in (DATA / "covers").glob(f"{tid}.*"):
        old.unlink(missing_ok=True)
    path = DATA / "covers" / f"{tid}{ext}"
    data = await cover.read(MAX_COVER + 1)
    if len(data) > MAX_COVER:
        raise HTTPException(413, "обложка больше 15 МБ")
    path.write_bytes(data)
    return str(path)


@app.post("/api/tracks")
async def upload_track(audio: UploadFile = File(...), title: str = Form(""), lyrics: str = Form(""),
                       instrumental: bool = Form(False),
                       cover: UploadFile | None = File(None), u=Depends(current_user)):
    tid = uuid.uuid4().hex[:12]
    ext = (Path(audio.filename or "a.mp3").suffix or ".mp3").lower()[:6]
    path = DATA / "tracks" / f"{tid}{ext}"
    size = 0
    with open(path, "wb") as f:
        while chunk := await audio.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                f.close()
                path.unlink(missing_ok=True)
                raise HTTPException(413, "файл больше 80 МБ")
            f.write(chunk)
    try:
        dur = render.probe_duration(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "не получилось прочитать аудио — пришли mp3, wav или m4a")
    if dur > MAX_TRACK_SEC:
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"трек длиннее {MAX_TRACK_SEC // 60} минут — загрузи сам трек, не сет")
    title = title.strip() or Path(audio.filename or "Трек").stem
    q("INSERT INTO tracks(id,user_id,title,audio,lyrics,status,duration,created) VALUES(?,?,?,?,?,?,?,?)",
      (tid, u["id"], title[:80], str(path), lyrics.strip(), "queued", dur, time.time()))
    if instrumental:
        q("UPDATE tracks SET instrumental=1, lyrics='' WHERE id=?", (tid,))
    if cover is not None and cover.filename:
        q("UPDATE tracks SET cover=? WHERE id=?", (await save_cover(tid, cover), tid))
    wake.set()
    return track_json(q("SELECT * FROM tracks WHERE id=?", (tid,), one=True))


@app.get("/api/tracks/{tid}")
def track(tid: str, u=Depends(current_user)):
    t = q("SELECT * FROM tracks WHERE id=? AND user_id=?", (tid, u["id"]), one=True)
    if not t:
        raise HTTPException(404)
    return track_json(t)


@app.post("/api/tracks/{tid}/cover")
async def upload_cover(tid: str, cover: UploadFile = File(...), u=Depends(current_user)):
    t = q("SELECT * FROM tracks WHERE id=? AND user_id=?", (tid, u["id"]), one=True)
    if not t:
        raise HTTPException(404)
    q("UPDATE tracks SET cover=? WHERE id=?", (await save_cover(tid, cover), tid))
    return track_json(q("SELECT * FROM tracks WHERE id=?", (tid,), one=True))


@app.get("/api/tracks/{tid}/cover")
def track_cover(tid: str):
    t = q("SELECT cover FROM tracks WHERE id=?", (tid,), one=True)
    if not t or not t["cover"] or not Path(t["cover"]).exists():
        raise HTTPException(404)
    return FileResponse(t["cover"], headers={"Cache-Control": "max-age=86400"})


@app.get("/api/tracks/{tid}/peaks")
def track_peaks(tid: str, n: int = 240, u=Depends(current_user)):
    """Волна трека для выбора фрагмента: n значений 0..1 (кэшируется рядом с треком)."""
    t = q("SELECT * FROM tracks WHERE id=? AND user_id=?", (tid, u["id"]), one=True)
    if not t:
        raise HTTPException(404)
    n = max(50, min(600, n))
    cache = Path(t["audio"]).with_suffix(f".peaks{n}.json")
    if cache.exists():
        return json.loads(cache.read_text())
    import array
    import subprocess
    pr = subprocess.run(["ffmpeg", "-v", "error", "-i", t["audio"], "-ac", "1", "-ar", "4000", "-f", "s16le", "-"],
                        capture_output=True)
    raw = pr.stdout
    if not raw:
        log.warning("peaks: ffmpeg вернул пусто для %s: %s", tid, pr.stderr[-300:])
        raise HTTPException(500, "не удалось прочитать звук трека")
    a = array.array("h"); a.frombytes(raw[: len(raw) // 2 * 2])
    step = max(1, len(a) // n)
    peaks = [max((abs(x) for x in a[i:i + step]), default=0) for i in range(0, step * n, step)]
    top = max(peaks) or 1
    out = {"duration": len(a) / 4000, "peaks": [round(p / top, 3) for p in peaks]}
    cache.write_text(json.dumps(out))
    return out


@app.delete("/api/tracks/{tid}")
def delete_track(tid: str, u=Depends(current_user)):
    """Удаляет трек, его обложку, кэш волны и все эдиты по нему (если не монтируются прямо сейчас)."""
    t = q("SELECT * FROM tracks WHERE id=? AND user_id=?", (tid, u["id"]), one=True)
    if not t:
        raise HTTPException(404)
    if t["status"] == "processing" or q("SELECT 1 FROM edits WHERE track_id=? AND status='rendering'", (tid,), one=True):
        raise HTTPException(409, "трек сейчас в работе — удалить можно после монтажа")
    for e in q("SELECT video FROM edits WHERE track_id=?", (tid,)):
        if e["video"]:
            Path(e["video"]).unlink(missing_ok=True)
    audio = Path(t["audio"])
    for f in audio.parent.glob(audio.stem + "*"):
        f.unlink(missing_ok=True)
    if t["cover"]:
        Path(t["cover"]).unlink(missing_ok=True)
    q("DELETE FROM edits WHERE track_id=?", (tid,))
    q("DELETE FROM tracks WHERE id=?", (tid,))
    return {"ok": True}


@app.get("/api/tracks/{tid}/audio")
def track_audio(tid: str):
    t = q("SELECT * FROM tracks WHERE id=?", (tid,), one=True)
    if not t:
        raise HTTPException(404)
    return FileResponse(t["audio"])


class LinesIn(BaseModel):
    lines: list[dict]
    frag_start: float | None = None
    frag_end: float | None = None


@app.put("/api/tracks/{tid}/lines")
def save_lines(tid: str, body: LinesIn, u=Depends(current_user)):
    t = q("SELECT * FROM tracks WHERE id=? AND user_id=?", (tid, u["id"]), one=True)
    if not t:
        raise HTTPException(404)
    clean = [{"text": str(l["text"])[:200], "start": float(l["start"]), "end": float(l["end"]),
              "chorus": bool(l.get("chorus"))} for l in body.lines]
    fs, fe = body.frag_start, body.frag_end
    if fs is not None and fe is not None:   # фрагмент 3–60 с в пределах трека
        dur = t["duration"] or fe
        fs = max(0.0, min(float(fs), dur - 3))
        fe = max(fs + 3, min(float(fe), dur, fs + 60))
    q("UPDATE tracks SET lines=?, frag_start=COALESCE(?,frag_start), frag_end=COALESCE(?,frag_end) WHERE id=?",
      (json.dumps(clean, ensure_ascii=False), fs, fe, tid))
    return track_json(q("SELECT * FROM tracks WHERE id=?", (tid,), one=True))


class EditIn(BaseModel):
    track_id: str
    mode: str = "line"
    bw: bool = False
    footage: str = "mix"
    font: str = "inter"
    kind: str = "edit"        # edit — эдит на футажах, cover — анимация обложки
    caption: str = "ПРЕМЬЕРА"  # надпись для анимации обложки


@app.post("/api/edits")
def make_edit(body: EditIn, u=Depends(current_user)):
    t = q("SELECT * FROM tracks WHERE id=? AND user_id=?", (body.track_id, u["id"]), one=True)
    if not t or t["status"] != "ready":
        raise HTTPException(400, "трек ещё не готов")
    if body.kind == "cover" and not (t["cover"] and Path(t["cover"]).exists()):
        raise HTTPException(400, "сначала добавь обложку трека")
    if not u["is_owner"]:
        if u["free_left"] <= 0:
            raise HTTPException(402, "бесплатные эдиты закончились")
        q("UPDATE users SET free_left=free_left-1 WHERE id=?", (u["id"],))
    eid = uuid.uuid4().hex
    font = body.font if (body.font in FONTS or user_font(u["id"], body.font)) else "inter"
    params = {"mode": body.mode if body.mode in ("line", "word") else "line", "bw": body.bw,
              "footage": body.footage, "font": font, "watermark": not u["is_owner"],
              "kind": "cover" if body.kind == "cover" else "edit", "caption": body.caption.strip().upper()[:24]}
    q("INSERT INTO edits(id,user_id,track_id,params,status,created) VALUES(?,?,?,?,?,?)",
      (eid, u["id"], t["id"], json.dumps(params), "queued", time.time()))
    wake.set()
    return edit_json(q("SELECT * FROM edits WHERE id=?", (eid,), one=True))


@app.get("/api/edits")
def edits(u=Depends(current_user)):
    return [edit_json(e) for e in q("SELECT * FROM edits WHERE user_id=? ORDER BY created DESC LIMIT 30", (u["id"],))]


@app.get("/media/{eid}.mp4")
def media(eid: str):
    e = q("SELECT * FROM edits WHERE id=?", (eid,), one=True)
    if not e or not e["video"] or not Path(e["video"]).exists():
        raise HTTPException(404)
    return FileResponse(e["video"], media_type="video/mp4")


@app.get("/api/health")
def health():
    return {"ok": True, "build": BUILD, "queue": q("SELECT COUNT(*) c FROM edits WHERE status IN ('queued','rendering')", one=True)["c"]}


# ---------------- фоновая очередь ----------------
wake = threading.Event()


def tg_of(user_id: int) -> int | None:
    r = q("SELECT tg_id FROM users WHERE id=?", (user_id,), one=True)
    return r["tg_id"] if r else None


def run_job(cmd: str, args: dict, timeout: int = 1800) -> dict:
    """Тяжёлая работа — в отдельном процессе с низким приоритетом: сервер аппки не тормозит."""
    import subprocess
    import sys
    p = subprocess.run(["nice", "-n", "15", sys.executable, "-m", "server.engine.job", cmd, json.dumps(args, ensure_ascii=False)],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()[-1:] or ["неизвестная ошибка"]
        log.warning("job %s failed: %s", cmd, (p.stderr or "")[-1500:])
        raise RuntimeError(tail[0][:200])
    return json.loads(p.stdout.strip().splitlines()[-1])


def loudest_fragment(audio: str, dur: float, length: float = 20.0) -> tuple[float, float]:
    """Для композиции без слов: самый громкий/насыщенный кусок (обычно кульминация), не с самого начала."""
    import array
    import subprocess
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", audio, "-ac", "1", "-ar", "4000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    a = array.array("h"); a.frombytes(raw[: len(raw) // 2 * 2])
    sec = [sum(x * x for x in a[i:i + 4000]) / 4000 for i in range(0, len(a) - 4000, 4000)]
    length = min(length, max(3.0, dur - 1))
    n = int(length)
    if len(sec) <= n:
        return (0.0, round(min(dur, length), 2))
    best, best_i = -1.0, 0
    lo = int(len(sec) * 0.1)
    for i in range(lo, len(sec) - n):
        v = sum(sec[i:i + n])
        if v > best:
            best, best_i = v, i
    return (float(best_i), float(best_i + n))


def process_track(t: sqlite3.Row):
    q("UPDATE tracks SET status='processing' WHERE id=?", (t["id"],))
    dur = render.probe_duration(Path(t["audio"]))
    if t["instrumental"]:
        fs, fe = loudest_fragment(t["audio"], dur)
        q("UPDATE tracks SET lines='[]', frag_start=?, frag_end=?, duration=?, status='ready' WHERE id=?",
          (fs, fe, dur, t["id"]))
        chat = tg_of(t["user_id"])
        if bot and chat:
            bot.send_message(chat, f"«{t['title']}» готов: композиция без слов, выбрали самый яркий кусок "
                                   f"({fs:.0f}–{fe:.0f} с). Можно сдвинуть рамку и монтировать.", with_app_button=True)
        return
    lyr = [l.strip() for l in (t["lyrics"] or "").splitlines() if l.strip()]
    if FAKE_ASR:
        words, segs = align.fake_transcribe(t["lyrics"] or "ля ля ля", dur)
    else:
        r = run_job("transcribe", {"audio": t["audio"], "lyrics": t["lyrics"]})
        words = [align.Word(w, s, e) for w, s, e in r["words"]]
        segs = [tuple(x) for x in r["segs"]]
    lines = align.align_lines(lyr, words, dur) if lyr else align.lines_from_segments(segs)
    if not lines:
        raise RuntimeError("не удалось распознать текст — добавь текст вручную")
    ch = align.find_chorus_words(words, dur)
    if ch:   # припев по словам — точнее, работает и без текста артиста
        align.mark_chorus_by_time(lines, *ch)
        fs, fe = align.pick_fragment_from(lines, ch[0])
    else:
        a, b = align.find_chorus(lines)
        for i, l in enumerate(lines):
            l["chorus"] = a <= i <= b
        fs, fe = align.pick_fragment(lines)
    q("UPDATE tracks SET lines=?, frag_start=?, frag_end=?, duration=?, status='ready' WHERE id=?",
      (json.dumps(lines, ensure_ascii=False), fs, fe, dur, t["id"]))
    chat = tg_of(t["user_id"])
    if bot and chat:
        bot.send_message(chat, f"«{t['title']}»: текст расставлен, припев найден ({fs:.0f}–{fe:.0f} с). "
                               f"Проверь строки в аппке и жми «Смонтировать».", with_app_button=True)


def process_cover(e: sqlite3.Row, t: sqlite3.Row, p: dict):
    fkey = p.get("font", "inter")
    font_path = None                     # по умолчанию — широкий жирный Unbounded
    if fkey.startswith("u:"):
        uf = user_font(e["user_id"], fkey)
        font_path = uf["path"] if uf else None
    elif fkey in FONTS and fkey not in ("inter",):
        font_path = str(FONTS_DIR / f"{fkey}.ttf")
    out = DATA / "edits" / f"{e['id']}.mp4"
    t0 = time.time()
    run_job("cover", {"cover": t["cover"], "audio": t["audio"], "out": str(out), "start": t["frag_start"],
                      "dur": t["frag_end"] - t["frag_start"], "title": p.get("caption", "ПРЕМЬЕРА"),
                      "font_path": font_path}, timeout=3600)
    q("UPDATE edits SET status='done', video=? WHERE id=?", (str(out), e["id"]))
    chat = tg_of(e["user_id"])
    if bot and chat:
        bot.send_video(chat, str(out), caption=f"Анимация обложки «{t['title']}» · монтаж {time.time() - t0:.0f} с")


def process_edit(e: sqlite3.Row):
    q("UPDATE edits SET status='rendering' WHERE id=?", (e["id"],))
    t = q("SELECT * FROM tracks WHERE id=?", (e["track_id"],), one=True)
    p = json.loads(e["params"])
    if p.get("kind") == "cover":
        return process_cover(e, t, p)
    folder = FOOTAGE / p["footage"] if p["footage"] != "mix" else FOOTAGE
    clips = render.list_footage(folder) or render.list_footage(FOOTAGE)
    if not clips:
        raise RuntimeError("в библиотеке нет футажей — положи видео в /opt/edits/footage/")
    lines = [render.Line(l["start"], l["end"], l["text"]) for l in json.loads(t["lines"])]
    fs, fe = t["frag_start"], t["frag_end"]
    frag = [l for l in lines if l.end > fs and l.start < fe]
    out = DATA / "edits" / f"{e['id']}.mp4"
    # шрифты для libass: комплект + шрифт артиста, если выбран
    fkey = p.get("font", "inter")
    family, scale = FONTS["inter"][0], 1.0
    tmp_fonts = tempfile.TemporaryDirectory()
    for k in FONTS:
        os.symlink(FONTS_DIR / f"{k}.ttf", Path(tmp_fonts.name) / f"{k}.ttf")
    if fkey in FONTS:
        family, scale = FONTS[fkey][0], FONTS[fkey][1]
    else:
        uf = user_font(e["user_id"], fkey)
        if uf and Path(uf["path"]).exists():
            os.symlink(uf["path"], Path(tmp_fonts.name) / ("user" + Path(uf["path"]).suffix))
            family = uf["family"]
    st = render.Style(mode=p["mode"], bw=p["bw"], seed=random.randint(1, 10**9), font=family,
                      font_size=int(72 * scale), fonts_dir=tmp_fonts.name,
                      watermark="LUDUS EDITS" if p.get("watermark") else None)
    t0 = time.time()
    try:
        render.render_edit(render.EditJob(audio=Path(t["audio"]), lines=frag or lines, footage=clips, out=out,
                                          start=fs, duration=fe - fs, style=st))
    finally:
        tmp_fonts.cleanup()
    q("UPDATE edits SET status='done', video=? WHERE id=?", (str(out), e["id"]))
    chat = tg_of(e["user_id"])
    if bot and chat:
        bot.send_video(chat, str(out), caption=f"Эдит на «{t['title']}» · монтаж {time.time() - t0:.0f} с")


def worker():
    while True:
        wake.wait(5)
        wake.clear()
        while True:
            t = q("SELECT * FROM tracks WHERE status='queued' ORDER BY created LIMIT 1", one=True)
            if t:
                try:
                    process_track(t)
                except Exception as ex:
                    log.exception("track %s", t["id"])
                    q("UPDATE tracks SET status='error', error=? WHERE id=?", (str(ex)[:300], t["id"]))
                continue
            e = q("SELECT * FROM edits WHERE status='queued' ORDER BY created LIMIT 1", one=True)
            if e:
                try:
                    process_edit(e)
                except Exception as ex:
                    log.exception("edit %s", e["id"])
                    q("UPDATE edits SET status='error', error=? WHERE id=?", (str(ex)[:300], e["id"]))
                continue
            break


@app.on_event("startup")
def _startup():
    # задачи, прерванные перезапуском, — обратно в очередь
    q("UPDATE tracks SET status='queued' WHERE status='processing'")
    # монтаж, прерванный перезапуском (например, не хватило памяти), не повторяем по кругу
    q("UPDATE edits SET status='error', error='прервано — попробуй ещё раз' WHERE status='rendering'")
    threading.Thread(target=worker, daemon=True, name="worker").start()
    if bot:
        bot.setup_menu_button()
        bot.start_polling()
    log.info("LUDUS EDITS started · dev=%s · fake_asr=%s · bot=%s", DEV, FAKE_ASR, bool(bot))


# фронтенд: отдаём ТОЛЬКО index.html (никаких статик-папок — рядом лежат .env и база)
@app.get("/")
def index():
    return FileResponse(WEB / "index.html", media_type="text/html",
                        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})
