"""提炼层：map（分块笔记）→ reduce（摘要 / 观点 / 文章），带缓存与引用校验。"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import render
from .config import Config, ConfigError
from .llm import LLM, LLMError, Usage, load_prompt
from .normalize import build_chunks, to_paragraphs
from .util import fmt_ts, now_iso, parse_ts, read_json, write_json, write_text

MODES = ("summary", "points", "article", "copy")

# 文案风格：原样（纯确定性）/ 轻度整理 / 口播稿
COPY_STYLES = {
    "raw": ("原样", None),
    "light": ("轻度整理", "reduce_copy"),
    "script": ("口播稿", "reduce_copy_script"),
}
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


def _copy_messages(meta: dict, paras: list[str], part_label: str = "",
                   prompt: str = "reduce_copy") -> list[dict]:
    system = load_prompt(prompt)
    text = "\n\n".join(paras)
    label = f"（{part_label}）" if part_label else ""
    user = (
        f"视频标题：{meta.get('title')}\n"
        f"作者：{meta.get('uploader') or '未知'}\n\n"
        f"逐字稿{label}：\n{text}\n\n只输出 JSON 对象。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _polish_copy(llm, meta: dict, paras: list[str], prompt: str = "reduce_copy") -> dict:
    """文案润色：短稿一次过；长稿按 9000 字分片，保序拼接。"""
    text = "\n\n".join(paras)
    if len(text) <= 12000:
        return llm.chat_json(_copy_messages(meta, paras, prompt=prompt), stage="copy")

    parts: list[str] = []
    buffer = ""
    for paragraph in paras:
        if buffer and len(buffer) + len(paragraph) > 9000:
            parts.append(buffer)
            buffer = ""
        buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
    if buffer:
        parts.append(buffer)

    merged: dict = {"title": "", "paras": [], "cuts": [], "keep_as_is": []}
    for index, part in enumerate(parts, 1):
        data = llm.chat_json(_copy_messages(meta, [part], f"第 {index}/{len(parts)} 段", prompt=prompt),
                             stage=f"copy {index}/{len(parts)}")
        merged["title"] = merged["title"] or (data.get("title") or "")
        merged["paras"].extend(str(p).strip() for p in (data.get("paras") or []) if str(p).strip())
        merged["cuts"].extend(data.get("cuts") or [])
        merged["keep_as_is"].extend(data.get("keep_as_is") or [])
    return merged


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

    llm = None
    llm_error = ""
    try:
        llm = LLM(cfg)
        llm.task = workdir.name
    except ConfigError as exc:
        llm_error = str(exc)
        _log(f"  ! 没有可用的 LLM 配置：{exc}")
        if "copy" not in modes:
            raise
        _log("    → 只产出「文案」的原始整理版（去时间码/口水词），其余模式跳过")
    if llm is not None:
        _log(f"  提炼模型：{cfg.model} @ {cfg.base_url}")

    # ---- map（带缓存、带并发）
    notes_dir = workdir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    notes: list[dict] = [None] * len(chunks)  # type: ignore[list-item]
    todo: list[tuple[int, dict]] = []
    if llm is None:
        notes = [{"chunk_id": c["id"], "gist": "", "points": [], "quotes": [],
                  "entities": [], "open_questions": []} for c in chunks]
    for idx, chunk in enumerate(chunks):
        cache = notes_dir / f"chunk-{chunk['id']:03d}.json"
        if cache.exists() and not force:
            notes[idx] = read_json(cache)
        else:
            todo.append((idx, chunk))

    if todo and llm is not None:
        _log(f"  map 阶段：{len(todo)} 块待处理（并发 {cfg.workers}）")
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
            futures = {
                pool.submit(llm.chat_json, _map_messages(meta, chunk), stage=f"map #{idx + 1}"): (idx, chunk)
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
    elif llm is not None:
        _log(f"  map 阶段：{len(chunks)} 块全部命中缓存")

    digest = _digest(chunks, notes)
    write_text(workdir / "notes" / "digest.md", f"# {meta.get('title')}\n\n{digest}")

    # ---- reduce
    out_dir = workdir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}
    invalid: dict[str, list[str]] = {}
    skipped: list[str] = []

    for mode in modes:
        if mode == "summary":
            target = out_dir / "summary.json"
            cached = read_json(target) if target.exists() and not force else None
            if cached:
                data = cached
                _log("  summary 命中缓存")
            elif llm is None:
                skipped.append("summary")
                continue
            else:
                data = llm.chat_json(_reduce_messages("summary", meta, digest), stage="reduce:summary")
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
            elif llm is None:
                skipped.append("points")
                continue
            else:
                data = llm.chat_json(_reduce_messages("points", meta, digest), stage="reduce:points")
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
            elif llm is None:
                skipped.append("article")
                continue
            else:
                body = llm.chat(_reduce_messages("article", meta, digest), stage="reduce:article")
                body = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", body.strip())
            md = render.article_md(meta, body, cfg, transcript)
            write_text(path, md)
            outputs[mode] = str(path)
            invalid[mode] = _check_citations(md, chunks)

        elif mode == "copy":
            style = getattr(cfg, "copy_style", "light") or "light"
            if style not in COPY_STYLES:
                _log(f"  ! 未知文案风格 {style!r}，按 light 处理")
                style = "light"
            style_label, prompt_name = COPY_STYLES[style]
            target = out_dir / "copy.json"
            cached = read_json(target) if target.exists() and not force else None
            if cached and cached.get("style") == style:
                data = cached
                _log(f"  copy 命中缓存（{style_label}）")
            else:
                base_paras = to_paragraphs(transcript["segments"])
                data = {
                    "title": meta.get("title") or "",
                    "paras": base_paras,
                    "polished": False,
                    "style": style,
                    "style_label": style_label,
                    "cuts": [],
                    "keep_as_is": [],
                    "note": llm_error,
                }
                if prompt_name is None:
                    _log("  文案：原样（不做润色）")
                elif llm is None:
                    _log(f"  文案：{style_label} 需要模型，缺配置 → 退回原样")
                else:
                    try:
                        polished = _polish_copy(llm, meta, base_paras, prompt_name)
                        paras = [str(p).strip() for p in (polished.get("paras") or []) if str(p).strip()]
                        if paras:
                            data.update({
                                "title": polished.get("title") or data["title"],
                                "paras": paras,
                                "cuts": polished.get("cuts") or [],
                                "keep_as_is": polished.get("keep_as_is") or [],
                                "polished": True,
                                "note": "",
                            })
                    except LLMError as exc:
                        _log(f"  ! 文案改写失败，退回原样：{exc}")
                        data["note"] = str(exc)
                write_json(target, data)
            md = render.copy_md(meta, data, cfg, transcript)
            path = out_dir / "copy.md"
            write_text(path, md)
            write_text(out_dir / "copy.txt", render.copy_txt(data))
            outputs[mode] = str(path)
            invalid[mode] = []
            tail = "已润色" if data.get("polished") else "未润色"
            _log(f"  文案：{len(data.get('paras') or [])} 段 · {data.get('style_label') or style_label}（{tail}）")

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
        "usage": llm.usage.as_dict() if llm else {"calls": 0, "failures": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "outputs": outputs,
        "invalid_citations": invalid,
        "skipped": skipped,
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
            "tokens": llm.usage.total_tokens if llm else 0,
        },
        "notes": notes,
    }
    write_json(out_dir / "digest.json", digest_json)
    if llm is not None:
        _log(f"  token 用量：{llm.usage.total_tokens}（{llm.usage.calls} 次调用）")
    usage = llm.usage.as_dict() if llm else {"calls": 0, "failures": 0, "prompt_tokens": 0,
                                            "completion_tokens": 0, "total_tokens": 0}
    return {"outputs": outputs, "usage": usage, "invalid": invalid, "skipped": skipped}
