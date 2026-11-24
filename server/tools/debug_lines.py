#!/usr/bin/env python3
"""Диагностика строк: показывает текст, сохранённые тайминги и то, что слышит распознавание (слова со временем).
Запуск: cd /opt/edits && WHISPER_MODEL=small venv/bin/python -m server.tools.debug_lines [часть названия трека]"""
import os, sqlite3, sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.engine import align

db = sqlite3.connect(Path(os.environ.get("EDITS_DATA", "/opt/edits/data")) / "edits.db")
db.row_factory = sqlite3.Row
name = sys.argv[1] if len(sys.argv) > 1 else ""
rows = db.execute("SELECT * FROM tracks ORDER BY created DESC").fetchall()
hit = [r for r in rows if name.lower() in (r["title"] or "").lower()]   # SQLite LIKE не понимает регистр кириллицы, ищем сами
if not hit:
    print("трек не найден. Что есть в базе:")
    for r in rows[:15]:
        print("  ", r["title"], "·", r["artist"] or "-")
    sys.exit(1)
t = hit[0]
print("ТРЕК:", t["title"], "· длительность", t["duration"], "· фрагмент", t["frag_start"], "-", t["frag_end"])
print("\nТЕКСТ, который ты прислал:")
print(t["lyrics"] or "(пусто)")
print("\nСТРОКИ В БАЗЕ (старт-конец):")
for l in json.loads(t["lines"] or "[]"):
    print(f'{l["start"]:7.1f} {l["end"]:7.1f}  {l["text"]}')
print("\nЧТО СЛЫШИТ РАСПОЗНАВАНИЕ (модель", os.environ.get("WHISPER_MODEL", "small"), "):")
words, segs = align.transcribe(t["audio"], t["lyrics"])
print("слов:", len(words), "· сегментов:", len(segs))
print(" ".join(f"{w.text}[{w.start:.0f}]" for w in words))
print("\nВЫРАВНИВАНИЕ СЕЙЧАС:")
lyr = [l.strip() for l in (t["lyrics"] or "").splitlines() if l.strip()]
for o in align.align_lines(lyr, words, t["duration"] or 180):
    print(f'{o["start"]:7.1f} {o["end"]:7.1f}  {o["text"]}')
