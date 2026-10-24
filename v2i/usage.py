"""大模型调用记账：每次调用写一行 JSONL，按任务聚合出 token 与费用摘要。

价格是**估算**，按各家公开价折算成人民币（美元按固定汇率 7.2），单位：元 / 百万 token。
要改价直接设环境变量 `V2I_PRICE_TABLE`（JSON: {"model": [输入价, 输出价]}），或用
`V2I_PRICE_OVERRIDE` 单点覆盖（JSON: {"model": {"in": 1.0, "out": 2.0}}）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = PROJECT_ROOT / ".runtime" / "llm_calls.jsonl"
USD_CNY = 7.2

# 元 / 百万 token（输入, 输出）
_DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "deepseek-chat": (2.0, 8.0),
    "deepseek-reasoner": (4.0, 16.0),
    "deepseek-coder": (2.0, 8.0),
    "moonshot-v1-8k": (12.0, 12.0),
    "moonshot-v1-32k": (24.0, 24.0),
    "gpt-4o-mini": (0.15 * USD_CNY, 0.60 * USD_CNY),
    "gpt-4o": (2.5 * USD_CNY, 10.0 * USD_CNY),
    "qwen-plus": (0.8, 2.0),
    "qwen-turbo": (0.3, 0.6),
    "mock-model": (0.0, 0.0),
}

_lock = threading.Lock()


def price_table() -> dict[str, tuple[float, float]]:
    table = dict(_DEFAULT_PRICES)
    raw = os.environ.get("V2I_PRICE_TABLE")
    if raw:
        try:
            for model, pair in json.loads(raw).items():
                table[model] = (float(pair[0]), float(pair[1]))
        except (ValueError, TypeError, IndexError):
            pass
    raw_one = os.environ.get("V2I_PRICE_OVERRIDE")
    if raw_one:
        try:
            for model, spec in json.loads(raw_one).items():
                table[model] = (float(spec["in"]), float(spec["out"]))
        except (ValueError, TypeError, KeyError):
            pass
    return table


def price_for(model: str) -> tuple[float, float] | None:
    table = price_table()
    if model in table:
        return table[model]
    # 前缀匹配：deepseek-chat-0324 之类
    for key, value in table.items():
        if model.startswith(key):
            return value
    return None


def estimate(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    pair = price_for(model)
    if pair is None:
        return None
    return round(prompt_tokens / 1e6 * pair[0] + completion_tokens / 1e6 * pair[1], 6)


def log_call(*, task: str, stage: str, model: str,
             prompt_tokens: int, completion_tokens: int,
             seconds: float = 0.0, ok: bool = True) -> dict:
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "task": task or "",
        "stage": stage or "chat",
        "model": model or "",
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "cost": estimate(model, prompt_tokens, completion_tokens),
        "seconds": round(float(seconds or 0.0), 2),
        "ok": bool(ok),
    }
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _lock:
        with LOG_PATH.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    return record


def load_calls(limit: int = 20_000) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    rows: list[dict] = []
    try:
        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows[-limit:]


def summarize() -> dict:
    """按任务聚合；没有逐次记录的旧任务用 manifest 里的汇总兜底。"""
    calls = load_calls()
    tasks: dict[str, dict] = {}
    for row in calls:
        name = row.get("task") or "（未知任务）"
        item = tasks.setdefault(name, {
            "task": name, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "cost": 0.0, "priced": True, "last": "", "detail": [], "fallback": False,
        })
        item["calls"] += 1
        item["prompt_tokens"] += row.get("prompt_tokens", 0)
        item["completion_tokens"] += row.get("completion_tokens", 0)
        if row.get("cost") is None:
            item["priced"] = False
        else:
            item["cost"] += row["cost"]
        item["last"] = max(item["last"], row.get("ts") or "")
        item["detail"].append({
            "ts": row.get("ts"), "stage": row.get("stage"), "model": row.get("model"),
            "prompt_tokens": row.get("prompt_tokens", 0),
            "completion_tokens": row.get("completion_tokens", 0),
            "cost": row.get("cost"), "seconds": row.get("seconds", 0),
        })

    # 旧任务：manifest.distill.usage 只有总量，补一条"汇总"记录
    runs_dir = PROJECT_ROOT / "runs"
    if runs_dir.exists():
        for run in runs_dir.iterdir():
            if not run.is_dir() or run.name.startswith("_") or run.name in tasks:
                continue
            manifest = {}
            try:
                manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            distill = (manifest or {}).get("distill") or {}
            usage = distill.get("usage") or {}
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            if not prompt and not completion:
                continue
            model = distill.get("model") or ""
            cost = estimate(model, prompt, completion)
            tasks[run.name] = {
                "task": run.name,
                "calls": int(usage.get("calls") or 0),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cost": cost or 0.0,
                "priced": cost is not None,
                "last": (distill.get("at") or "")[:19].replace("T", " "),
                "detail": [{
                    "ts": distill.get("at"), "stage": "提炼（汇总，无逐次记录）", "model": model,
                    "prompt_tokens": prompt, "completion_tokens": completion,
                    "cost": cost, "seconds": 0,
                }],
                "fallback": True,
            }

    order = sorted(tasks.values(), key=lambda x: x["last"], reverse=True)
    totals = {
        "calls": sum(t["calls"] for t in order),
        "prompt_tokens": sum(t["prompt_tokens"] for t in order),
        "completion_tokens": sum(t["completion_tokens"] for t in order),
        "cost": round(sum(t["cost"] for t in order), 4),
        "tasks": len(order),
        "unpriced": sum(1 for t in order if not t["priced"]),
    }
    return {"tasks": order, "totals": totals}
