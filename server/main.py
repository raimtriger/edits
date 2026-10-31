"""LUDUS EDITS — сервер мини-аппки.

Запуск: uvicorn server.main:app --host 0.0.0.0 --port 8444 --ssl-keyfile ... --ssl-certfile ...
Настройки — в /opt/edits/.env (см. deploy/env.example).
"""
from __future__ import annotations

import json
import logging
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

for d in (DATA / "tracks", DATA / "edits"):
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
    CREATE TABLE IF NOT EXISTS edits(id TEXT PRIMARY KEY, user_id INTEGER, track_id TEXT, params TEXT,
        status TEXT, video TEXT, error TEXT, created REAL);
    """)
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
bot = Bot(BOT_TOKEN, WEBAPP_URL) if BOT_TOKEN else None


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
            "lines": json.loads(t["lines"]) if t["lines"] else []}


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


@app.get("/api/tracks")
def tracks(u=Depends(current_user)):
    return [track_json(t) for t in q("SELECT * FROM tracks WHERE user_id=? ORDER BY created DESC", (u["id"],))]


@app.post("/api/tracks")
async def upload_track(audio: UploadFile = File(...), title: str = Form(""), lyrics: str = Form(""),
                       u=Depends(current_user)):
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
    title = title.strip() or Path(audio.filename or "Трек").stem
    q("INSERT INTO tracks(id,user_id,title,audio,lyrics,status,created) VALUES(?,?,?,?,?,?,?)",
      (tid, u["id"], title[:80], str(path), lyrics.strip(), "queued", time.time()))
    wake.set()
    return track_json(q("SELECT * FROM tracks WHERE id=?", (tid,), one=True))


@app.get("/api/tracks/{tid}")
def track(tid: str, u=Depends(current_user)):
    t = q("SELECT * FROM tracks WHERE id=? AND user_id=?", (tid, u["id"]), one=True)
    if not t:
        raise HTTPException(404)
    return track_json(t)


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
    clean = [{"text": str(l["text"])[:200], "start": float(l["start"]), "end": float(l["end"])} for l in body.lines]
    q("UPDATE tracks SET lines=?, frag_start=COALESCE(?,frag_start), frag_end=COALESCE(?,frag_end) WHERE id=?",
      (json.dumps(clean, ensure_ascii=False), body.frag_start, body.frag_end, tid))
    return track_json(q("SELECT * FROM tracks WHERE id=?", (tid,), one=True))


class EditIn(BaseModel):
    track_id: str
    mode: str = "line"
    bw: bool = False
    footage: str = "mix"


@app.post("/api/edits")
def make_edit(body: EditIn, u=Depends(current_user)):
    t = q("SELECT * FROM tracks WHERE id=? AND user_id=?", (body.track_id, u["id"]), one=True)
    if not t or t["status"] != "ready":
        raise HTTPException(400, "трек ещё не готов")
    if not u["is_owner"]:
        if u["free_left"] <= 0:
            raise HTTPException(402, "бесплатные эдиты закончились")
        q("UPDATE users SET free_left=free_left-1 WHERE id=?", (u["id"],))
    eid = uuid.uuid4().hex
    params = {"mode": body.mode if body.mode in ("line", "word") else "line", "bw": body.bw,
              "footage": body.footage, "watermark": not u["is_owner"]}
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
    return {"ok": True, "queue": q("SELECT COUNT(*) c FROM edits WHERE status IN ('queued','rendering')", one=True)["c"]}


# ---------------- фоновая очередь ----------------
wake = threading.Event()


def tg_of(user_id: int) -> int | None:
    r = q("SELECT tg_id FROM users WHERE id=?", (user_id,), one=True)
    return r["tg_id"] if r else None


def process_track(t: sqlite3.Row):
    q("UPDATE tracks SET status='processing' WHERE id=?", (t["id"],))
    dur = render.probe_duration(Path(t["audio"]))
    lyr = [l.strip() for l in (t["lyrics"] or "").splitlines() if l.strip()]
    if FAKE_ASR:
        words, segs = align.fake_transcribe(t["lyrics"] or "ля ля ля", dur)
    else:
        words, segs = align.transcribe(t["audio"], t["lyrics"])
    lines = align.align_lines(lyr, words, dur) if lyr else align.lines_from_segments(segs)
    if not lines:
        raise RuntimeError("не удалось распознать текст — добавь текст вручную")
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


def process_edit(e: sqlite3.Row):
    q("UPDATE edits SET status='rendering' WHERE id=?", (e["id"],))
    t = q("SELECT * FROM tracks WHERE id=?", (e["track_id"],), one=True)
    p = json.loads(e["params"])
    folder = FOOTAGE / p["footage"] if p["footage"] != "mix" else FOOTAGE
    clips = render.list_footage(folder) or render.list_footage(FOOTAGE)
    if not clips:
        raise RuntimeError("в библиотеке нет футажей — положи видео в /opt/edits/footage/")
    lines = [render.Line(l["start"], l["end"], l["text"]) for l in json.loads(t["lines"])]
    fs, fe = t["frag_start"], t["frag_end"]
    frag = [l for l in lines if l.end > fs and l.start < fe]
    out = DATA / "edits" / f"{e['id']}.mp4"
    st = render.Style(mode=p["mode"], bw=p["bw"], seed=random.randint(1, 10**9),
                      watermark="LUDUS EDITS" if p.get("watermark") else None)
    t0 = time.time()
    render.render_edit(render.EditJob(audio=Path(t["audio"]), lines=frag or lines, footage=clips, out=out,
                                      start=fs, duration=fe - fs, style=st))
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
    q("UPDATE edits SET status='queued' WHERE status='rendering'")
    threading.Thread(target=worker, daemon=True, name="worker").start()
    if bot:
        bot.setup_menu_button()
        bot.start_polling()
    log.info("LUDUS EDITS started · dev=%s · fake_asr=%s · bot=%s", DEV, FAKE_ASR, bool(bot))


# фронтенд: отдаём ТОЛЬКО index.html (никаких статик-папок — рядом лежат .env и база)
@app.get("/")
def index():
    return FileResponse(WEB / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})
