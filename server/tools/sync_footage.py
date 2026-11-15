#!/usr/bin/env python3
"""Подтягивает футажи с публичной папки Яндекс.Диска в /opt/edits/footage/<категория>/.
Запуск: /opt/edits/venv/bin/python /opt/edits/server/tools/sync_footage.py [ссылка]
Папки на диске = категории (city, night, nature, love, road, ...). Уже скачанное пропускается, можно запускать повторно."""
import json, os, sys, urllib.parse, urllib.request
from pathlib import Path

LINK = sys.argv[1] if len(sys.argv) > 1 else "https://disk.yandex.ru/d/KbQXxQxahi5ZSQ"
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


def main():
    n_new = n_all = 0
    for top in items("/"):
        # корень может быть папкой «edits» с категориями внутри — спускаемся на один уровень, если категорий нет сверху
        pass
    roots = [("/", list(items("/")))]
    names = {i["name"].lower() for i in roots[0][1] if i["type"] == "dir"}
    if not (names & CATS):  # категории лежат глубже (например, внутри папки edits)
        for d in [i for i in roots[0][1] if i["type"] == "dir"]:
            sub = list(items(d["path"]))
            if {i["name"].lower() for i in sub if i["type"] == "dir"} & CATS:
                roots = [(d["path"], sub)]
                break
    base, top_items = roots[0]
    for d in top_items:
        cat = d["name"].lower()
        if d["type"] != "dir" or cat not in CATS:
            continue
        out = DEST / cat
        out.mkdir(parents=True, exist_ok=True)
        for f in items(d["path"]):
            if f["type"] != "file" or not f["name"].lower().endswith(EXT):
                continue
            n_all += 1
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
    print(f"готово: новых {n_new}, всего видео {n_all}")


if __name__ == "__main__":
    main()
