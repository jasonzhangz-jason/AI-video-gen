"""小工具：时间码、JSON 读写、文件名清洗。"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def fmt_ts(seconds: float) -> str:
    """431.2 -> '07:11'；超过 1 小时 -> '01:07:11'。"""
    seconds = max(0, int(round(float(seconds or 0))))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_ts(text: str) -> float:
    """'07:11' / '01:07:11' -> 431.0 / 4031.0"""
    parts = [p for p in str(text).strip().split(":") if p != ""]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return -1.0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return -1.0


def human_duration(seconds: float | None) -> str:
    if not seconds:
        return "未知"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 40) -> str:
    """保留中英文与数字，其余转 -，用于目录名。"""
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "-", str(text or "").strip())
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-.")
    return (text[:max_len] or "video").strip("-.")


def read_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data) -> None:
    """原子写，避免中断后留下半个 JSON。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def tail(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"

# ---------------------------------------------------------------- 中文文本

_PUNCT_MAP = {",": "，", ".": "。", "?": "？", "!": "！", ";": "；", ":": "：", "~": "～"}
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# Whisper 的中文偶尔会吐繁体（small/base 尤其明显），装了 opencc 就统一转简体
try:
    from opencc import OpenCC

    _T2S = OpenCC("t2s")
except Exception:  # 没装就跳过，不影响主流程
    _T2S = None


def to_simplified(text: str) -> str:
    if _T2S is None or not _CJK_RE.search(text or ""):
        return text
    try:
        return _T2S.convert(text)
    except Exception:
        return text


def fix_cjk_punct(text: str) -> str:
    """半角标点夹在汉字之间时换成全角（不动数字、英文和 URL）。"""
    def repl(match: re.Match) -> str:
        prev, mark, nxt = match.group(1), match.group(2), match.group(3) or ""
        if _CJK_RE.match(prev) and (not nxt or _CJK_RE.match(nxt) or nxt.isspace()):
            return prev + _PUNCT_MAP[mark] + nxt
        return match.group(0)

    return re.sub(r"(.)([,\.\?!;:~])(.)?", repl, text or "")


def clean_cjk(text: str) -> str:
    """转写文本的统一清洗入口：半角标点 -> 全角，繁体 -> 简体。"""
    return to_simplified(fix_cjk_punct(text or ""))

def display_url(url: str) -> str:
    """去掉跟踪参数：B 站分享链接能长到 800 字符，渲染出来会撑爆版面。"""
    url = str(url or "")
    try:
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.netloc:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        pass
    return url


_URL_RE = __import__("re").compile(r"https?://[^\s<>'\"()（）【】]+")


def extract_url(text: str) -> str:
    """从"分享文案 + 链接"里抽出第一个 http(s) 链接。

    抖音/快手的分享都是「文案 + 短链 + 复制提示」，直接把整段贴进来很常见，
    这里统一兜住；抽不到就返回去掉首尾空白的原文。
    """
    raw = (text or "").strip()
    m = _URL_RE.search(raw)
    if not m:
        return raw
    url = m.group(0).rstrip("，。！？、；：,.!?;:")
    return url
