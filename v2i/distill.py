"""提炼层：map（分块笔记）→ reduce（摘要 / 观点 / 文章），带缓存与引用校验。"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import render
from .config import Config
from .llm import LLM, LLMError, Usage, load_prompt
from .normalize import build_chunks
from .util import fmt_ts, now_iso, parse_ts, read_json, write_json, write_text

MODES = ("summary", "points", "article")
_CITE_RE = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")


def _log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- map


def _map_messages(meta: dict, chunk: dict) -> list[dict]:
    system = load_prompt("map_notes")
    user = (
        f"视频标题：{meta.get('title')}\n"
        f"作者：{meta.get('uploader') or '未知'}\n"
        f"时长：{meta.get('duration_text') or '未知'}\n"
        f"本块范围：{fmt_ts(chunk['start'])} - {fmt_ts(chunk['end'])}（第 {chunk['id']} 块）\n\n"
        f"逐字稿：\n{chunk.get('annotated') or chunk['text']}\n\n"
        "按系统提示的字段输出 JSON。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _clean_note(note: dict, chunk: dict) -> dict:
    """规范化模型输出：丢掉越界/非法时间码，保持字段完整。"""
    if not isinstance(note, dict):
        note = {}
    lo, hi = chunk["start"] - 10, chunk["end"] + 10

    def good_ts(value) -> str:
        seconds = parse_ts(str(value or ""))
        return fmt_ts(seconds) if seconds >= 0 and lo <= seconds <= hi else ""

    points = []
    for item in note.get("points") or []:
        if isinstance(item, str):
            item = {"claim": item}
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or "").strip()
        if not claim:
            continue
        points.append({
            "claim": claim,
            "evidence": str(item.get("evidence") or "").strip(),
            "ts": good_ts(item.get("ts")),
            "type": str(item.get("type") or "claim").strip().lower(),
        })
    quotes = []
    for item in note.get("quotes") or []:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            quotes.append({"text": text, "ts": good_ts(item.get("ts"))})

    return {
        "chunk_id": chunk["id"],
        "gist": str(note.get("gist") or "").strip()[:120],
        "points": points,
        "quotes": quotes[:3],
        "entities": [str(e).strip() for e in (note.get("entities") or []) if str(e).strip()][:12],
        "open_questions": [str(q).strip() for q in (note.get("open_questions") or []) if str(q).strip()][:6],
    }


def _digest(chunks: list[dict], notes: list[dict]) -> str:
    lines: list[str] = []
    for chunk, note in zip(chunks, notes):
        lines.append(f"## 块 {chunk['id']}  {fmt_ts(chunk['start'])} - {fmt_ts(chunk['end'])}")
        lines.append(f"要点: {note.get('gist') or '（无）'}")
        for p in note.get("points") or []:
            ev = f"：{p['evidence']}" if p.get("evidence") else ""
            ts = f" [{p['ts']}]" if p.get("ts") else ""
            lines.append(f"- （{p.get('type') or 'claim'}）{p['claim']}{ev}{ts}")
        for q in note.get("quotes") or []:
            ts = f" [{q['ts']}]" if q.get("ts") else ""
            lines.append(f"引用: “{q['text']}”{ts}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- reduce


def _reduce_messages(mode: str, meta: dict, digest: str) -> list[dict]:
    system = load_prompt(f"reduce_{mode}")
    hint = "只输出 JSON 对象。" if mode != "article" else "直接输出 Markdown 正文。"
    user = (
        f"视频标题：{meta.get('title')}\n"
        f"作者：{meta.get('uploader') or '未知'}\n"
        f"时长：{meta.get('duration_text') or '未知'}\n\n"
        f"分块笔记：\n{digest}\n\n{hint}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _check_citations(text: str, chunks: list[dict], tolerance: float = 3.0) -> list[str]:
    """返回落在任何分块时间范围之外的引用时间码（即无法回溯的引用）。"""
    spans = [(float(c["start"]) - tolerance, float(c["end"]) + tolerance) for c in chunks]
    bad = []
    for raw in set(_CITE_RE.findall(text or "")):
        seconds = parse_ts(raw)
        if seconds < 0 or not any(lo <= seconds <= hi for lo, hi in spans):
            bad.append(raw)
    return sorted(bad)


# ---------------------------------------------------------------- 主流程


def distill(workdir: Path, cfg: Config, modes: tuple[str, ...] = MODES,
            force: bool = False) -> dict:
    workdir = Path(workdir)
    transcript = read_json(workdir / "transcript.json")
    if not transcript or not transcript.get("segments"):
        raise RuntimeError(f"{workdir} 里没有 transcript.json，先跑 fetch / transcribe")
    meta = transcript.get("meta") or read_json(workdir / "meta.json", {}) or {}

    chunks = read_json(workdir / "chunks.json") or build_chunks(
        transcript["segments"], cfg.chunk_chars, cfg.overlap_sentences
    )
    write_json(workdir / "chunks.json", chunks)

    llm = LLM(cfg)
    _log(f"  提炼模型：{cfg.model} @ {cfg.base_url}")

    # ---- map（带缓存、带并发）
    notes_dir = workdir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    notes: list[dict] = [None] * len(chunks)  # type: ignore[list-item]
    todo: list[tuple[int, dict]] = []
    for idx, chunk in enumerate(chunks):
        cache = notes_dir / f"chunk-{chunk['id']:03d}.json"
        if cache.exists() and not force:
            notes[idx] = read_json(cache)
        else:
            todo.append((idx, chunk))

    if todo:
        _log(f"  map 阶段：{len(todo)} 块待处理（并发 {cfg.workers}）")
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
            futures = {
                pool.submit(llm.chat_json, _map_messages(meta, chunk)): (idx, chunk)
                for idx, chunk in todo
            }
            for future in as_completed(futures):
                idx, chunk = futures[future]
                try:
                    note = _clean_note(future.result(), chunk)
                except LLMError as exc:
                    raise LLMError(f"第 {chunk['id']} 块提炼失败：{exc}") from exc
                notes[idx] = note
                write_json(notes_dir / f"chunk-{chunk['id']:03d}.json", note)
                done += 1
                _log(f"    [{done}/{len(todo)}] 块 {chunk['id']} 完成（{len(note['points'])} 条要点）")
    else:
        _log(f"  map 阶段：{len(chunks)} 块全部命中缓存")

    digest = _digest(chunks, notes)
    write_text(workdir / "notes" / "digest.md", f"# {meta.get('title')}\n\n{digest}")

    # ---- reduce
    out_dir = workdir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}
    invalid: dict[str, list[str]] = {}

    for mode in modes:
        if mode == "summary":
            target = out_dir / "summary.json"
            cached = read_json(target) if target.exists() and not force else None
            if cached:
                data = cached
                _log("  summary 命中缓存")
            else:
                data = llm.chat_json(_reduce_messages("summary", meta, digest))
                write_json(target, data)
            md = render.summary_md(meta, data, cfg, transcript)
            path = out_dir / "summary.md"
            write_text(path, md)
            outputs[mode] = str(path)
            invalid[mode] = _check_citations(md, chunks)

        elif mode == "points":
            target = out_dir / "points.json"
            cached = read_json(target) if target.exists() and not force else None
            if cached:
                data = cached
                _log("  points 命中缓存")
            else:
                data = llm.chat_json(_reduce_messages("points", meta, digest))
                write_json(target, data)
            md = render.points_md(meta, data, cfg, transcript)
            path = out_dir / "points.md"
            write_text(path, md)
            outputs[mode] = str(path)
            invalid[mode] = _check_citations(md, chunks)

        elif mode == "article":
            path = out_dir / "article.md"
            if path.exists() and not force:
                body = path.read_text(encoding="utf-8")
                _log("  article 命中缓存")
                # 抹掉上次加的 frontmatter 再重渲染，避免叠加
                if body.startswith("---"):
                    parts = body.split("---", 2)
                    body = parts[2].lstrip("\n") if len(parts) > 2 else body
            else:
                body = llm.chat(_reduce_messages("article", meta, digest))
                body = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", body.strip())
            md = render.article_md(meta, body, cfg, transcript)
            write_text(path, md)
            outputs[mode] = str(path)
            invalid[mode] = _check_citations(md, chunks)

    # ---- 汇总 + manifest
    for mode, bad in invalid.items():
        if bad:
            _log(f"  ! {mode}: {len(bad)} 个时间码无法回溯（{', '.join(bad[:6])}）")

    summary_report = {
        "at": now_iso(),
        "model": cfg.model,
        "provider": cfg.provider,
        "chunks": len(chunks),
        "modes": list(modes),
        "usage": llm.usage.as_dict(),
        "outputs": outputs,
        "invalid_citations": invalid,
    }
    manifest = read_json(workdir / "manifest.json", {}) or {}
    manifest["distill"] = summary_report
    write_json(workdir / "manifest.json", manifest)

    digest_json = {
        "meta": meta,
        "stats": {
            "chunks": len(chunks),
            "points": sum(len(n.get("points") or []) for n in notes),
            "quotes": sum(len(n.get("quotes") or []) for n in notes),
            "tokens": llm.usage.total_tokens,
        },
        "notes": notes,
    }
    write_json(out_dir / "digest.json", digest_json)
    _log(f"  token 用量：{llm.usage.total_tokens}（{llm.usage.calls} 次调用）")
    return {"outputs": outputs, "usage": llm.usage.as_dict(), "invalid": invalid}
