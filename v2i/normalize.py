"""规范化层：断句、按字数分块、保留时间锚点，产出 transcript.md / .srt / chunks.json。"""
from __future__ import annotations

import re
from pathlib import Path

from .util import clean_cjk, fmt_ts, write_json, write_text

# 句末标点（中英混排都覆盖）
_END_RE = re.compile(r"[^。！？!?；;…\n]+[。！？!?；;…]*")
_STRIP_RE = re.compile(r"[\s\u200b\ufeff]+")


def _chop_long(piece: str, limit: int = 100) -> list[str]:
    """无标点长句（歌词、口播不停顿）按逗号/空格切成可读的小块。"""
    if len(piece) <= limit:
        return [piece]
    out: list[str] = []
    for part in re.split(r"(?<=[，,、])", piece):
        part = part.strip()
        if not part:
            continue
        if len(part) <= limit + 20:
            out.append(part)
            continue
        buf = ""
        for word in part.split(" "):
            if not word:
                continue
            if buf and len(buf) + len(word) + 1 > limit:
                out.append(buf)
                buf = word
            else:
                buf = f"{buf} {word}".strip()
        if buf:
            out.append(buf)
    return out or [piece]


def _split_sentence(text: str) -> list[str]:
    pieces = [p.strip() for p in _END_RE.findall(text) if p.strip()]
    out: list[str] = []
    for piece in pieces:
        out.extend(_chop_long(piece))
    return out or ([text.strip()] if text.strip() else [])


def _join_units(units: list[dict]) -> str:
    """拼回一段话：中英相邻处补空格，避免 'loveYou' 这种粘连。"""
    out = ""
    for unit in units:
        text = unit["text"]
        if not text:
            continue
        if out and not out.endswith((" ", "\n")):
            prev, nxt = out[-1], text[0]
            if (prev.isascii() and prev.isalnum()) or (nxt.isascii() and nxt.isalnum()):
                out += " "
        out += text
    return out


def _sentence_units(segments: list[dict]) -> list[dict]:
    """把 segment 拆成句子，并按字数比例分配时间。"""
    units: list[dict] = []
    for seg in segments:
        text = clean_cjk(_STRIP_RE.sub(" ", str(seg.get("text") or "")).strip())
        if not text:
            continue
        start, end = float(seg["start"]), float(seg["end"])
        span = max(end - start, 0.2)
        sentences = _split_sentence(text)
        total_chars = sum(len(s) for s in sentences) or 1
        cursor = start
        for sentence in sentences:
            share = span * len(sentence) / total_chars
            units.append({
                "start": round(cursor, 2),
                "end": round(min(end, cursor + share), 2),
                "text": sentence,
            })
            cursor += share
    return units


def build_chunks(segments: list[dict], chunk_chars: int = 1600, overlap: int = 1) -> list[dict]:
    units = _sentence_units(segments)
    if not units:
        return []
    n = len(units)

    # 1) 先按字数切出互不重叠的区间
    groups: list[tuple[int, int]] = []
    cursor = 0
    while cursor < n:
        end, chars = cursor, 0
        while end < n and (chars < chunk_chars or end == cursor):
            chars += len(units[end]["text"])
            end += 1
        groups.append((cursor, end))
        cursor = end

    # 2) 尾块太小就并进上一块，避免为几十个字单独调一次模型
    if len(groups) > 1:
        start, end = groups[-1]
        tail = sum(len(units[i]["text"]) for i in range(start, end))
        if tail < chunk_chars * 0.35:
            groups.pop()
            groups[-1] = (groups[-1][0], end)

    # 3) 渲染成块，块首带上一块的末尾几句做重叠上下文
    chunks: list[dict] = []
    for index, (start, end) in enumerate(groups):
        begin = max(0, start - overlap) if index > 0 and overlap > 0 else start
        buffer = units[begin:end]
        chunks.append({
            "id": index + 1,
            "start": buffer[0]["start"],
            "end": buffer[-1]["end"],
            "chars": sum(len(u["text"]) for u in units[start:end]),
            "text": _join_units(buffer),
            # 带时间锚点的版本，直接喂给 map 阶段，便于模型引用
            "annotated": " ".join(f"[{fmt_ts(u['start'])}] {u['text']}" for u in buffer),
        })
    return chunks


def to_srt(segments: list[dict]) -> str:
    def stamp(seconds: float) -> str:
        ms = int(round(float(seconds) * 1000))
        h, rem = divmod(ms, 3600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = []
    for i, seg in enumerate(segments, 1):
        blocks.append(f"{i}\n{stamp(seg['start'])} --> {stamp(seg['end'])}\n{seg['text']}\n")
    return "\n".join(blocks)


def to_markdown(meta: dict, segments: list[dict], source: dict) -> str:
    title = meta.get("title") or "未命名视频"
    lines = [
        f"# {title} — 逐字稿",
        "",
        f"- 来源：{meta.get('webpage_url') or ''}",
        f"- 作者：{meta.get('uploader') or '未知'}",
        f"- 时长：{meta.get('duration_text') or fmt_ts(meta.get('duration') or 0)}",
        f"- 取词方式：{'字幕直取（' + str(source.get('lang')) + '）' if source.get('kind') == 'subtitle' else '本地转写'}",
        "",
    ]
    # 一条 segment 一行：既好读，也方便按时间定位回原文
    for seg in segments:
        lines.append(f"**[{fmt_ts(seg['start'])}]** {seg['text']}")
        lines.append("")
    return "\n".join(lines)


def normalize(transcript: dict, workdir: Path, chunk_chars: int = 1600, overlap: int = 1) -> list[dict]:
    segments = transcript.get("segments") or []
    if not segments:
        raise ValueError("transcript 没有 segments，先跑 fetch / transcribe")
    chunks = build_chunks(segments, chunk_chars=chunk_chars, overlap=overlap)
    write_json(workdir / "chunks.json", chunks)
    write_text(workdir / "transcript.srt", to_srt(segments))
    write_text(workdir / "transcript.md",
               to_markdown(transcript.get("meta", {}), segments, transcript.get("source", {})))
    total_chars = sum(len(s.get("text") or "") for s in segments)
    print(f"  分块完成：{len(chunks)} 块 / 共 {total_chars} 字", flush=True)
    return chunks
