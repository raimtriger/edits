"""Тяжёлые задачи в ОТДЕЛЬНОМ процессе, чтобы веб-сервер всегда отвечал мгновенно.

python -m server.engine.job transcribe '<json>'   -> stdout: {"words": [[text, start, end], ...], "segs": [[s, e, text], ...]}
python -m server.engine.job cover '<json>'        -> рендер анимации обложки
"""
import json
import sys


def main():
    cmd, args = sys.argv[1], json.loads(sys.argv[2])
    if cmd == "transcribe":
        from .align import transcribe
        words, segs = transcribe(args["audio"], args.get("lyrics"))
        print(json.dumps({"words": [[w.text, w.start, w.end] for w in words], "segs": segs}, ensure_ascii=False))
    elif cmd == "cover":
        from .cover_anim import render_cover_anim
        render_cover_anim(**args)
        print(json.dumps({"ok": True}))
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
