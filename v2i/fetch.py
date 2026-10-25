"""下载层（fetch）：字幕优先，音频兜底。

设计要点：
- 先用 extract_info(download=False) 探测字幕，有字幕就只下字幕，省掉下载与转写；
- 字幕解析成统一的 segments: [{start, end, text}]，与 ASR 产物同构；
- 没字幕才下 bestaudio（m4a/webm 均可，PyAV 直接解码，不强依赖 ffmpeg）。
"""
from __future__ import annotations

import html
import re
import subprocess
import time
from pathlib import Path

from .config import Config
from .util import display_url, fmt_ts, now_iso, read_json, write_json

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
    url = resolve_douyin(url, cfg)
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


def download_media(url: str, cfg: Config, workdir: Path) -> tuple[Path, bool]:
    """下载媒体。keep_video=True 时拿带画面的 mp4（界面里能直接播），否则只要音频。

    返回 (路径, 是否视频)。视频文件同样可以直接喂给 faster-whisper（PyAV 会解出音轨）。
    """
    import yt_dlp

    opts = _base_opts(cfg)
    if getattr(cfg, "keep_video", False):
        opts.update({
            "format": "bv*[height<=720]+ba/b[height<=720]/best",
            "merge_output_format": "mp4",
        })
        is_video = True
    else:
        opts.update({"format": "bestaudio/best"})
        is_video = False
    opts.update({
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
        raise FetchError("媒体下载失败：没有找到落盘文件")
    return path, is_video


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


DOUYIN_VIDEO_RE = re.compile(r"/video/(\d{6,})")


def _cookie_header(cfg: Config) -> str:
    """从 Netscape cookies.txt 里挑出抖音相关 cookie，拼成请求头。"""
    path = getattr(cfg, "cookies", None)
    if not path:
        return ""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    pairs = []
    for line in lines:
        if not line.strip() or line.startswith("# ") or line.count("\t") < 6:
            continue
        parts = line.split("\t")
        domain, name, value = parts[0].lstrip("#"), parts[5], parts[6]
        if "douyin" in domain and name and value:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def cookie_domains(cfg: Config) -> set[str]:
    """cookies.txt 里出现过的域名（去掉前导点）。"""
    path = getattr(cfg, "cookies", None)
    if not path or not Path(path).exists():
        return set()
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    domains = set()
    for line in lines:
        if line.count("\t") < 6 or line.startswith("# ") and "#HttpOnly_" not in line:
            continue
        domains.add(line.split("\t")[0].lstrip("#").lstrip(".").lower())
    return domains


def missing_cookie_domain(url: str, cfg: Config) -> str | None:
    """该平台需要 cookies，但 cookies.txt 里没有任何相关域名时返回域名。"""
    from . import platforms as platforms_mod

    platform = platforms_mod.match(url) or platforms_mod.get("auto")
    if not platform or "cookies" not in (platform.get("needs") or []):
        return None
    domains = cookie_domains(cfg)
    if not domains:
        return "（cookies.txt 为空）"
    hosts = [h.replace("www.", "") for h in (platform.get("hosts") or [])]
    for domain in domains:
        if any(host in domain or domain in host for host in hosts):
            return None
    return hosts[0] if hosts else url


def douyin_fallback_ready(url: str, cfg: Config | None = None) -> bool:
    """抖音链接 + 装了（或指定了）外部下载器时，允许绕过 probe 直接进 fetch。"""
    from . import douyin as douyin_mod

    prefer = getattr(cfg, "douyin_downloader", "") if cfg else ""
    return "douyin" in (url or "").lower() and bool(douyin_mod.find_downloader(prefer))


def _douyin_fallback(url: str, cfg: Config, workdir: Path, exc: Exception) -> Path | None:
    """抖音签名过不去时，尝试外部下载器（f2 / douyin-downloader）。"""
    text = str(exc).lower()
    if "douyin" not in url.lower():
        return None
    if not any(k in text for k in ("fresh cookies", "a_bogus", "签名")):
        return None
    from . import douyin as douyin_mod

    prefer = getattr(cfg, "douyin_downloader", "") or ""
    if not douyin_mod.find_downloader(prefer):
        _log("  ! 没找到可用的抖音下载器，按下面任一方式装好即可自动接管：")
        for line in douyin_mod.INSTALL_HINT.splitlines():
            _log("    " + line)
        return None
    _log("  抖音接口需要 a_bogus 签名，改用外部下载器…")
    media = douyin_mod.download(url, workdir, getattr(cfg, "cookies", None),
                                prefer=prefer, proxy=getattr(cfg, "proxy", "") or "")
    if media:
        _log(f"  外部下载器拿到文件：{media.name}（{media.stat().st_size / 1e6:.1f} MB）")
        return media
    _log("  ! 外部下载器没下到文件")
    return None


def resolve_douyin(url: str, cfg: Config) -> str:
    """抖音的 /shipin/（合集）、分享页等 yt-dlp 不认，尽量换成 /video/<id>。

    解析不出来就原样返回，让后续报错给出可操作提示。
    """
    if "douyin.com" not in url or DOUYIN_VIDEO_RE.search(url):
        return url

    # 形如 ...?modal_id=7300000000000000000 的分享链接，直接取参数即可
    m = re.search(r"[?&]modal_id=(\d{6,})", url)
    if m:
        fixed = f"https://www.douyin.com/video/{m.group(1)}"
        _log(f"  链接已转换（modal_id）：{display_url(fixed)}")
        return fixed

    try:
        import requests

        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            "Referer": "https://www.douyin.com/",
        }
        cookie = _cookie_header(cfg)
        if cookie:
            headers["Cookie"] = cookie
        resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        patterns = (
            DOUYIN_VIDEO_RE,
            re.compile(r'"aweme_id"\s*:\s*"(\d{6,})"'),
            re.compile(r"aweme_id=(\d{6,})"),
        )
        for text in (resp.url, resp.text):
            for pattern in patterns:
                m = pattern.search(text)
                if m:
                    fixed = f"https://www.douyin.com/video/{m.group(1)}"
                    _log(f"  链接已转换：{display_url(url)} → {display_url(fixed)}")
                    return fixed
    except Exception:  # 网络问题不阻断，让原来的报错继续
        pass
    return url


AUDIO_SUFFIXES = {".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus", ".wma"}


def extract_audio(media: Path, workdir: Path, cfg: Config) -> Path | None:
    """从视频里抽出独立音轨（audio.m4a），方便单独取用/播放。

    已经是音频就原样返回；没有 ffmpeg 就返回 None，不影响主流程。
    """
    media = Path(media)
    if media.suffix.lower() in AUDIO_SUFFIXES:
        return media
    if not getattr(cfg, "extract_audio", True):
        return None
    ffmpeg = getattr(cfg, "ffmpeg_exe", None)
    if not ffmpeg:
        return None
    target = workdir / "audio.m4a"
    if target.exists() and target.stat().st_size > 1000:
        return target
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(media),
           "-vn", "-c:a", "aac", "-b:a", "128k", str(target)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not target.exists() or target.stat().st_size < 1000:
        return None
    return target


def fetch(url: str, cfg: Config, workdir: Path, force: bool = False, info: dict | None = None) -> dict:
    """返回 {meta, source, segments}；字幕路径会直接产出 transcript.json。"""
    workdir.mkdir(parents=True, exist_ok=True)
    cached = read_json(workdir / "transcript.json")
    if cached and cached.get("segments") and not force:
        _log("  已存在 transcript.json，跳过下载（--force 可重来）")
        return {"meta": cached.get("meta", {}), "source": cached.get("source", {}),
                "segments": cached["segments"], "cached": True}

    if cfg.cookies:
        _log(f"  使用 cookies：{Path(cfg.cookies).name}（{Path(cfg.cookies).stat().st_size} B）")
    elif getattr(cfg, "cookies_from_browser", None):
        _log(f"  使用浏览器 cookies：{cfg.cookies_from_browser}")
    url = resolve_douyin(url, cfg)
    _log(f"  解析链接：{display_url(url)}")
    try:
        info = info or probe(url, cfg)
    except FetchError as exc:
        media = _douyin_fallback(url, cfg, workdir, exc)
        if media is None:
            raise
        import json as _json

        desc = ""
        sidecar = media.parent / "douyin-meta.json"
        if sidecar.exists():
            try:
                desc = (_json.loads(sidecar.read_text(encoding="utf-8")) or {}).get("desc") or ""
            except (OSError, ValueError):
                desc = ""
        import re as _re

        vid = ""
        m = _re.search(r"/video/(\d{6,})", url)
        if m:
            vid = m.group(1)
        meta = {
            "id": vid or media.stem,
            "title": (desc.strip()[:60] or media.stem),
            "uploader": "",
            "duration": 0,
            "duration_text": "待转写",
            "webpage_url": url,
        }
        write_json(workdir / "meta.json", meta)
        source = {
            "kind": "video",
            "media": str(media),
            "audio": str(media),
            "subtitle": None,
            "lang": None,
            "via": "external-downloader",
        }
        audio = extract_audio(media, workdir, cfg)
        if audio and audio != media:
            source["audio_file"] = str(audio)
            _log(f"  音频已提取：{audio.name}（{audio.stat().st_size / 1e6:.1f} MB）")
            write_json(workdir / "source.json", source)
        write_json(workdir / "source.json", source)
        return {"meta": meta, "source": source, "segments": [], "cached": False}
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
    prev_media = prev.get("media") or prev.get("audio")
    video_suffixes = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
    if prev_media and Path(prev_media).exists() and not force:
        media = Path(prev_media)
        is_video = media.suffix.lower() in video_suffixes
        _log(f"  复用已下载{'视频' if is_video else '音频'}：{media.name}")
    else:
        _log("  没有可用字幕，下载媒体走转写")
        media, is_video = download_media(url, cfg, workdir)
        _log(f"  {'视频' if is_video else '音频'}就绪：{media.name}（{media.stat().st_size / 1e6:.1f} MB）")
    source = {
        "kind": "video" if is_video else "audio",
        "media": str(media),
        "audio": str(media),
        "subtitle": None,
        "lang": None,
    }
    if is_video:
        audio_file = extract_audio(media, workdir, cfg)
        if audio_file and audio_file != media:
            source["audio_file"] = str(audio_file)
            _log(f"  音频已提取：{audio_file.name}（{audio_file.stat().st_size / 1e6:.1f} MB）")
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
        if "douyin" in low or "抖音" in text:
            return ("抖音要求 a_bogus 签名，yt-dlp 拿不到详情接口（cookies 齐全也一样）。"
                    "v2i 会用「浏览器自动化（本机 Chrome）」打开页面、截获真实播放地址再下载，"
                    "所以直接开始任务即可；如果本机没有 Chrome/node，就退回「本地文件」拖 mp4。")
        return ("该站点要求「站点自己种的新鲜 cookie」（不一定要登录）：先在浏览器里打开一次该站点"
                "（快手/小红书），再点「一键从浏览器获取」；Chrome/Edge 读不出来就用扩展导出 cookies.txt。")
    if "unsupported url" in low:
        return ("这个链接形式不被支持。抖音可用：douyin.com/video/<id>、v.douyin.com 短链、"
                "或带 modal_id 的分享链接（/shipin/ 是合集页，需要点开具体视频再复制）；"
                "小红书请用 xhslink 短链或笔记页；快手/其他站点请贴视频详情页。")
    if "private" in low or "login" in low or "sign in" in low:
        return "内容需要登录：导出 cookies.txt 后设 V2I_COOKIES。"
    if "http error 403" in low:
        return "403：可能需要 cookies 或代理；B 站高清晰度也要登录。"
    if "unable to download webpage" in low or "connection refused" in low or "connection reset" in low:
        return "网页打不开（网络被阻断或站点拒绝）：国内访问 YouTube / X / TikTok 需要代理（V2I_PROXY）。"
    if "timed out" in low or "timeout" in low:
        return "网络超时：国内访问 YouTube / X / TikTok 需要代理（V2I_PROXY）。"
    return text


def error_action(message: str) -> str | None:
    """给前端一个可点击的下一步：cookies_file / close_browser / proxy / url。

    报错可能已被 friendly_error() 翻译成中文，所以中英两种措辞都要认。
    """
    text = str(message or "")
    low = text.lower()
    if "could not copy chrome cookie database" in low or "chrome 正在运行" in text:
        return "close_browser"
    if "failed to decrypt with dpapi" in low or "app-bound" in low or "app bound" in low:
        return "cookies_file"
    if "douyin" in low and "fresh cookies" in low:
        return "local"
    if ("fresh cookies" in low or "not a bot" in low or "sign in" in low or "login" in low
            or "private" in low or "cookie" in low
            or "需要新鲜 cookies" in text or "机器人校验" in text or "需要登录" in text):
        return "cookies_file"
    if ("unable to download webpage" in low or "timed out" in low or "timeout" in low
            or "connection" in low or "网络超时" in text or "网页打不开" in text):
        return "proxy"
    if "unsupported url" in low or "链接形式不被支持" in text:
        return "url"
    return None
