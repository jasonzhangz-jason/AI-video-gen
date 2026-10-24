"""下载层（fetch）：字幕优先，音频兜底。

设计要点：
- 先用 extract_info(download=False) 探测字幕，有字幕就只下字幕，省掉下载与转写；
- 字幕解析成统一的 segments: [{start, end, text}]，与 ASR 产物同构；
- 没字幕才下 bestaudio（m4a/webm 均可，PyAV 直接解码，不强依赖 ffmpeg）。
"""
from __future__ import annotations

import html
import re
import time
from pathlib import Path

from .config import Config
from .util import fmt_ts, now_iso, read_json, write_json

# 字幕语言优先级：中文优先，其次英文；具体命中哪个由视频实际提供的字幕决定
LANG_PRIORITY = [
    "zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "zh-HK",
    "en", "en-US", "en-GB", "ja",
]

_META_FIELDS = (
    "id", "title", "fulltitle", "uploader", "channel", "uploader_id",
    "duration", "webpage_url", "original_url", "thumbnail", "upload_date",
    "view_count", "like_count", "live_status", "extractor_key", "age_limit",
)


class FetchError(RuntimeError):
    pass


def _log(msg: str) -> None:
    print(msg, flush=True)


def _base_opts(cfg: Config, fast: bool = False) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 1 if fast else 5,
        "fragment_retries": 1 if fast else 5,
        "socket_timeout": 15 if fast else 30,
        "ignoreerrors": False,
        "no_color": True,
    }
    if cfg.ffmpeg_exe:
        opts["ffmpeg_location"] = cfg.ffmpeg_exe
    if cfg.cookies and Path(cfg.cookies).exists():
        opts["cookiefile"] = str(cfg.cookies)
    elif getattr(cfg, "cookies_from_browser", None):
        # 直接复用浏览器登录态：抖音/小红书/X 这类站点几乎必须
        opts["cookiesfrombrowser"] = (str(cfg.cookies_from_browser),)
    if cfg.proxy:
        opts["proxy"] = cfg.proxy
    if cfg.max_height:
        opts["format"] = f"bestaudio/best[height<={cfg.max_height}]/best"
    return opts


def _clean_meta(info: dict) -> dict:
    meta = {k: info.get(k) for k in _META_FIELDS}
    meta["title"] = (info.get("title") or info.get("id") or "未命名视频").strip()
    desc = (info.get("description") or "").strip()
    meta["description"] = desc[:2000]
    chapters = []
    for ch in info.get("chapters") or []:
        chapters.append({
            "start": round(float(ch.get("start_time") or 0), 1),
            "title": (ch.get("title") or "").strip(),
        })
    meta["chapters"] = chapters
    meta["fetched_at"] = now_iso()
    meta["duration_text"] = fmt_ts(meta.get("duration") or 0)
    return meta


def _pick_subtitle(info: dict) -> tuple[str, str, str] | None:
    """返回 (kind, lang, ext)：kind 为 subtitles（人工）或 automatic_captions（自动）。

    只认 vtt / json3 / srt —— B 站的 danmaku(xml) 也挂在 subtitles 下，但那是弹幕不是字幕。
    """
    for kind in ("subtitles", "automatic_captions"):
        table = info.get(kind) or {}
        if not table:
            continue
        for lang in LANG_PRIORITY:
            if lang not in table:
                continue
            formats = table[lang] or []
            exts = {f.get("ext") for f in formats}
            for want in ("vtt", "json3", "srt"):
                if want in exts:
                    return kind, lang, want
    return None


def probe(url: str, cfg: Config, fast: bool = False) -> dict:
    import yt_dlp

    opts = _base_opts(cfg, fast=fast)
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            raise FetchError(f"解析失败：{friendly_error(exc)}") from exc
    if info is None:
        raise FetchError("解析失败：站点返回空结果（可能需要 cookies 或该内容不可公开访问）")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise FetchError("播放列表为空")
        info = entries[0]
    return info


def _download_subtitle(url: str, cfg: Config, workdir: Path, lang: str, ext: str) -> Path | None:
    import yt_dlp

    opts = _base_opts(cfg)
    opts.update({
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang],
        "subtitlesformat": ext,
        "outtmpl": {
            "default": str(workdir / "source.%(ext)s"),
            "subtitle": str(workdir / "subs.%(ext)s"),
        },
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            ydl.extract_info(url, download=True)
        except Exception as exc:  # noqa: BLE001
            _log(f"  ! 字幕下载失败（{exc}），回落到音频转写")
            return None
    hits = sorted(workdir.glob("subs*.vtt")) + sorted(workdir.glob("subs*.srt")) + sorted(workdir.glob("subs*.json3"))
    return hits[0] if hits else None


def download_audio(url: str, cfg: Config, workdir: Path) -> Path:
    import yt_dlp

    opts = _base_opts(cfg)
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": {"default": str(workdir / "source.%(ext)s")},
        "overwrites": False,
    })

    last = {"t": 0.0}

    def hook(d):
        if d.get("status") != "downloading":
            return
        now = time.time()
        if now - last["t"] < 5:
            return
        last["t"] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        got = d.get("downloaded_bytes") or 0
        pct = f"{got * 100 / total:5.1f}%" if total else "  ?  "
        _log(f"  下载音频 {pct}")

    opts["progress_hooks"] = [hook]
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except Exception as exc:  # noqa: BLE001
            raise FetchError(f"音频下载失败：{exc}") from exc

    path = None
    for item in (info or {}).get("requested_downloads") or []:
        candidate = item.get("filepath") or item.get("_filename")
        if candidate and Path(candidate).exists():
            path = Path(candidate)
            break
    if path is None:
        hits = [p for p in workdir.glob("source.*") if p.suffix.lower() not in (".json", ".part")]
        path = hits[0] if hits else None
    if path is None or not path.exists():
        raise FetchError("音频下载失败：没有找到落盘文件")
    return path


# ---------------------------------------------------------------- 字幕解析

_VTT_TS = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})$")


def _to_seconds(stamp: str) -> float | None:
    m = _VTT_TS.match(stamp.strip())
    if not m:
        return None
    h, mi, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _clean_cue(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)          # <c>, <00:00:01.500> 等内联标签
    text = html.unescape(text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _dedupe(cues: list[dict]) -> list[dict]:
    """自动字幕是"滚动"的：后一条常包含前一条，需要合并去重。"""
    out: list[dict] = []
    for cue in cues:
        text = cue["text"]
        if not text:
            continue
        if out:
            prev = out[-1]
            if text == prev["text"]:
                prev["end"] = max(prev["end"], cue["end"])
                continue
            if text.startswith(prev["text"]) and cue["start"] - prev["end"] < 1.5:
                prev["text"] = text
                prev["end"] = max(prev["end"], cue["end"])
                continue
            if prev["text"].endswith(text) and cue["start"] - prev["end"] < 1.5:
                prev["end"] = max(prev["end"], cue["end"])
                continue
        out.append(dict(cue))
    return [c for c in out if c["text"]]


def parse_subtitle(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    cues: list[dict] = []
    if path.suffix.lower() == ".json3":
        import json

        data = json.loads(raw)
        for event in data.get("events") or []:
            segs = event.get("segs") or []
            text = _clean_cue("".join(s.get("utf8", "") for s in segs))
            if not text:
                continue
            start = (event.get("tStartMs") or 0) / 1000
            dur = (event.get("dDurationMs") or 0) / 1000
            cues.append({"start": start, "end": start + max(dur, 0.5), "text": text})
        return _renumber(_dedupe(cues))

    for block in re.split(r"\n\s*\n", raw):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        left, _, right = lines[idx].partition("-->")
        start = _to_seconds(left.strip().split()[0]) if left.strip() else None
        end_token = right.strip().split()[0] if right.strip() else ""
        end = _to_seconds(end_token)
        if start is None or end is None:
            continue
        text = _clean_cue(" ".join(lines[idx + 1:]))
        if text:
            cues.append({"start": start, "end": max(end, start + 0.4), "text": text})
    return _renumber(_dedupe(cues))


def _renumber(segments: list[dict]) -> list[dict]:
    out = []
    for i, seg in enumerate(segments):
        out.append({
            "id": i,
            "start": round(float(seg["start"]), 2),
            "end": round(float(seg["end"]), 2),
            "text": seg["text"],
        })
    return out


# ---------------------------------------------------------------- 主流程


def fetch(url: str, cfg: Config, workdir: Path, force: bool = False, info: dict | None = None) -> dict:
    """返回 {meta, source, segments}；字幕路径会直接产出 transcript.json。"""
    workdir.mkdir(parents=True, exist_ok=True)
    cached = read_json(workdir / "transcript.json")
    if cached and cached.get("segments") and not force:
        _log("  已存在 transcript.json，跳过下载（--force 可重来）")
        return {"meta": cached.get("meta", {}), "source": cached.get("source", {}),
                "segments": cached["segments"], "cached": True}

    _log(f"  解析链接：{url}")
    info = info or probe(url, cfg)
    meta = _clean_meta(info)
    write_json(workdir / "meta.json", meta)
    _log(f"  {meta['title']} ｜ {meta.get('uploader') or '未知作者'} ｜ {meta['duration_text']}")

    segments: list[dict] = []
    source = {"kind": "none", "audio": None, "subtitle": None, "lang": None}
    picked = _pick_subtitle(info) if cfg.prefer_subs else None
    if picked:
        kind, lang, ext = picked
        label = "人工字幕" if kind == "subtitles" else "自动字幕"
        _log(f"  发现{label}：{lang} ({ext})，走字幕直取")
        sub_path = _download_subtitle(url, cfg, workdir, lang, ext)
        if sub_path:
            segments = parse_subtitle(sub_path)
            if segments:
                source = {
                    "kind": "subtitle",
                    "subtitle": str(sub_path),
                    "lang": lang,
                    "subtitle_type": kind,
                    "audio": None,
                }
                _log(f"  字幕解析完成：{len(segments)} 段，末段 {fmt_ts(segments[-1]['end'])}")

    if segments:
        write_json(workdir / "source.json", source)
        write_json(workdir / "transcript.json", {
            "meta": meta, "source": source, "segments": segments,
            "text": " ".join(s["text"] for s in segments).strip(),
        })
        return {"meta": meta, "source": source, "segments": segments, "cached": False}

    # 无字幕：复用已下载的音频，避免重复请求
    prev = read_json(workdir / "source.json", {}) or {}
    prev_audio = prev.get("audio")
    if prev_audio and Path(prev_audio).exists() and not force:
        audio = Path(prev_audio)
        _log(f"  复用已下载音频：{audio.name}")
    else:
        _log("  没有可用字幕，下载音频走转写")
        audio = download_audio(url, cfg, workdir)
        _log(f"  音频就绪：{audio.name}（{audio.stat().st_size / 1e6:.1f} MB）")
    source = {"kind": "audio", "audio": str(audio), "subtitle": None, "lang": None}
    write_json(workdir / "source.json", source)
    return {"meta": meta, "source": source, "segments": [], "cached": False}


def friendly_error(message: str) -> str:
    """把 yt-dlp 的英文报错翻译成"下一步该干什么"。"""
    text = str(message or "")
    low = text.lower()
    if "could not copy chrome cookie database" in low:
        return "Chrome 正在运行，cookie 库被占用：先关掉 Chrome 再试，或导出 cookies.txt 设 V2I_COOKIES。"
    if "failed to decrypt with dpapi" in low:
        return "新版 Chrome/Edge 的 cookie 有 App-Bound 加密，yt-dlp 解不开：请用浏览器扩展导出 cookies.txt，或改用 Firefox。"
    if "sign in to confirm you" in low or "not a bot" in low:
        return "YouTube 触发了机器人校验：需要 cookies.txt（导出后设 V2I_COOKIES），或挂代理换 IP。"
    if "fresh cookies" in low:
        return "该站点要求新鲜 cookies：导出 cookies.txt 后设 V2I_COOKIES（抖音/快手/小红书基本都需要）。"
    if "unsupported url" in low:
        return "这个链接形式不被支持：抖音请用 /video/<id>（不是 /shipin/），小红书请用 xhslink 短链或笔记页。"
    if "private" in low or "login" in low or "sign in" in low:
        return "内容需要登录：导出 cookies.txt 后设 V2I_COOKIES。"
    if "http error 403" in low:
        return "403：可能需要 cookies 或代理；B 站高清晰度也要登录。"
    if "timed out" in low or "timeout" in low:
        return "网络超时：国内访问 YouTube / X / TikTok 需要代理（V2I_PROXY）。"
    return text
