#!/usr/bin/env python3
"""Подтягивает футажи с публичной папки Яндекс.Диска в /opt/edits/footage/<категория>/.
Запуск: /opt/edits/venv/bin/python /opt/edits/server/tools/sync_footage.py <ссылка> [--replace]
Папки верхнего уровня с именами city/night/nature/love/road/bokeh/cinema/stock становятся категориями, всё остальное (в т.ч. лежащее россыпью
или в других папках) попадает в stock. Уже скачанное пропускается, можно запускать повторно.
--replace: качает в footage.new, и только если скачано хотя бы одно видео, старую папку откладывает в footage.old-<время> и ставит новую (откат: вернуть .old)."""
import json, os, shutil, sys, time, urllib.parse, urllib.request
from pathlib import Path

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
REPLACE = "--replace" in sys.argv
LINK = ARGS[0] if ARGS else "https://disk.yandex.ru/d/KbQXxQxahi5ZSQ"
DEST = Path(os.environ.get("EDITS_FOOTAGE", "/opt/edits/footage"))
CATS = {"city", "night", "nature", "love", "road", "bokeh", "cinema", "stock"}
EXT = (".mp4", ".mov", ".webm", ".m4v")
MAX_MB = 300
API = "https://cloud-api.yandex.net/v1/disk/public/resources"


def api(path, offset=0):
    u = f"{API}?{urllib.parse.urlencode({'public_key': LINK, 'path': path, 'limit': 200, 'offset': offset})}"
    with urllib.request.urlopen(u, timeout=30) as r:
        return json.load(r)


def items(path):
    off = 0
    while True:
        j = api(path, off)
        emb = j.get("_embedded", {})
        for it in emb.get("items", []):
            yield it
        off += len(emb.get("items", []))
        if off >= emb.get("total", 0) or not emb.get("items"):
            return


def walk(path="/", cat=None, depth=0):
    """Все видео рекурсивно; категория — имя папки верхнего уровня из CATS, иначе stock."""
    for it in items(path):
        if it["type"] == "dir":
            name = it["name"].lower()
            sub = name if (depth == 0 and name in CATS) else cat
            yield from walk(it["path"], sub, depth + 1)
        elif it["name"].lower().endswith(EXT):
            yield (cat or "stock"), it


def main():
    out_root = DEST.with_name(DEST.name + ".new") if REPLACE else DEST
    n_new = n_all = 0
    for cat, f in walk():
        n_all += 1
        out = out_root / cat
        out.mkdir(parents=True, exist_ok=True)
        target = out / f["name"]
        if target.exists() and target.stat().st_size == f.get("size"):
            continue
        if f.get("size", 0) > MAX_MB * 1048576:
            print(f"пропуск (>{MAX_MB} МБ): {cat}/{f['name']}")
            continue
        tmp = target.with_suffix(target.suffix + ".part")
        urllib.request.urlretrieve(f["file"], tmp)
        tmp.replace(target)
        n_new += 1
        print(f"+ {cat}/{f['name']}  {f.get('size', 0) // 1048576} МБ", flush=True)
    print(f"готово: новых {n_new}, всего видео в папке {n_all}")
    if REPLACE:
        if n_all == 0 or not out_root.exists():
            sys.exit("в папке нет видео — старый футаж не тронут")
        old = DEST.with_name(f"{DEST.name}.old-{time.strftime('%Y%m%d-%H%M')}")
        if DEST.exists():
            DEST.rename(old)
            print("старый футаж:", old)
        out_root.rename(DEST)
        print("новый футаж на месте")


if __name__ == "__main__":
    main()
