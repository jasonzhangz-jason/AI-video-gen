"""渲染层：把结构化结果渲染成带 frontmatter 的 Markdown。"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .config import Config
from .util import fmt_ts, now_iso

__all__ = ["summary_md", "points_md", "article_md", "copy_md", "copy_txt"]


def _q(value) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def _display_url(url: str) -> str:
    """去掉跟踪参数（B 站链接能长到 800 字符），只留 scheme://host/path。"""
    url = str(url or "")
    try:
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.netloc:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        pass
    return url


def _frontmatter(meta: dict, cfg: Config, transcript: dict, extra: dict | None = None) -> str:
    source = transcript.get("source") or {}
    asr = transcript.get("asr") or {}
    if source.get("kind") == "subtitle":
        how = f"字幕直取（{source.get('lang') or '?'}）"
    else:
        how = f"faster-whisper {asr.get('model') or cfg.asr_model}（{asr.get('device') or cfg.device}/{asr.get('compute_type') or cfg.compute_type(cfg.device)}）"
    fields: list[tuple[str, str]] = [
        ("title", _q(meta.get("title"))),
        ("source", _q(_display_url(meta.get("webpage_url") or "") or meta.get("local_file") or "")),
        ("author", _q(meta.get("uploader") or "")),
        ("duration", _q(meta.get("duration_text") or "")),
        ("transcript", _q(how)),
        ("llm", _q(cfg.model)),
        ("generated", _q(now_iso())),
        ("tags", "[v2i, 视频笔记]"),
    ]
    for key, value in (extra or {}).items():
        fields.append((key, _q(value)))
    body = "\n".join(f"{k}: {v}" for k, v in fields)
    return f"---\n{body}\n---\n"


def _footer(meta: dict) -> str:
    url = meta.get("webpage_url") or ""
    local = meta.get("local_file") or ""
    stamp = now_iso()[:16].replace("T", " ")
    if url:
        shown = _display_url(url)
        origin = f"原文：[{shown}]({shown})"
    elif local:
        origin = f"本地文件：{Path(local).name}"
    else:
        origin = "本地文件"
    return f"\n---\n\n<sub>由 v2i 生成于 {stamp} ｜ {origin}</sub>\n"


def _ts(token) -> str:
    token = str(token or "").strip()
    return f" `[{token}]`" if token else ""


def summary_md(meta: dict, data: dict, cfg: Config, transcript: dict) -> str:
    title = (data.get("title") or meta.get("title") or "未命名视频").strip()
    out = [_frontmatter(meta, cfg, transcript), f"\n# {title}\n"]
    if data.get("one_liner"):
        out.append(f"\n> {data['one_liner']}\n")

    if data.get("summary"):
        out.append("\n## 摘要\n\n")
        out.append(str(data["summary"]).strip() + "\n")

    points = data.get("key_points") or []
    if points:
        out.append("\n## 要点\n\n")
        for item in points:
            if isinstance(item, str):
                out.append(f"- {item}\n")
            else:
                out.append(f"- {item.get('point', '').strip()}{_ts(item.get('ts'))}\n")

    timeline = data.get("timeline") or []
    if timeline:
        out.append("\n## 时间轴\n\n")
        for item in timeline:
            if isinstance(item, str):
                out.append(f"- {item}\n")
            else:
                out.append(f"- **{item.get('ts', '')}** — {item.get('event', '').strip()}\n")

    quotes = data.get("quotes") or []
    if quotes:
        out.append("\n## 金句\n\n")
        for item in quotes:
            text = item.get("text") if isinstance(item, dict) else str(item)
            ts = item.get("ts") if isinstance(item, dict) else ""
            out.append(f"> {text}{_ts(ts)}\n\n")
        out.append("")

    actions = data.get("actions") or []
    if actions:
        out.append("\n## 可以做什么\n\n")
        for item in actions:
            out.append(f"- {item}\n")

    caveats = data.get("caveats") or []
    if caveats:
        out.append("\n## 存疑 / 需核实\n\n")
        for item in caveats:
            out.append(f"- {item}\n")

    out.append(_footer(meta))
    return "".join(out)


def points_md(meta: dict, data: dict, cfg: Config, transcript: dict | None = None) -> str:
    transcript = transcript or {"source": {"kind": "asr"}, "asr": {}}
    title = meta.get("title") or "未命名视频"
    out = [_frontmatter(meta, cfg, transcript), f"\n# 观点清单：{title}\n"]
    if data.get("thesis"):
        out.append(f"\n**核心主张**：{data['thesis']}\n")

    points = data.get("points") or []
    if points:
        out.append("\n## 论点（按承重排序）\n")
    for i, item in enumerate(points, 1):
        if isinstance(item, str):
            out.append(f"\n### {i}. {item}\n")
            continue
        claim = str(item.get("claim") or "").strip()
        out.append(f"\n### {i}. {claim}\n\n")
        if item.get("evidence"):
            out.append(f"- **论据**：{item['evidence']}\n")
        kind = item.get("type") or "claim"
        strength = item.get("strength") or "medium"
        out.append(f"- **类型**：{kind} ｜ **强度**：{strength}\n")
        if item.get("ts"):
            out.append(f"- **位置**：`[{item['ts']}]`\n")
        if item.get("counter"):
            out.append(f"- **可能的反驳**：{item['counter']}\n")

    structure = data.get("structure") or []
    if structure:
        out.append("\n## 结构\n\n| 段落 | 时间 | 内容 |\n|---|---|---|\n")
        for part in structure:
            out.append(
                f"| {part.get('part', '')} | {part.get('from', '')}–{part.get('to', '')} "
                f"| {part.get('gist', '')} |\n"
            )

    blind = data.get("blind_spots") or []
    if blind:
        out.append("\n## 盲区\n\n")
        for item in blind:
            out.append(f"- {item}\n")

    if data.get("verdict"):
        out.append(f"\n## 判断\n\n{data['verdict']}\n")

    out.append(_footer(meta))
    return "".join(out)


def article_md(meta: dict, body: str, cfg: Config, transcript: dict | None = None) -> str:
    transcript = transcript or {"source": {"kind": "asr"}, "asr": {}}
    body = (body or "").strip()
    chars = len(body)
    return _frontmatter(meta, cfg, transcript, {"chars": str(chars)}) + f"\n{body}\n" + _footer(meta)

def copy_md(meta: dict, data: dict, cfg: Config, transcript: dict | None = None) -> str:
    """文案：去时间码、去口水词的成稿；有 key 时是润色版，没有就是原始整理版。"""
    transcript = transcript or {"source": {"kind": "asr"}, "asr": {}}
    paras = [str(p).strip() for p in (data.get("paras") or []) if str(p).strip()]
    title = (data.get("title") or meta.get("title") or "未命名视频").strip()
    chars = sum(len(p) for p in paras)
    style = data.get("style") or "light"
    labels = {"raw": "原样", "light": "轻度整理", "script": "口播稿"}
    style_label = data.get("style_label") or labels.get(style, style)
    if data.get("polished"):
        state = "已润色"
    elif style == "raw":
        state = "未润色（原样）"
    else:
        state = "未润色（退回原样）"

    out = [_frontmatter(meta, cfg, transcript, {"kind": "文案", "style": style_label, "chars": str(chars)}),
           f"\n# {title}\n"]
    out.append(f"\n> 文案 · {style_label} · {state} · {len(paras)} 段 · 约 {chars} 字\n")
    if not data.get("polished") and data.get("note"):
        out.append(f"\n> ⚠ 未润色：{data['note']}\n")
    for para in paras:
        out.append(f"\n{para}\n")

    cuts = data.get("cuts") or []
    if cuts:
        out.append("\n## 整理说明\n\n")
        for item in cuts:
            out.append(f"- {item}\n")

    keep = data.get("keep_as_is") or []
    if keep:
        out.append("\n## 保留原样\n\n")
        for item in keep:
            out.append(f"- {item}\n")

    out.append(_footer(meta))
    return "".join(out)


def copy_txt(data: dict) -> str:
    """纯文本版：不带 frontmatter 和 Markdown 标记，直接可复制粘贴。"""
    paras = [str(p).strip() for p in (data.get("paras") or []) if str(p).strip()]
    title = str(data.get("title") or "").strip()
    head = f"{title}\n\n" if title else ""
    return head + "\n\n".join(paras) + "\n"
