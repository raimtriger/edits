"""Распознавание текста, привязка строк ко времени и поиск припева.

- transcribe(): faster-whisper -> слова с таймингами
- align_lines(): если артист дал текст — привязываем ЕГО строки к распознанным словам
- lines_from_segments(): если текста нет — строки берём из распознавания
- find_chorus(): ищем повторяющийся блок строк
"""
from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass


@dataclass
class Word:
    text: str
    start: float
    end: float


_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # импорт тут, чтобы сервер стартовал без модели
        size = os.environ.get("WHISPER_MODEL", "small")
        threads = int(os.environ.get("WHISPER_THREADS", "2"))
        _model = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=threads)
    return _model


def transcribe(audio_path: str, lyrics_hint: str | None = None) -> tuple[list[Word], list[tuple[float, float, str]]]:
    """Возвращает (слова, сегменты)."""
    model = _get_model()
    prompt = (lyrics_hint or "")[:400] or None
    segments, _ = model.transcribe(
        audio_path, language="ru", word_timestamps=True, vad_filter=True,
        initial_prompt=prompt, condition_on_previous_text=False,
    )
    words, segs = [], []
    for s in segments:
        segs.append((s.start, s.end, s.text.strip()))
        for w in (s.words or []):
            words.append(Word(w.word.strip(), w.start, w.end))
    return words, segs


def fake_transcribe(lyrics: str, duration: float) -> tuple[list[Word], list[tuple[float, float, str]]]:
    """Для тестов без модели: раскладываем текст равномерно."""
    toks = [t for ln in lyrics.splitlines() for t in ln.split()]
    if not toks:
        return [], []
    step = duration * 0.85 / len(toks)
    words = [Word(t, 1 + i * step, 1 + (i + 0.9) * step) for i, t in enumerate(toks)]
    return words, []


def norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    return re.sub(r"[^\w]+", "", s)


def align_lines(lines: list[str], words: list[Word], total: float) -> list[dict]:
    """Привязываем строки артиста к словам распознавания через выравнивание последовательностей."""
    toks, owner = [], []
    for i, ln in enumerate(lines):
        for t in ln.split():
            n = norm(t)
            if n:
                toks.append(n)
                owner.append(i)
    wn = [norm(w.text) for w in words]
    sm = difflib.SequenceMatcher(a=toks, b=wn, autojunk=False)
    hit: dict[int, list[Word]] = {}
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            hit.setdefault(owner[blk.a + k], []).append(words[blk.b + k])

    out = []
    for i, ln in enumerate(lines):
        ws = hit.get(i)
        out.append({"text": ln, "start": ws[0].start if ws else None, "end": ws[-1].end if ws else None})
    _fill_gaps(out, total)
    return out


def _fill_gaps(out: list[dict], total: float) -> None:
    """Строки, которые не распознались, ставим между соседями."""
    n = len(out)
    i = 0
    while i < n:
        if out[i]["start"] is not None:
            i += 1
            continue
        j = i
        while j < n and out[j]["start"] is None:
            j += 1
        left = out[i - 1]["end"] if i > 0 else 0.5
        right = out[j]["start"] if j < n else min(total, left + 3.0 * (j - i))
        step = max(0.6, (right - left) / (j - i)) if right > left else 2.0
        for k in range(i, j):
            out[k]["start"] = round(left + (k - i) * step, 2)
            out[k]["end"] = round(left + (k - i + 1) * step, 2)
        i = j
    # строка держится до начала следующей (но не дольше 6 с)
    for k in range(n):
        nxt = out[k + 1]["start"] if k + 1 < n else total
        out[k]["end"] = round(min(max(out[k]["end"] or 0, nxt - 0.05), out[k]["start"] + 6.0, nxt), 2)
        out[k]["start"] = round(out[k]["start"], 2)


def lines_from_segments(segs: list[tuple[float, float, str]], max_words: int = 6) -> list[dict]:
    """Без текста артиста: режем распознанные сегменты на короткие строки."""
    out = []
    for s, e, text in segs:
        ws = text.split()
        if not ws:
            continue
        chunks = [ws[i:i + max_words] for i in range(0, len(ws), max_words)]
        dur = (e - s) / len(chunks)
        for k, ch in enumerate(chunks):
            out.append({"text": " ".join(ch), "start": round(s + k * dur, 2), "end": round(s + (k + 1) * dur, 2)})
    return out


def find_chorus(lines: list[dict]) -> tuple[int, int]:
    """Индексы [from, to] припева: самый длинный блок подряд идущих строк, которые повторяются в песне."""
    keys = [norm(l["text"]) for l in lines]
    counts = {k: keys.count(k) for k in keys}
    best, cur_start = (0, -1), None
    for i, k in enumerate(keys + ["__end__"]):
        rep = i < len(keys) and counts.get(k, 0) >= 2 and k
        if rep and cur_start is None:
            cur_start = i
        if not rep and cur_start is not None:
            if i - cur_start > best[1] - best[0] + 1:
                best = (cur_start, i - 1)
            cur_start = None
    if best[1] >= best[0] >= 0 and best[1] >= 0:
        return best
    # повторов нет — берём кусок с ~трети песни
    if not lines:
        return (0, -1)
    a = len(lines) // 3
    return (a, min(len(lines) - 1, a + 5))


def pick_fragment(lines: list[dict], max_len: float = 28.0) -> tuple[float, float]:
    """Отрывок для эдита: от начала припева, целыми строками, не длиннее max_len."""
    a, b = find_chorus(lines)
    if b < a:
        return (0.0, min(max_len, 20.0))
    start = max(0.0, lines[a]["start"] - 0.3)
    end = lines[a]["end"]
    for k in range(a, len(lines)):
        if lines[k]["end"] - start > max_len:
            break
        end = lines[k]["end"]
        if k >= b and end - start >= 15:
            break
    return (round(start, 2), round(end + 0.4, 2))
