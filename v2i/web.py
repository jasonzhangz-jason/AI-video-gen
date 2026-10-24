"""本地 Web GUI：一个页面 + 一组 JSON API，复用 CLI 同一条流水线。

    python -m v2i web --port 8765

设计取舍：
- 只用标准库 http.server，零额外依赖（国内装 Flask 反而慢）；
- 任务串行（单 worker 线程）：ASR 本来就吃满 CPU，并发只会互相拖慢；
- 日志走 stdout 重定向捕获，不必改造 pipeline 里的 print；
- 只监听 127.0.0.1，不对外网暴露。
"""
from __future__ import annotations

import io
import json
import queue
import socket
import threading
import time
import traceback
import webbrowser
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import __version__
from .config import DEFAULT_COOKIES, PROJECT_ROOT, Config, ConfigError
from .util import now_iso, read_json, slugify, write_json

STATIC_DIR = Path(__file__).resolve().parent / "static"
ALLOWED_SUFFIX = {".md", ".json", ".srt", ".vtt", ".txt", ".log", ".csv", ".tsv"}
TEXT_SUFFIX = ALLOWED_SUFFIX

# 预览用的 content-type（媒体要正确才能让 <audio>/<video> 播）
CONTENT_TYPES = {
    ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8", ".srt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".vtt": "text/vtt; charset=utf-8",
    ".csv": "text/csv; charset=utf-8", ".tsv": "text/tab-separated-values; charset=utf-8",
    ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".aac": "audio/aac", ".wma": "audio/x-ms-wma", ".amr": "audio/amr",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".ts": "video/mp2t",
}
MAX_JOBS = 40

# 本地文件上传
UPLOAD_DIR = "_uploads"
ALLOWED_MEDIA = {
    ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus", ".wma", ".amr",
    ".mp4", ".mkv", ".mov", ".m4v", ".webm", ".ts", ".flv",
}
MAX_UPLOAD_BYTES = 4 * 1024 ** 3


def _cookie_status() -> dict:
    path = DEFAULT_COOKIES
    if not path.exists():
        return {"present": False, "path": str(path)}
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    cookies = [ln for ln in lines if not ln.startswith("#") or ln.startswith("#HttpOnly_")]
    netscape = "Netscape HTTP Cookie File" in text or (
        cookies and all(len(ln.split("\t")) >= 7 for ln in cookies[:5])
    )
    stat = path.stat()
    return {
        "present": True,
        "path": str(path),
        "size": stat.st_size,
        "count": len(cookies),
        "mtime": time.strftime("%m-%d %H:%M", time.localtime(stat.st_mtime)),
        "netscape": bool(netscape),
    }

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_queue: "queue.Queue[str]" = queue.Queue()


# ---------------------------------------------------------------- 任务


class _LogWriter(io.TextIOBase):
    """把 print 的每一行收进 job 日志，并顺手推断阶段与进度。"""

    encoding = "utf-8"

    def __init__(self, job: dict):
        self.job = job
        self.buf = ""

    def write(self, text: str) -> int:
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._emit(line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self.buf.strip():
            self._emit(self.buf.strip())
            self.buf = ""

    def isatty(self) -> bool:
        return False

    def _emit(self, line: str) -> None:
        if not line:
            return
        self.job["logs"].append(line)
        if len(self.job["logs"]) > 800:
            del self.job["logs"][:-800]
        stage, progress = _guess_stage(line)
        if stage:
            self.job["stage"] = stage
        if progress is not None:
            self.job["progress"] = max(self.job["progress"], progress)


def _guess_stage(line: str) -> tuple[str | None, int | None]:
    if "解析链接" in line:
        return "解析链接", 3
    if "工作目录" in line:
        return "准备工作目录", 5
    if "字幕" in line and ("发现" in line or "解析完成" in line):
        return "字幕直取", 25
    if "下载音频" in line:
        return "下载音频", 12
    if "音频就绪" in line or "复用已下载音频" in line:
        return "音频就绪", 20
    if "加载模型" in line:
        return "加载 ASR 模型", 25
    if "转写中" in line:
        pct = None
        for token in line.split():
            if token.endswith("%"):
                try:
                    pct = 25 + int(float(token.rstrip("%")) * 0.4)
                except ValueError:
                    pct = None
        return "转写中", pct
    if "转写完成" in line:
        return "转写完成", 66
    if "分块完成" in line:
        return "分块", 70
    if "map 阶段" in line:
        return "提炼：分块笔记", 72
    if "完成（" in line and "条要点" in line:
        return None, None
    if "token 用量" in line:
        return "收尾", 96
    if "完成，用时" in line:
        return "完成", 100
    return None, None


def _run_local(path: Path, cfg: Config, modes: tuple[str, ...], force: bool) -> Path:
    """本地文件：转写 → 分块 → 提炼（与 v2i transcribe + distill 等价）。"""
    from . import asr, distill, normalize

    path = Path(path)
    workdir = cfg.runs_dir / f"{slugify(path.stem)}-local"
    workdir.mkdir(parents=True, exist_ok=True)
    meta = {
        "title": path.stem,
        "uploader": "本地文件",
        "duration": None,
        "duration_text": "未知",
        "webpage_url": "",
        "local_file": str(path),
        "chapters": [],
        "fetched_at": now_iso(),
    }
    write_json(workdir / "meta.json", meta)

    transcript = asr.transcribe(path, cfg, workdir, force=force)
    chunks = normalize.normalize(transcript, workdir, cfg.chunk_chars, cfg.overlap_sentences)

    manifest = read_json(workdir / "manifest.json", {}) or {}
    manifest.update({
        "workdir": str(workdir),
        "local_file": str(path),
        "fetch": {"kind": "upload", "lang": None, "cached": False, "at": now_iso()},
        "chunks": len(chunks),
        "chars": sum(c["chars"] for c in chunks),
        "asr": transcript.get("asr"),
    })
    write_json(workdir / "manifest.json", manifest)

    try:
        distill.distill(workdir, cfg, modes, force=force)
    except ConfigError as exc:
        print(f"  ! 提炼跳过：{exc}", flush=True)
        print(f"    转写产物已保留：{workdir}", flush=True)
        manifest = read_json(workdir / "manifest.json", {}) or {}
        manifest["distill_skipped"] = str(exc)
        write_json(workdir / "manifest.json", manifest)
    return workdir


def _run_job(job: dict) -> None:
    from .cli import _run_one

    job["status"] = "running"
    job["started"] = now_iso()
    started = time.time()
    try:
        with redirect_stdout(_LogWriter(job)):
            cfg = job["_cfg"]
            if job.get("kind") == "local":
                workdir = _run_local(Path(job["_file_path"]), cfg, job["modes"], job["force"])
            else:
                workdir = _run_one(job["url"], cfg, cfg.runs_dir, job["modes"], job["force"])
        job["workdir"] = str(workdir)
        job["status"] = "done"
        job["progress"] = 100
        job["stage"] = "完成"
    except BaseException as exc:  # noqa: BLE001 - 任务线程必须兜住一切
        job["status"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"
        job["logs"].append(traceback.format_exc(limit=4))
    finally:
        job["elapsed"] = round(time.time() - started, 1)
        job["finished"] = now_iso()


def _worker() -> None:
    while True:
        job_id = _queue.get()
        job = _jobs.get(job_id)
        try:
            if job is not None:
                _run_job(job)
        except Exception:  # noqa: BLE001
            pass
        finally:
            _queue.task_done()


threading.Thread(target=_worker, daemon=True, name="v2i-worker").start()


# ---------------------------------------------------------------- 数据


def _file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIX:
        return "text"
    ctype = CONTENT_TYPES.get(suffix, "")
    if ctype.startswith("image/"):
        return "image"
    if ctype.startswith("audio/"):
        return "audio"
    if ctype.startswith("video/"):
        return "video"
    return "binary"


def _list_files(run_dir: Path) -> list[dict]:
    """运行目录下的产物清单（两层：根目录 + out/ + notes/ 只列摘要）。"""
    items: list[dict] = []
    root = run_dir.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith("_"):
            continue
        # notes 下几十个分块文件只保留计数，避免列表把页面撑爆
        if rel.startswith("notes/chunk-"):
            continue
        stat = path.stat()
        items.append({
            "path": rel,
            "size": stat.st_size,
            "kind": _file_kind(path),
            "mtime": time.strftime("%m-%d %H:%M", time.localtime(stat.st_mtime)),
        })
    notes = len(list((root / "notes").glob("chunk-*.json"))) if (root / "notes").is_dir() else 0
    if notes:
        items.append({"path": "notes/", "size": notes, "kind": "dir", "mtime": "", "note": f"{notes} 块笔记"})
    return items


def _runs_dir(cfg: Config) -> Path:
    return cfg.runs_dir


def _run_summary(path: Path, cfg: Config) -> dict:
    meta = read_json(path / "meta.json", {}) or {}
    manifest = read_json(path / "manifest.json", {}) or {}
    out = path / "out"
    outputs = [p.name for p in out.glob("*.md")] if out.exists() else []
    has_transcript = (path / "transcript.json").exists()
    return {
        "dir": path.name,
        "title": meta.get("title") or path.name,
        "uploader": meta.get("uploader") or "",
        "duration": meta.get("duration_text") or "",
        "url": meta.get("webpage_url") or manifest.get("url") or "",
        "source": (manifest.get("fetch") or {}).get("kind") or "",
        "outputs": sorted(outputs),
        "has_transcript": has_transcript,
        "elapsed": manifest.get("elapsed_sec"),
        "tokens": ((manifest.get("distill") or {}).get("usage") or {}).get("total_tokens"),
        "mtime": path.stat().st_mtime,
        "mtime_text": time.strftime("%m-%d %H:%M", time.localtime(path.stat().st_mtime)),
        "invalid": (manifest.get("distill") or {}).get("invalid_citations") or {},
        "skipped": (manifest.get("distill") or {}).get("skipped") or [],
    }


def _safe_run_dir(name: str, cfg: Config) -> Path | None:
    if not name or name in (".", ".."):
        return None
    root = _runs_dir(cfg).resolve()
    target = (root / name).resolve()
    if root not in target.parents:
        return None
    return target if target.is_dir() else None


def _safe_any_file(run_dir: Path, name: str) -> Path | None:
    name = (name or "").replace("\\", "/").lstrip("/")
    if not name:
        return None
    target = (run_dir / name).resolve()
    if run_dir.resolve() not in target.parents or not target.is_file():
        return None
    return target


def _safe_file(run_dir: Path, name: str) -> Path | None:
    name = (name or "").replace("\\", "/").lstrip("/")
    target = (run_dir / name).resolve()
    if run_dir.resolve() not in target.parents:
        return None
    if target.suffix.lower() not in ALLOWED_SUFFIX or not target.is_file():
        return None
    return target


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = f"v2i/{__version__}"
    protocol_version = "HTTP/1.1"
    cfg: Config = Config.from_env()

    def log_message(self, fmt, *args):  # 静音访问日志
        return

    # ---- 基础工具
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _send_media(self, path: Path) -> None:
        """流式预览：支持 Range（浏览器播放音视频必须），文本/图片也能直接用。"""
        size = path.stat().st_size
        ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        start, end, status = 0, size - 1, 200
        raw_range = self.headers.get("Range") or ""
        if raw_range.startswith("bytes="):
            try:
                spec = raw_range[6:].split(",")[0]
                first, _, last = spec.partition("-")
                if first.strip():
                    start = int(first)
                    end = int(last) if last.strip() else size - 1
                elif last.strip():
                    start = max(0, size - int(last))
                    end = size - 1
                start = max(0, min(start, size - 1))
                end = max(0, min(end, size - 1))
                if start <= end:
                    status = 206
                else:
                    start, end = 0, size - 1
            except ValueError:
                start, end, status = 0, size - 1, 200

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_download(self, path: Path) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
        self.end_headers()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _file(self, path: Path, ctype: str) -> None:
        try:
            body = path.read_bytes()
        except OSError as exc:
            self._error(404, str(exc))
            return
        self._send(200, body, ctype)

    # ---- 路由
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        route, query = parsed.path, parse_qs(parsed.query)
        try:
            if route in ("/", "/index.html"):
                return self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            if route.startswith("/static/"):
                name = route[len("/static/"):]
                target = (STATIC_DIR / name).resolve()
                if STATIC_DIR.resolve() not in target.parents or not target.is_file():
                    return self._error(404, "not found")
                ctype = {
                    ".js": "application/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".html": "text/html; charset=utf-8",
                    ".svg": "image/svg+xml",
                    ".png": "image/png",
                    ".ico": "image/x-icon",
                }.get(target.suffix, "text/plain; charset=utf-8")
                return self._file(target, ctype)
            if route == "/api/status":
                return self._json(self._status())
            if route == "/api/runs":
                return self._json(self._runs())
            if route == "/api/cookies":
                return self._json(_cookie_status())
            if route == "/api/platforms":
                from . import platforms as platforms_mod
                url = (query.get("url") or [""])[0]
                found = platforms_mod.match(url) if url else None
                return self._json({
                    "platforms": platforms_mod.PLATFORMS,
                    "detected": found["id"] if found else None,
                })
            if route == "/api/probe":
                return self._json(self._probe(query))
            if route == "/api/job":
                job = _jobs.get((query.get("id") or [""])[0])
                if job:
                    job = {k: v for k, v in job.items() if not k.startswith("_")}
                return self._json(job or {"error": "job not found"}, 200 if job else 404)
            if route == "/api/run":
                return self._json(self._run_detail((query.get("dir") or [""])[0]))
            if route == "/api/files":
                run_dir = _safe_run_dir((query.get("dir") or [""])[0], self.cfg)
                if not run_dir:
                    return self._error(404, "run not found")
                return self._json({"dir": run_dir.name, "files": _list_files(run_dir)})
            if route == "/api/preview":
                run_dir = _safe_run_dir((query.get("dir") or [""])[0], self.cfg)
                if not run_dir:
                    return self._error(404, "run not found")
                target = _safe_any_file(run_dir, (query.get("name") or [""])[0])
                if not target:
                    return self._error(404, "file not found")
                return self._send_media(target)
            if route == "/api/download":
                run_dir = _safe_run_dir((query.get("dir") or [""])[0], self.cfg)
                if not run_dir:
                    return self._error(404, "run not found")
                target = _safe_any_file(run_dir, (query.get("name") or [""])[0])
                if not target:
                    return self._error(404, "file not found")
                return self._send_download(target)
            if route == "/api/file":
                run_dir = _safe_run_dir((query.get("dir") or [""])[0], self.cfg)
                if not run_dir:
                    return self._error(404, "run not found")
                target = _safe_file(run_dir, (query.get("name") or [""])[0])
                if not target:
                    return self._error(404, "file not allowed")
                return self._send(200, target.read_bytes(), "text/plain; charset=utf-8")
            return self._error(404, "not found")
        except Exception as exc:  # noqa: BLE001
            return self._error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        # 上传是原始字节流，不能按 JSON 读
        if parsed.path == "/api/cookies":
            try:
                result = self._save_cookies()
                if result.get("error"):
                    return self._error(400, result["error"])
                return self._json(result)
            except Exception as exc:  # noqa: BLE001
                return self._error(500, f"{type(exc).__name__}: {exc}")
        if parsed.path == "/api/cookies/clear":
            try:
                if DEFAULT_COOKIES.exists():
                    DEFAULT_COOKIES.unlink()
                return self._json(_cookie_status())
            except OSError as exc:
                return self._error(500, str(exc))
        if parsed.path == "/api/upload":
            try:
                return self._json(self._upload(query))
            except Exception as exc:  # noqa: BLE001
                return self._error(500, f"{type(exc).__name__}: {exc}")

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._error(400, "body 不是合法 JSON")

        try:
            if parsed.path == "/api/start":
                return self._json(self._start(body))
            if parsed.path == "/api/retry":
                return self._json(self._start(body, retry=True))
            return self._error(404, "not found")
        except Exception as exc:  # noqa: BLE001
            return self._error(500, f"{type(exc).__name__}: {exc}")

    # ---- API 实现
    def _status(self) -> dict:
        cfg = Config.from_env()
        device = cfg.device
        llm_ready = bool(cfg.api_key) or cfg.provider == "ollama"
        pending = sum(1 for j in _jobs.values() if j["status"] in ("queued", "running"))
        active = next((j["id"] for j in _jobs.values() if j["status"] == "running"), None)
        if not active:
            active = next((j["id"] for j in _jobs.values() if j["status"] == "queued"), None)
        return {
            "version": __version__,
            "runs_dir": str(cfg.runs_dir),
            "project": str(PROJECT_ROOT),
            "active_job": active,
            "llm": {
                "provider": cfg.provider,
                "model": cfg.model or "",
                "base_url": cfg.base_url,
                "ready": llm_ready,
                "env_hint": "DEEPSEEK_API_KEY" if cfg.provider == "deepseek" else "",
            },
            "asr": {"model": cfg.asr_model, "device": device, "compute": cfg.compute_type(device)},
            "ffmpeg": cfg.ffmpeg_exe,
            "queue": pending,
        }

    def _runs(self) -> list[dict]:
        cfg = self.cfg
        root = _runs_dir(cfg)
        if not root.exists():
            return []
        items = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith("_")]
        items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [_run_summary(p, cfg) for p in items[:60]]

    def _run_detail(self, name: str) -> dict:
        run_dir = _safe_run_dir(name, self.cfg)
        if not run_dir:
            return {"error": "run not found"}
        files = []
        for rel in ("out/summary.md", "out/points.md", "out/article.md", "out/copy.md",
                    "out/copy.txt", "transcript.md", "transcript.srt", "out/digest.json",
                    "meta.json", "manifest.json"):
            if (run_dir / rel).exists():
                files.append(rel)
        return {"dir": name, "files": files, "summary": _run_summary(run_dir, self.cfg)}

    def _cfg_from(self, body: dict) -> Config:
        prefer = body.get("prefer_subs")
        if prefer is None and "no_subs" in body:
            prefer = not bool(body.get("no_subs"))
        return Config.from_env(
            asr_model=body.get("asr_model") or None,
            language=body.get("language") or None,
            model=body.get("model") or None,
            prefer_subs=bool(prefer) if prefer is not None else None,
            copy_style=body.get("copy_style") or None,
            cookies_from_browser=body.get("cookies_from_browser") or None,
            proxy=body.get("proxy") or None,
        )

    def _probe(self, query: dict) -> dict:
        from . import fetch as fetch_mod
        from . import platforms as platforms_mod

        url = (query.get("url") or [""])[0].strip()
        if not url:
            return {"ok": False, "error": "缺少 url"}
        platform_id = (query.get("platform") or ["auto"])[0]
        ok, hint = platforms_mod.check(platform_id, url)
        cfg = Config.from_env(
            cookies_from_browser=(query.get("cookies_from_browser") or [""])[0] or None,
            proxy=(query.get("proxy") or [""])[0] or None,
        )
        try:
            info = fetch_mod.probe(url, cfg, fast=True)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "hint": hint, "platform": platform_id}
        meta = fetch_mod._clean_meta(info)
        subs = sorted(set(info.get("subtitles") or {}) | set(info.get("automatic_captions") or {}))
        return {
            "ok": True,
            "hint": hint,
            "platform": platform_id,
            "title": meta.get("title"),
            "uploader": meta.get("uploader") or "",
            "duration": meta.get("duration_text"),
            "extractor": info.get("extractor_key") or info.get("extractor") or "",
            "subtitles": subs[:8],
            "formats": len(info.get("formats") or []),
        }

    def _new_job(self, body: dict, kind: str, url: str = "",
                 file_path: Path | None = None, file_name: str = "") -> dict:
        modes_raw = body.get("modes") or ["all"]
        if isinstance(modes_raw, str):
            modes_raw = [m.strip() for m in modes_raw.split(",") if m.strip()]
        from .distill import MODES

        modes = tuple(m for m in modes_raw if m in MODES) or MODES
        job_id = f"j{int(time.time() * 1000)}{len(_jobs) % 97}"
        job = {
            "id": job_id,
            "kind": kind,
            "url": url,
            "file": file_name,
            "modes": list(modes),
            "_cfg": self._cfg_from(body),
            "_file_path": str(file_path or ""),
            "force": bool(body.get("force")),
            "status": "queued",
            "stage": "排队中",
            "progress": 0,
            "logs": [],
            "error": "",
            "workdir": "",
            "started": "",
            "finished": "",
            "elapsed": None,
            "created": now_iso(),
        }
        with _lock:
            _jobs[job_id] = job
            while len(_jobs) > MAX_JOBS:
                oldest = next(iter(_jobs))
                _jobs.pop(oldest, None)
        _queue.put(job_id)
        return {"job_id": job_id, "status": "queued"}

    def _start(self, body: dict, retry: bool = False) -> dict:
        url = str(body.get("url") or "").strip()
        if not url and not retry:
            return {"error": "请填视频链接"}
        return self._new_job(body, "url", url=url)

    def _save_cookies(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {"error": "空文件"}
        if length > 2 * 1024 * 1024:
            return {"error": "文件太大：cookies.txt 一般不到 1MB"}
        raw = self.rfile.read(length)
        if len(raw) < length:
            return {"error": "上传中断，请重试"}
        text = raw.decode("utf-8", "replace")
        if "Netscape HTTP Cookie File" not in text and "\t" not in text:
            return {"error": "这不像 Netscape 格式的 cookies.txt（浏览器扩展导出的那种），JSON 导出不支持"}
        DEFAULT_COOKIES.parent.mkdir(parents=True, exist_ok=True)
        if DEFAULT_COOKIES.exists():
            backup = DEFAULT_COOKIES.with_suffix(".bak")
            try:
                backup.write_bytes(DEFAULT_COOKIES.read_bytes())
            except OSError:
                pass
        DEFAULT_COOKIES.write_text(text, encoding="utf-8")
        print(f"  已更新 cookies：{DEFAULT_COOKIES}（{len(text)} 字节）", flush=True)
        return _cookie_status()

    def _upload(self, query: dict) -> dict:
        """原始字节上传（不走 multipart：标准库没有可用的 multipart 解析器）。"""
        from urllib.parse import unquote

        name = Path(unquote(self.headers.get("X-Filename") or "")).name
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_MEDIA:
            return {"error": f"不支持的文件类型：{suffix or '（无扩展名）'}"}
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {"error": "空文件"}
        if length > MAX_UPLOAD_BYTES:
            return {"error": "文件太大（上限 4GB）"}

        # 保留原文件名（标题才干净），用子目录隔开同名文件
        upload_dir = self.cfg.runs_dir / UPLOAD_DIR / time.strftime("%Y%m%d-%H%M%S")
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / name

        remaining = length
        with target.open("wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        if remaining:
            target.unlink(missing_ok=True)
            return {"error": "上传中断，请重试"}

        body = {
            "modes": (query.get("modes") or ["all"])[0],
            "asr_model": (query.get("asr_model") or [""])[0],
            "language": (query.get("language") or [""])[0],
            "model": (query.get("model") or [""])[0],
            "force": (query.get("force") or ["0"])[0] in ("1", "true", "yes"),
            "prefer_subs": (query.get("prefer_subs") or ["1"])[0] not in ("0", "false"),
            "copy_style": (query.get("copy_style") or [""])[0],
            "cookies_from_browser": (query.get("cookies_from_browser") or [""])[0],
            "proxy": (query.get("proxy") or [""])[0],
        }
        result = self._new_job(body, "local", file_path=target, file_name=name)
        result["file"] = name
        result["size"] = length
        return result


class _Server(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 允许"重复绑定"同一端口，会导致两个实例抢连接
    allow_reuse_address = False
    daemon_threads = True


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def serve(port: int = 8765, host: str = "127.0.0.1", open_browser: bool = True) -> None:
    Handler.cfg = Config.from_env()
    if _port_in_use(host, port):
        print(f"⚠ 端口 {port} 已经被占用（可能已经开着一个 v2i web）")
        print(f"   直接访问 http://{host}:{port} ，或换端口：python -m v2i web --port {port + 1}")
        return
    try:
        server = _Server((host, port), Handler)
    except OSError as exc:
        print(f"⚠ 端口 {port} 起不来：{exc}")
        print(f"   换一个端口再试：python -m v2i web --port {port + 1}")
        return
    url = f"http://{host}:{port}"
    print(f"v2i web 已启动：{url}")
    print(f"运行目录：{Handler.cfg.runs_dir}")
    if not Handler.cfg.api_key and Handler.cfg.provider != "ollama":
        print("⚠ 还没配置 LLM key：转写能跑，提炼会失败。把 DEEPSEEK_API_KEY 写进 .env 后重启。")
    print("Ctrl+C 停止")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
