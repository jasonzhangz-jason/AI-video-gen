"""文案改写历史：存 `.runtime/rewrite_history.json`，供界面回查与复用。

只保留最近 N 条，每条记录输入/输出全文（方便"再改一次"），写入用原子替换，
进程内用一把锁串行化（HTTP 是多线程的）。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HISTORY_PATH = PROJECT_ROOT / ".runtime" / "rewrite_history.json"

MAX_ITEMS = 60
MAX_TEXT = 40_000

_lock = threading.Lock()


def _read() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write(items: list[dict]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(HISTORY_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(items, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, HISTORY_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _style_label(style: str | None) -> str:
    from .rewrite_styles import label

    return label(style or "") if style else ""


def _meta(item: dict) -> dict:
    """列表用的轻量结构：不带全文，只给摘要。"""
    output = item.get("output") or ""
    return {
        "id": item.get("id"),
        "at": item.get("at"),
        "style": item.get("style"),
        "style_label": _style_label(item.get("style")),
        "model": item.get("model"),
        "tokens": item.get("tokens"),
        "in_chars": item.get("in_chars"),
        "out_chars": item.get("out_chars"),
        "preview": output[:70].replace("\n", " "),
    }


def add(*, style: str, model: str, tokens: int, input_text: str, output_text: str) -> dict:
    item = {
        "id": f"rw{int(time.time() * 1000)}",
        "at": time.strftime("%m-%d %H:%M:%S", time.localtime()),
        "style": style,
        "model": model,
        "tokens": tokens,
        "in_chars": len(input_text),
        "out_chars": len(output_text),
        "input": input_text[:MAX_TEXT],
        "output": output_text[:MAX_TEXT],
    }
    with _lock:
        items = _read()
        items.insert(0, item)
        _write(items[:MAX_ITEMS])
    return item


def listing(limit: int = 30) -> list[dict]:
    with _lock:
        items = _read()
    return [_meta(item) for item in items[:limit]]


def get(item_id: str) -> dict | None:
    with _lock:
        items = _read()
    for item in items:
        if item.get("id") == item_id:
            return item
    return None


def delete(item_id: str) -> bool:
    with _lock:
        items = _read()
        kept = [item for item in items if item.get("id") != item_id]
        if len(kept) == len(items):
            return False
        _write(kept)
    return True


def clear() -> int:
    with _lock:
        items = _read()
        _write([])
    return len(items)
