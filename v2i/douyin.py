"""抖音兜底下载：yt-dlp 过不了 a_bogus 签名，改调外部下载器。

支持三种，可在界面上选（也可以用环境变量 `V2I_DOUYIN_DOWNLOADER` 指定）：
- `f2`：pip 包，命令 `f2 dy -u URL -p DIR`（`-k` 传 cookies 文件）
- `douyin-downloader`：独立 CLI，命令 `douyin-downloader -u URL -o DIR`
- `custom`：`V2I_DOUYIN_CMD` 指定任意命令，占位符 `{url}` / `{dir}`

查找顺序：PATH → 项目内 `.tools/`。`auto` 时按 f2 → douyin-downloader → custom。
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}

TOOL_LABELS = {
    "browser": "浏览器自动化（本机 Chrome + CDP）",
    "f2": "f2（pip install f2）",
    "douyin-downloader": "douyin-downloader",
    "custom": "自定义命令（V2I_DOUYIN_CMD）",
}

_LOCAL_CANDIDATES = {
    "f2": (
        ".tools/f2/bin/f2.exe", ".tools/f2/bin/f2", ".tools/f2/bin/f2.cmd",
        ".tools/f2/Scripts/f2.exe", ".tools/f2/Scripts/f2.cmd",
    ),
    "douyin-downloader": (
        ".tools/douyin-downloader/douyin-downloader.exe",
        ".tools/douyin-downloader/douyin-downloader",
        ".tools/douyin-downloader/douyin-downloader.cmd",
        ".tools/douyin-downloader/douyin-downloader-main/run.py",
    ),
}

INSTALL_HINT = (
    "没有找到可用的抖音下载器。任选其一：\n"
    "  · 用 Python 3.12 装 f2：uv tool install f2 --python 3.12\n"
    "    （Python 3.14 上 pyyaml / pydantic-core 没有预编译包，pip 会去编译而失败）\n"
    "  · 装好后如果没进 PATH，可设 V2I_F2_PATH=<f2.exe 完整路径>\n"
    "  · 或把 douyin-downloader 放进 .tools/douyin-downloader/（纯 Python，不挑 3.14）\n"
    "  · 或用 V2I_DOUYIN_CMD 指定任意命令\n"
    "也可以直接用它把视频下下来，再切「本地文件」拖进 v2i。"
)

_ENV_PATHS = {"f2": "V2I_F2_PATH", "douyin-downloader": "V2I_DOUYIN_DOWNLOADER_PATH"}

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def browser_ready() -> bool:
    """浏览器自动化需要 node + Chrome 都在。"""
    if not shutil.which("node"):
        return False
    if os.environ.get("CHROME_PATH") and Path(os.environ["CHROME_PATH"]).exists():
        return True
    return any(Path(p).exists() for p in CHROME_CANDIDATES)


def _which(name: str) -> str | None:
    for candidate in (name, name + ".exe", name + ".cmd"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def custom_command() -> list[str] | None:
    raw = (os.environ.get("V2I_DOUYIN_CMD") or "").strip()
    if not raw:
        return None
    try:
        return shlex.split(raw, posix=False)
    except ValueError:
        return None


def locate(tool: str) -> str | None:
    """找某个具体工具的路径；找不到返回 None。"""
    if tool == "custom":
        return "custom" if custom_command() else None
    if tool == "browser":
        return "browser" if browser_ready() else None

    # 显式指定优先（venv 里的 f2 往往不 import 到 PATH）
    env_key = _ENV_PATHS.get(tool)
    if env_key:
        raw = (os.environ.get(env_key) or "").strip().strip('"')
        if raw:
            path = Path(raw)
            if path.is_dir():
                for name in (tool + ".exe", tool, tool + ".cmd"):
                    candidate = path / name
                    if candidate.exists():
                        return str(candidate)
            elif path.exists():
                return str(path)

    found = _which(tool)
    if found:
        return found
    for rel in _LOCAL_CANDIDATES.get(tool, ()):
        path = PROJECT_ROOT / rel
        if path.exists():
            return str(path)
    return None


def list_tools() -> list[dict]:
    """给界面用：每个工具是否可用、在哪。"""
    rows = []
    for tool in ("browser", "f2", "douyin-downloader", "custom"):
        path = locate(tool)
        rows.append({
            "id": tool,
            "label": TOOL_LABELS[tool],
            "available": bool(path),
            "path": "" if tool == "custom" else (path or ""),
        })
    return rows


def find_downloader(prefer: str = "") -> tuple[str, str] | None:
    """返回 (工具名, 路径)。prefer 为空/auto 时按 f2 → douyin-downloader → custom。"""
    prefer = (prefer or "").strip().lower()
    order = [prefer] if prefer and prefer != "auto" else ["browser", "f2", "douyin-downloader", "custom"]
    for tool in order:
        if tool not in TOOL_LABELS:
            continue
        path = locate(tool)
        if path:
            return tool, path
    return None


def _newest_media(folder: Path) -> Path | None:
    files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _download_via_browser(url: str, out_dir: Path, timeout: int = 900) -> Path | None:
    """用 node + Chrome(CDP) 截获真实播放地址再下载。"""
    import json as _json

    node = shutil.which("node")
    script = PROJECT_ROOT / "tools" / "douyin_browser.mjs"
    if not node or not script.exists():
        return None
    try:
        proc = subprocess.run(
            [node, str(script), url, "35000"],
            capture_output=True, text=True, timeout=max(60, min(timeout, 180)),
            encoding="utf-8", errors="replace", cwd=str(PROJECT_ROOT),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    payload = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            payload = _json.loads(line)
            break
        except ValueError:
            continue
    if not payload or not payload.get("playUrls"):
        return None

    import requests

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        "Referer": "https://www.douyin.com/",
        "Accept": "*/*",
    }
    for candidate in payload["playUrls"][:3]:
        try:
            resp = requests.get(candidate, headers=headers, stream=True, timeout=90)
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "video" not in ctype and "octet-stream" not in ctype:
            continue
        target = out_dir / "douyin-video.mp4"
        size = 0
        try:
            with target.open("wb") as fh:
                for chunk in resp.iter_content(1 << 16):
                    if chunk:
                        fh.write(chunk)
                        size += len(chunk)
        except OSError:
            continue
        if size > 100_000:
            try:
                (out_dir / "douyin-meta.json").write_text(
                    _json.dumps({"desc": payload.get("desc") or "", "url": url},
                                ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass
            return target
    return None


def _cookie_string(cookies: Path | None) -> str:
    """从 Netscape cookies.txt 里拼出抖音相关的 cookie 字符串（f2 -k 要的是字符串）。"""
    if not cookies or not Path(cookies).exists():
        return ""
    pairs = []
    try:
        for line in Path(cookies).read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip() or line.startswith("# ") or line.count("\t") < 6:
                continue
            parts = line.split("\t")
            domain, name, value = parts[0].lstrip("#"), parts[5], parts[6]
            if "douyin" in domain and name and value:
                pairs.append(f"{name}={value}")
    except OSError:
        return ""
    return "; ".join(pairs)


def _write_downloader_config(workdir: Path, cookies: Path | None, proxy: str) -> str:
    """给 douyin-downloader（run.py 版）生成一份最小配置。"""
    jar = {}
    if cookies and Path(cookies).exists():
        for line in Path(cookies).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.count("\t") < 6:
                continue
            parts = line.split("\t")
            if "douyin" in parts[0]:
                jar[parts[5]] = parts[6]
    wanted = ("ttwid", "odin_tt", "sessionid", "sessionid_ss", "sid_guard",
              "uid_tt", "passport_csrf_token", "s_v_web_id", "__ac_signature", "__ac_nonce")
    lines = [
        "video: true", "music: false", "cover: false", "avatar: false", "json: false",
        "folderstyle: false", "download_pinned: true", "mode:", "  - post",
        "cookies:",
    ]
    for key in ("ttwid", "odin_tt"):
        lines.append(f"  {key}: \"{jar.get(key, '')}\"")
    for key in wanted:
        if key in jar and key not in ("ttwid", "odin_tt"):
            lines.append(f"  {key}: \"{jar[key]}\"")
    if proxy:
        lines.append(f"proxy: \"{proxy}\"")
    path = workdir / "downloader.config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _run(cmd: list[str], out_dir: Path, timeout: int) -> Path | None:
    if cmd and cmd[0].lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd
    env = None
    exe = Path(cmd[0]) if cmd else None
    # run.py 版：依赖装在 .tools/douyin-downloader/deps
    if len(cmd) > 1 and cmd[1].endswith("run.py"):
        deps = Path(cmd[1]).parents[2] / "deps"
        if deps.is_dir():
            env = dict(os.environ)
            env["PYTHONPATH"] = str(deps) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if exe and exe.exists():
        # .tools/f2 这种 --target 安装：包在 exe 的上两级目录，需要 PYTHONPATH
        pkg_root = exe.parent.parent
        if (pkg_root / "f2").is_dir():
            env = dict(os.environ)
            env["PYTHONPATH"] = str(pkg_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace", env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _newest_media(out_dir)


def _run_tool(name: str, exe: str, url: str, out_dir: Path,
              cookies: Path | None, proxy: str, timeout: int) -> Path | None:
    if name == "browser":
        return _download_via_browser(url, out_dir, timeout)
    if name == "custom":
        cmd = [part.replace("{url}", url).replace("{dir}", str(out_dir))
               for part in (custom_command() or [])]
        return _run(cmd, out_dir, timeout) if cmd else None
    if name == "f2":
        cmd = [exe, "dy", "-u", url, "-p", str(out_dir), "-M", "one"]
        cookie = _cookie_string(cookies)
        if cookie:
            cmd += ["-k", cookie]
        if proxy:
            cmd += ["-P", proxy]
        return _run(cmd, out_dir, timeout)
    if exe.endswith("run.py"):
        cfg = _write_downloader_config(out_dir, cookies, proxy)
        return _run([sys.executable, exe, "-u", url, "-p", str(out_dir), "-c", cfg],
                    out_dir, timeout)
    return _run([exe, "-u", url, "-o", str(out_dir)], out_dir, timeout)


def download(url: str, workdir: Path, cookies: Path | None = None,
             prefer: str = "", timeout: int = 900, proxy: str = "") -> Path | None:
    """按指定（或 auto 顺序）依次尝试下载器，直到拿到视频。"""
    out_dir = workdir / "downloaded"
    out_dir.mkdir(parents=True, exist_ok=True)

    prefer = (prefer or "").strip().lower()
    order = [prefer] if prefer and prefer != "auto" else ["browser", "f2", "douyin-downloader", "custom"]
    for name in order:
        if name not in TOOL_LABELS:
            continue
        exe = locate(name)
        if not exe:
            continue
        media = _run_tool(name, exe, url, out_dir, cookies, proxy, timeout)
        if media:
            return media
    return None
