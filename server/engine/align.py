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
    """Возвращает (слова, сегменты). Параметры как в рабочем пайплайне автопостинга: без VAD (на пении под музыку он вырезает всё), beam 1."""
    model = _get_model()
    prompt = (lyrics_hint or "")[:400] or None
    segments, _ = model.transcribe(
        audio_path, language="ru", word_timestamps=True, vad_filter=False,
        initial_prompt=prompt, condition_on_previous_text=False, beam_size=1)
    words, segs = [], []
    for s in segments:
        # в проигрышах Whisper с подсказкой-текстом любит «допевать» строки из подсказки: такие сегменты отбрасываем
        if getattr(s, "no_speech_prob", 0) > 0.6 and getattr(s, "avg_logprob", 0) < -0.8:
            continue
        if getattr(s, "avg_logprob", 0) < -1.6:
            continue
        segs.append((s.start, s.end, s.text.strip()))
        for w in (s.words or []):
            if getattr(w, "probability", 1.0) < 0.2:
                continue
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


def _dp_match(a: list[str], b: list[str]) -> list[tuple[int, int]]:
    """Монотонное выравнивание (Needleman–Wunsch): пары (i, j), где слово текста a[i] совпало с распознанным b[j].
    В отличие от difflib, не «хватает» первый попавшийся повтор припева: порядок слов сохраняется глобально,
    поэтому все 4-6 одинаковых припевов встают каждый на своё место."""
    n, m = len(a), len(b)
    if not n or not m:
        return []
    import numpy as np
    GA, GB = -0.6, -0.25          # пропуск слова текста / лишнего распознанного слова
    S = np.zeros((n + 1, m + 1), np.float32)
    S[:, 0] = np.arange(n + 1) * GA * 0.0      # начало и конец трека могут быть без голоса — пропуски по краям бесплатны
    T = np.zeros((n + 1, m + 1), np.int8)       # 0 диагональ, 1 вверх (пропуск a), 2 влево (пропуск b)
    for i in range(1, n + 1):
        ai = a[i - 1]
        row, prev = S[i], S[i - 1]
        for j in range(1, m + 1):
            sc = 2.0 if ai == b[j - 1] else -1.0
            d, u, l = prev[j - 1] + sc, prev[j] + GA, row[j - 1] + GB
            if d >= u and d >= l:
                row[j], T[i, j] = d, 0
            elif u >= l:
                row[j], T[i, j] = u, 1
            else:
                row[j], T[i, j] = l, 2
    # конец: лучшая клетка в последней строке или последнем столбце (хвост без голоса бесплатный)
    i, j = n, int(np.argmax(S[n]))
    if S[:, m].max() > S[n, j]:
        i, j = int(np.argmax(S[:, m])), m
    pairs = []
    while i > 0 and j > 0:
        t = T[i, j]
        if t == 0:
            if a[i - 1] == b[j - 1]:
                pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif t == 1:
            i -= 1
        else:
            j -= 1
    return pairs[::-1]


def _word_times(text: str, m: dict[int, Word]) -> list[list[float]] | None:
    """Время каждого слова строки: у пойманных — из распознавания, у остальных — между соседними пойманными."""
    n = len(text.split())
    if n == 0 or not m:
        return None
    t = [[m[k].start, m[k].end] if k in m else None for k in range(n)]
    known = [k for k in range(n) if t[k]]
    for k in range(n):
        if t[k]:
            continue
        lo = max((j for j in known if j < k), default=None)
        hi = min((j for j in known if j > k), default=None)
        if lo is not None and hi is not None:
            a, b = t[lo][1], t[hi][0]
            span = max(0.0, b - a)
            gaps = hi - lo - 1
            idx = k - lo - 1
            w = span / (gaps + 1) if gaps >= 0 else 0.3
            t[k] = [a + w * idx + 0.0, a + w * (idx + 1)]
        elif lo is not None:
            t[k] = [t[lo][1] + 0.35 * (k - lo - 1), t[lo][1] + 0.35 * (k - lo)]
        else:
            t[k] = [t[hi][0] - 0.35 * (hi - k), t[hi][0] - 0.35 * (hi - k - 1)]
    for k in range(1, n):                           # время не идёт назад
        if t[k][0] < t[k - 1][0]:
            t[k][0] = t[k - 1][0]
        t[k][1] = max(t[k][1], t[k][0] + 0.05)
    return [[round(a, 2), round(b, 2)] for a, b in t]


def align_lines(lines: list[str], words: list[Word], total: float) -> list[dict]:
    """Привязываем строки артиста к словам распознавания через выравнивание последовательностей."""
    toks, owner, tpos = [], [], []
    for i, ln in enumerate(lines):
        for k, t in enumerate(ln.split()):
            n = norm(t)
            if n:
                toks.append(n)
                owner.append(i)
                tpos.append(k)
    # мусор распознавания: не-кириллица/латиница («שוב») и слова, у которых время «прыгнуло» назад
    clean: list[Word] = []
    for w in words:
        if not re.search(r"[а-яёa-z]", w.text.lower()):
            continue
        if clean and w.start < clean[-1].start - 0.3:
            continue
        clean.append(w)
    words = clean
    wn = [norm(w.text) for w in words]
    # распознавание на пении часто путает буквы — подтягиваем слово к ближайшему слову текста (похожесть ≥ 0,7)
    vocab = sorted(set(toks))
    cache: dict[str, str] = {}
    for k, w in enumerate(wn):
        if w and w not in vocab:
            if w not in cache:
                m = difflib.get_close_matches(w, vocab, n=1, cutoff=0.7)
                cache[w] = m[0] if m else w
            wn[k] = cache[w]
    hit: dict[int, list[Word]] = {}
    wmatch: dict[int, dict[int, Word]] = {}          # строка -> {номер слова в строке: слово распознавания}
    for a_i, b_j in _dp_match(toks, wn):
        hit.setdefault(owner[a_i], []).append(words[b_j])
        wmatch.setdefault(owner[a_i], {})[tpos[a_i]] = words[b_j]

    out = []
    for i, ln in enumerate(lines):
        ws = hit.get(i) or []
        n_w = max(1, len([t for t in ln.split() if norm(t)]))
        if ws and len(ws) / n_w >= 0.34:           # строку принимаем, если поймано не меньше трети слов (как в автопостинге)
            out.append({"text": ln, "start": min(w.start for w in ws), "end": max(w.end for w in ws),
                        "wt": _word_times(ln, wmatch.get(i, {}))})
        else:
            out.append({"text": ln, "start": None, "end": None})
    prev_end = -1.0
    for o in out:                                   # строки не по порядку — плохое совпадение: выкидываем, их место заполнится между соседями
        if o["start"] is None:
            continue
        if o["start"] < prev_end - 0.2:
            o["start"] = o["end"] = None
        else:
            prev_end = o["end"]
    floor = 0.0                                     # Whisper нередко цепляет только хвост строки: слишком короткую строку растягиваем назад
    for o in out:
        if o["start"] is None:
            continue
        est = max(0.8, 0.34 * max(1, len([t for t in o["text"].split() if norm(t)])))
        if o["end"] - o["start"] < 0.6 * est:
            o["start"] = max(floor, o["end"] - est)
        floor = o["end"] + 0.05
    if sum(1 for o in out if o["start"] is not None) < max(2, len(lines) * 0.25):
        # распознавание почти ничего не поймало: раскладываем строки по словам на участке, где вообще был голос
        a = words[0].start if words else 1.0
        b = words[-1].end if words else total * 0.9
        if b - a < 3 or len(words) < 3:
            a, b = min(1.0, total * 0.1), total * 0.92
        cnt = [max(1, len(l.split())) for l in lines]
        t, tot = a, sum(cnt)
        for o, c in zip(out, cnt):
            o["start"], o["end"] = t, t + (b - a) * c / tot
            t = o["end"]
    for o in out:                                   # слова-времена нужны только строкам, реально пойманным; у остальных времена угаданные
        if o["start"] is None or o.get("wt") is None:
            o.pop("wt", None)
    _fill_gaps(out, total, words)
    return out


def _fill_gaps(out: list[dict], total: float, words: list[Word] | None = None) -> None:
    """Строки, которые не распознались, ставим между соседями."""
    n = len(out)
    for o in out:
        o["_m"] = o["start"] is not None            # строка реально поймана распознаванием
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
        left = min(left, total - 0.4 * (j - i))
        cnt = j - i
        # непойманные строки ставим только туда, где слышен голос (слова распознавания), а не растягиваем на весь проигрыш
        vs = [w for w in (words or []) if w.start >= left - 0.2 and w.end <= right + 0.2]
        spans = []
        for w in vs:
            if spans and w.start - spans[-1][1] < 1.5:
                spans[-1][1] = max(spans[-1][1], w.end)
            else:
                spans.append([w.start, w.end])
        voiced = sum(b - a for a, b in spans)
        if voiced >= 0.8 * cnt:
            def at(x):                                # позиция в «голосовом» времени -> реальное время
                for a, b in spans:
                    if x <= b - a:
                        return a + x
                    x -= b - a
                return spans[-1][1]
            for k in range(cnt):
                out[i + k]["start"] = round(at(voiced * k / cnt), 2)
                out[i + k]["end"] = round(at(voiced * (k + 1) / cnt), 2)
        else:                                          # голоса нет — не больше 2.5 с на строку, от левого соседа
            step = min(2.5, max(0.6, (right - left) / cnt)) if right > left else 2.0
            for k in range(cnt):
                out[i + k]["start"] = round(left + k * step, 2)
                out[i + k]["end"] = round(left + (k + 1) * step, 2)
        i = j
    # все строки внутри трека и по порядку: не выходим за конец и не накладываемся
    for k in range(n - 1, -1, -1):
        lim = total - 0.4 if k == n - 1 else out[k + 1]["start"] - 0.4
        out[k]["start"] = max(0.0, min(out[k]["start"], lim))
    for k in range(1, n):
        if out[k]["start"] < out[k - 1]["start"] + 0.3:
            out[k]["start"] = out[k - 1]["start"] + 0.3
    # строка держится до начала следующей (но не дольше 6 с)
    for k in range(n):
        nxt = out[k + 1]["start"] if k + 1 < n else total
        nw = max(1, len(out[k]["text"].split()))
        hold = out[k]["start"] + min(6.0, 1.2 + 0.5 * nw)      # строка не висит в проигрыше: держим не дольше пения + ~1 с
        end = max(out[k]["end"] or 0, nxt - 0.05) if nxt - out[k]["end"] < 1.0 else out[k]["end"] + 0.9
        out[k]["end"] = round(min(max(end, out[k]["start"] + 0.8), max(hold, out[k]["end"] + 0.9), nxt), 2)
        out[k]["start"] = round(out[k]["start"], 2)
        out[k].pop("_m", None)


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


def find_chorus_lyrics(lines: list[dict], min_words: int = 6) -> tuple[int, int, list[int]] | None:
    """Припев по ТЕКСТУ артиста: самый «весомый» блок из 2–8 подряд идущих строк, который повторяется (длина × повторы).
    Возвращает (первая строка, последняя строка первого появления, индексы начал всех повторов)."""
    keys = [norm(l["text"]) for l in lines]
    best = None
    for n in range(min(8, len(keys) // 2), 1, -1):
        seen: dict[tuple, list[int]] = {}
        for i in range(len(keys) - n + 1):
            g = tuple(keys[i:i + n])
            if all(g):
                seen.setdefault(g, []).append(i)
        for g, pos in seen.items():
            occ = [pos[0]]
            for p_ in pos[1:]:
                if p_ - occ[-1] >= n:
                    occ.append(p_)
            if len(occ) < 2 or sum(len(x) for x in g) < min_words * 3:
                continue
            score = n * min(len(occ), 4)
            if best is None or score > best[0] or (score == best[0] and occ[0] < best[1][0]):
                best = (score, occ, n)
    if not best:
        return None
    _, occ, n = best
    return occ[0], occ[0] + n - 1, occ


def find_chorus_words(words: list[Word], total: float, min_n: int = 5) -> tuple[float, float] | None:
    """Припев по словам: самая «весомая» повторяющаяся фраза (длина × повторы), берём ПЕРВОЕ её появление.
    Работает и без текста артиста — не зависит от того, как распознавание нарезало строки."""
    keys = [norm(w.text) for w in words]
    idx = [i for i, k in enumerate(keys) if k]
    seq = [keys[i] for i in idx]
    best = None  # (score, first_pos, n)
    for n in range(min(16, len(seq) // 2), min_n - 1, -1):
        seen: dict[tuple, list[int]] = {}
        for i in range(len(seq) - n + 1):
            seen.setdefault(tuple(seq[i:i + n]), []).append(i)
        for gram, pos in seen.items():
            # повторы не должны налезать друг на друга
            occ = [pos[0]]
            for p_ in pos[1:]:
                if p_ - occ[-1] >= n:
                    occ.append(p_)
            if len(occ) < 2:
                continue
            score = n * min(len(occ), 4)
            if best is None or score > best[0] or (score == best[0] and occ[0] < best[1]):
                best = (score, occ[0], n)
    if not best:
        return None
    _, first, n = best
    # расширяем назад: если перед найденной фразой слова тоже совпадают со вторым повтором — это тоже припев
    w0, w1 = words[idx[first]], words[idx[first + n - 1]]
    return (max(0.0, w0.start), min(total, w1.end))


def mark_chorus_by_time(lines: list[dict], start: float, end: float) -> None:
    for l in lines:
        l["chorus"] = bool(l["end"] > start + 0.05 and l["start"] < end - 0.05)


def pick_fragment_from(lines: list[dict], start: float, max_len: float = 28.0, min_len: float = 15.0) -> tuple[float, float]:
    """Фрагмент от времени start (начало припева) целыми строками."""
    fs = max(0.0, start - 2.5)                      # начинаем на 2–3 с раньше припева, чтобы эдит не обрывался на первом слове
    for l in lines:                                 # но не захватываем хвост предыдущей строки
        if fs < l["end"] <= start - 0.3:
            fs = min(start - 0.35, l["end"] + 0.05)
    fe = start + min_len
    for l in lines:
        if l["end"] <= start:
            continue
        if l["end"] - fs > max_len:
            break
        fe = l["end"]
        if fe - fs >= min_len and l.get("chorus") is False:
            break
    return (round(fs, 2), round(min(fe + 0.4, fs + max_len), 2))


def pick_fragment(lines: list[dict], max_len: float = 28.0) -> tuple[float, float]:
    """Отрывок для эдита: от начала припева, целыми строками, не длиннее max_len."""
    a, b = find_chorus(lines)
    if b < a:
        return (0.0, min(max_len, 20.0))
    start = max(0.0, lines[a]["start"] - 2.5)
    end = lines[a]["end"]
    for k in range(a, len(lines)):
        if lines[k]["end"] - start > max_len:
            break
        end = lines[k]["end"]
        if k >= b and end - start >= 15:
            break
    return (round(start, 2), round(end + 0.4, 2))
