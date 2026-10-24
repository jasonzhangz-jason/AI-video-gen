"""运行时设置：写进项目 `.runtime/settings.json`，改完立即生效（热加载）。

优先级（高 → 低）：
1. 操作系统环境变量（显式设的，例如 CI 里注入的）
2. 本文件的运行时设置（GUI「设置」页写的）
3. 项目 `.env`

之所以要这一层：`.env` 只在进程启动时读一次，且旧的 `load_dotenv` 会把空值也写进
`os.environ`，导致之后往 `.env` 里补 key 永远不生效。运行时设置每次构造 Config 都会
重新读盘并覆盖 `os.environ`，所以不用重启。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / ".runtime" / "settings.json"

# 允许通过设置页写入的键（白名单，避免变成任意环境变量注入）
ALLOWED_KEYS = {
    # 大模型
    "V2I_API_KEY",
    "V2I_PROVIDER",
    "V2I_MODEL",
    "V2I_BASE_URL",
    # 网络
    "V2I_PROXY",
    "V2I_COOKIES",
    "V2I_COOKIES_FROM_BROWSER",
    # 转写
    "V2I_ASR_MODEL",
    "V2I_ASR_DEVICE",
    "V2I_ASR_COMPUTE",
    "V2I_LANGUAGE",
    # 提炼 / 文案
    "V2I_COPY_STYLE",
    "V2I_CHUNK_CHARS",
}

# 记录"哪些键是运行时设置写进去的"，删除时要能从 os.environ 撤掉
_applied: set[str] = set()


def load() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: str(v) for k, v in data.items() if k in ALLOWED_KEYS and str(v).strip()}


def save(patch: dict) -> dict:
    """合并写入；值为空字符串表示删除该键。"""
    current = load()
    for key, value in patch.items():
        if key not in ALLOWED_KEYS:
            raise ValueError(f"不允许的设置项：{key}")
        text = "" if value is None else str(value).strip()
        if text:
            current[key] = text
        else:
            current.pop(key, None)

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(SETTINGS_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(current, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return current


def apply() -> dict:
    """把运行时设置刷进 os.environ（每次构造 Config 都调用，保证热加载）。"""
    data = load()
    for key, value in data.items():
        os.environ[key] = value
        _applied.add(key)
    for key in list(_applied):
        if key not in data:
            os.environ.pop(key, None)
            _applied.discard(key)
    return data


def mask(secret: str | None) -> str:
    secret = (secret or "").strip()
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{secret[-4:]}"
