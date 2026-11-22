#!/usr/bin/env python3
"""Добавляет в /opt/museup_bot/bot.py кнопку «🎬 Анимация обложки» после готовых PNG.
Запуск: python3 /opt/edits/deploy/patch_museup_anim.py   (бэкап: bot.py.bak-anim, синтаксис проверяется, при ошибке откат)
Анимацию рисует /opt/edits (server.engine.cover_coin) под nice, по одной за раз. Шрифт и знак — те же, что на статичной обложке."""
import py_compile, shutil, sys
from pathlib import Path

BOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/museup_bot/bot.py")
MARK = "# ───── анимация обложки (cover_coin) ─────"

ANCHOR_SEND = '    await state.clear()\n    await m.answer("Готово: PNG 3000×3000.'
SEND = '''    CANIM[str(stamp)] = {"cover": files[0], "artist": d["c_artist"], "song": d["c_song"]}
    await m.answer("Оживить обложку? 15 с, вертикаль для сторис и рилс.",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎬  Анимация обложки", callback_data=f"canim:{stamp}")]]))
'''

HANDLER = f'''{MARK}
EDITS_DIR = "/opt/edits"
CANIM: dict = {{}}
CANIM_LOCK = asyncio.Lock()          # одна анимация за раз: сервер на 1 vCPU


def _canim_run(cover, out, title):
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    font = os.path.join(here, "assets", "fonts", "serif.ttf")
    cmd = ["nice", "-n", "10", os.path.join(EDITS_DIR, "venv", "bin", "python"), "-m", "server.engine.cover_coin", cover, out,
           "--title", title, "--caption", "ПРЕМЬЕРА", "--font", font, "--dur", "15"]
    for p in getattr(cover_mod, "MARK_CANDIDATES", []):
        if p and os.path.exists(p):
            cmd += ["--icon", p]
            break
    r = subprocess.run(cmd, cwd=EDITS_DIR, env={{**os.environ, "PYTHONPATH": EDITS_DIR}}, capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError((r.stderr or r.stdout or "нет вывода")[-300:])


@router.callback_query(F.data.startswith("canim:"))
async def cover_anim(c, bot: Bot):
    if not allowed(c.from_user.id):
        return
    job = CANIM.get(c.data.split(":", 1)[1])
    await c.answer()
    if not job or not os.path.exists(job["cover"]):
        await c.message.answer("Эта обложка уже устарела — собери её заново через «🎼  Дизайн обложки».")
        return
    if CANIM_LOCK.locked():
        await c.message.answer("Сейчас оживляю другую обложку, твоя пойдёт следом.")
    async with CANIM_LOCK:
        wait = await c.message.answer("Оживляю обложку, это около минуты…")
        out = os.path.splitext(job["cover"])[0] + "_anim.mp4"
        title = f'{{job["artist"]}} - {{job["song"]}}'
        try:
            await asyncio.to_thread(_canim_run, job["cover"], out, title)
        except Exception as e:
            await wait.edit_text(f"Анимация не собралась: {{e}}")
            return
        await c.message.answer_video(FSInputFile(out), width=720, height=1280, supports_streaming=True, caption=title)
        await wait.delete()


'''


def main():
    s = BOT.read_text(encoding="utf-8")
    if MARK in s:
        sys.exit("уже установлено")
    if ANCHOR_SEND not in s or "\nasync def main():" not in s:
        sys.exit("не нашёл место вставки — бот изменился, пришли bot.py")
    bak = BOT.with_name(BOT.name + ".bak-anim")
    shutil.copy2(BOT, bak)
    s = s.replace(ANCHOR_SEND, SEND + ANCHOR_SEND, 1)
    s = s.replace("\nasync def main():", "\n" + HANDLER + "async def main():", 1)
    BOT.write_text(s, encoding="utf-8")
    try:
        py_compile.compile(str(BOT), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(bak, BOT)
        sys.exit(f"ошибка синтаксиса, откатил: {e}")
    print("готово; бэкап", bak)


main()
