"""v2i 命令行入口。"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from . import asr as asr_mod
from . import distill as distill_mod
from . import normalize as norm
from . import fetch as fetch_mod
from .config import PROJECT_ROOT, Config, ConfigError
from .util import now_iso, read_json, slugify, write_json


def _out(msg: str = "") -> None:
    print(msg, flush=True)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 参数


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="v2i",
        description="视频 → 文字 → 洞察：yt-dlp 取字幕/音频 + faster-whisper 转写 + 大模型提炼",
    )
    parser.add_argument("--version", action="version", version=f"v2i {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--runs", help="运行目录（默认项目下 runs/）")
        sp.add_argument("--provider", help="deepseek | openai | moonshot | siliconflow | ollama")
        sp.add_argument("--model", help="LLM 模型名，如 deepseek-chat")
        sp.add_argument("--force", action="store_true", help="忽略所有缓存，重跑")

    run = sub.add_parser("run", help="一条命令跑完整流水线")
    run.add_argument("url", help="视频链接（公开可访问）")
    run.add_argument("--mode", default="all", help="summary | points | article | all，可逗号组合")
    run.add_argument("--language", help="指定语言 zh/en，默认自动检测")
    run.add_argument("--asr-model", help="tiny | base | small | medium | large-v3 | turbo")
    run.add_argument("--no-subs", action="store_true", help="忽略原生字幕，强制下载音频转写")
    common(run)

    fetch = sub.add_parser("fetch", help="只做下载（字幕优先，无字幕则下音频）")
    fetch.add_argument("url")
    fetch.add_argument("--no-subs", action="store_true", help="忽略原生字幕")
    common(fetch)

    trans = sub.add_parser("transcribe", help="只做转写（本地音频/视频文件）")
    trans.add_argument("file")
    trans.add_argument("--language")
    trans.add_argument("--asr-model")
    common(trans)

    dist = sub.add_parser("distill", help="只做提炼（已有 transcript.json 的运行目录）")
    dist.add_argument("run_dir")
    dist.add_argument("--mode", default="all")
    common(dist)

    rend = sub.add_parser("render", help="只重渲染 Markdown（不调用大模型，用 out/*.json 的缓存结果）")
    rend.add_argument("run_dir")
    common(rend)

    batch = sub.add_parser("batch", help="批量跑 urls.txt（每行一个链接）")
    batch.add_argument("file")
    batch.add_argument("--mode", default="all")
    batch.add_argument("--language")
    batch.add_argument("--asr-model")
    common(batch)

    doc = sub.add_parser("doctor", help="环境自检")
    doc.add_argument("--ping", action="store_true", help="顺带调一次 LLM，验证 key 与网络")

    web = sub.add_parser("web", help="启动本地 Web GUI")
    web.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    web.add_argument("--host", default="127.0.0.1", help="监听地址（默认只监听本机）")
    web.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    return parser


def _config(args) -> Config:
    overrides = {}
    for field in ("model", "provider", "language", "asr_model"):
        value = getattr(args, field, None)
        if value:
            overrides[field] = value
    if getattr(args, "no_subs", False):
        overrides["prefer_subs"] = False
    return Config.from_env(**overrides)


def _runs(cfg: Config, args) -> Path:
    runs = getattr(args, "runs", None)
    return Path(runs).expanduser().resolve() if runs else cfg.runs_dir


def _modes(value: str) -> tuple[str, ...]:
    value = (value or "all").strip().lower()
    if value in ("all", "*"):
        return distill_mod.MODES
    picked = tuple(m for m in value.replace(" ", "").split(",") if m)
    bad = [m for m in picked if m not in distill_mod.MODES]
    if bad:
        raise SystemExit(f"--mode 不支持：{', '.join(bad)}（可选 {' / '.join(distill_mod.MODES)} / all）")
    return picked or distill_mod.MODES


# ---------------------------------------------------------------- 各命令


def _run_one(url: str, cfg: Config, runs: Path, modes: tuple[str, ...], force: bool) -> Path:
    started = time.time()

    info = fetch_mod.probe(url, cfg)
    vid = str(info.get("id") or "video")
    workdir = runs / f"{slugify(info.get('title') or vid)}-{vid}"
    workdir.mkdir(parents=True, exist_ok=True)
    _out(f"  工作目录：{workdir}")

    result = fetch_mod.fetch(url, cfg, workdir, force=force, info=info)
    if result["segments"]:
        transcript = read_json(workdir / "transcript.json")
    else:
        audio = Path(result["source"]["audio"])
        transcript = asr_mod.transcribe(audio, cfg, workdir, force=force)

    chunks = norm.normalize(transcript, workdir, cfg.chunk_chars, cfg.overlap_sentences)

    manifest = read_json(workdir / "manifest.json", {}) or {}
    manifest.update({
        "url": url,
        "workdir": str(workdir),
        "fetch": {
            "kind": result["source"].get("kind"),
            "lang": result["source"].get("lang"),
            "cached": result.get("cached", False),
            "at": now_iso(),
        },
        "chunks": len(chunks),
        "chars": sum(c["chars"] for c in chunks),
        "asr": transcript.get("asr"),
    })
    write_json(workdir / "manifest.json", manifest)

    try:
        distill_mod.distill(workdir, cfg, modes, force=force)
    except ConfigError as exc:
        # 没有 key 也别把转写白跑：保留 transcript/srt，只跳过提炼
        _out(f"  ! 提炼跳过：{exc}")
        _out(f"    转写产物已保留：{workdir}")
        manifest = read_json(workdir / "manifest.json", {}) or {}
        manifest["distill_skipped"] = str(exc)
        write_json(workdir / "manifest.json", manifest)

    elapsed = time.time() - started
    manifest = read_json(workdir / "manifest.json", {}) or {}
    manifest["elapsed_sec"] = round(elapsed, 1)
    write_json(workdir / "manifest.json", manifest)

    _out("")
    _out(f"  完成，用时 {elapsed:.0f}s：")
    for mode in modes:
        target = workdir / "out" / f"{mode}.md"
        if target.exists():
            _out(f"    - {target}")
    for extra in (workdir / "out" / "digest.json", workdir / "transcript.srt", workdir / "transcript.md"):
        if extra.exists():
            _out(f"    - {extra}")
    return workdir


def _cmd_run(args) -> int:
    cfg = _config(args)
    modes = _modes(args.mode)
    _out(f"→ {args.url}")
    _run_one(args.url, cfg, _runs(cfg, args), modes, args.force)
    return 0


def _cmd_fetch(args) -> int:
    cfg = _config(args)
    runs = _runs(cfg, args)
    info = fetch_mod.probe(args.url, cfg)
    vid = str(info.get("id") or "video")
    workdir = runs / f"{slugify(info.get('title') or vid)}-{vid}"
    workdir.mkdir(parents=True, exist_ok=True)
    result = fetch_mod.fetch(args.url, cfg, workdir, force=args.force, info=info)
    _out(f"\n  产出：{workdir}")
    if result["source"].get("audio"):
        _out(f"  音频：{result['source']['audio']}")
    if result["segments"]:
        _out(f"  字幕：{result['source'].get('subtitle')}")
    return 0


def _cmd_transcribe(args) -> int:
    cfg = _config(args)
    path = Path(args.file).expanduser().resolve()
    if not path.exists():
        _err(f"文件不存在：{path}")
        return 2
    runs = _runs(cfg, args)
    workdir = runs / f"{slugify(path.stem)}-local"
    workdir.mkdir(parents=True, exist_ok=True)
    meta = {
        "title": path.stem,
        "uploader": "本地文件",
        "duration": None,
        "duration_text": "未知",
        "webpage_url": str(path),
        "chapters": [],
    }
    write_json(workdir / "meta.json", meta)
    transcript = asr_mod.transcribe(path, cfg, workdir, force=args.force)
    norm.normalize(transcript, workdir, cfg.chunk_chars, cfg.overlap_sentences)
    _out(f"\n  产出：{workdir}")
    return 0


def _cmd_distill(args) -> int:
    cfg = _config(args)
    workdir = Path(args.run_dir).expanduser().resolve()
    transcript = read_json(workdir / "transcript.json")
    if not transcript:
        _err(f"{workdir} 里没有 transcript.json")
        return 2
    norm.normalize(transcript, workdir, cfg.chunk_chars, cfg.overlap_sentences)
    distill_mod.distill(workdir, cfg, _modes(args.mode), force=args.force)
    _out(f"\n  产出：{workdir / 'out'}")
    return 0


def _cmd_render(args) -> int:
    """用已缓存的 JSON 重新渲染 Markdown：改模板/改排版时不必再花钱调模型。"""
    from . import render

    cfg = _config(args)
    workdir = Path(args.run_dir).expanduser().resolve()
    transcript = read_json(workdir / "transcript.json")
    if not transcript:
        _err(f"{workdir} 里没有 transcript.json")
        return 2
    meta = transcript.get("meta") or read_json(workdir / "meta.json", {}) or {}
    out = workdir / "out"
    written = []
    for mode, builder in (("summary", render.summary_md), ("points", render.points_md)):
        data = read_json(out / f"{mode}.json")
        if not data:
            continue
        write_json(out / f"{mode}.json", data)
        (out / f"{mode}.md").write_text(builder(meta, data, cfg, transcript), encoding="utf-8")
        written.append(mode)
    article = out / "article.md"
    if article.exists():
        body = article.read_text(encoding="utf-8")
        if body.startswith("---"):
            parts = body.split("---", 2)
            body = parts[2].lstrip("\n") if len(parts) > 2 else body
        write_json(workdir / "manifest.json", read_json(workdir / "manifest.json", {}) or {})
        article.write_text(render.article_md(meta, body, cfg, transcript), encoding="utf-8")
        written.append("article")
    if not written:
        _err("没有可渲染的产物（out/*.json 不存在）")
        return 2
    _out(f"  已重新渲染：{', '.join(written)} → {out}")
    return 0


def _cmd_batch(args) -> int:
    cfg = _config(args)
    modes = _modes(args.mode)
    path = Path(args.file).expanduser().resolve()
    if not path.exists():
        _err(f"文件不存在：{path}")
        return 2
    # utf-8-sig：兼容记事本/Excel 导出带 BOM 的 urls.txt，否则第一行会带着 BOM 当 URL
    urls = [ln.strip().lstrip("\ufeff") for ln in path.read_text(encoding="utf-8-sig").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    if not urls:
        _err("urls.txt 里没有链接")
        return 2
    runs = _runs(cfg, args)
    ok, failed = 0, []
    for i, url in enumerate(urls, 1):
        _out(f"\n[{i}/{len(urls)}] {url}")
        try:
            _run_one(url, cfg, runs, modes, args.force)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            _err(f"  ✗ 失败：{exc}")
            failed.append((url, str(exc)))
    _out(f"\n批量结束：成功 {ok}，失败 {len(failed)}")
    for url, err in failed:
        _out(f"  - {url}：{err[:120]}")
    return 0 if not failed else 1


def _cmd_doctor(args) -> int:
    cfg = _config(args)
    _out(f"v2i {__version__} ｜ {PROJECT_ROOT}")
    problems: list[str] = []

    def check(name: str, fn, hint: str = "") -> None:
        try:
            detail = fn()
            _out(f"  ✓ {name}" + (f"  {detail}" if detail else ""))
        except Exception as exc:  # noqa: BLE001
            _out(f"  ✗ {name}  {exc}")
            if hint:
                _out(f"      → {hint}")
            problems.append(name)

    def py_version():
        if sys.version_info < (3, 10):
            raise RuntimeError(f"需要 Python ≥ 3.10，当前 {sys.version.split()[0]}")
        return f"Python {sys.version.split()[0]}"

    def ytdlp():
        import yt_dlp

        return f"yt-dlp {yt_dlp.version.__version__}"

    def whisper():
        import faster_whisper

        device = cfg.device
        return f"faster-whisper {faster_whisper.__version__}（device={device}, compute={cfg.compute_type(device)}）"

    def gpu():
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
        return f"CUDA 设备 {count} 个" if count else "无 CUDA，走 CPU"

    def ffmpeg():
        exe = cfg.ffmpeg_exe
        if not exe:
            raise RuntimeError("找不到 ffmpeg")
        return exe

    def llm():
        cfg.resolve_llm(required=True)
        key = cfg.api_key
        masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("已配置" if key else "本地免 key")
        return f"{cfg.provider} / {cfg.model} ｜ {cfg.base_url} ｜ key={masked}"

    def runs_dir():
        target = _runs(cfg, args)
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return str(target)

    check("Python", py_version, "装 Python 3.10+")
    check("yt-dlp", ytdlp, "pip install yt-dlp")
    check("faster-whisper", whisper, "pip install faster-whisper")
    check("CUDA", gpu)
    check("ffmpeg", ffmpeg, "pip install imageio-ffmpeg 或 winget install Gyan.FFmpeg")
    check("运行目录", runs_dir)
    check("LLM 配置", llm, "把 DEEPSEEK_API_KEY 写进 .env")

    if args.ping and not problems:
        try:
            from .llm import LLM

            llm_client = LLM(cfg)
            reply = llm_client.chat([{"role": "user", "content": "只回复：pong"}], max_tokens=8)
            _out(f"  ✓ LLM 连通性  {reply.strip()[:40]}")
        except Exception as exc:  # noqa: BLE001
            _out(f"  ✗ LLM 连通性  {exc}")
            problems.append("LLM 连通性")

    _out("")
    if problems:
        _out(f"  还有 {len(problems)} 项待处理：{', '.join(problems)}")
        return 1
    _out("  环境就绪，可以跑了：v2i run \"<视频链接>\"")
    return 0


def _cmd_web(args) -> int:
    from .web import serve

    serve(port=args.port, host=args.host, open_browser=not args.no_browser)
    return 0


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    handlers = {
        "run": _cmd_run,
        "fetch": _cmd_fetch,
        "transcribe": _cmd_transcribe,
        "distill": _cmd_distill,
        "render": _cmd_render,
        "batch": _cmd_batch,
        "doctor": _cmd_doctor,
        "web": _cmd_web,
    }
    try:
        return handlers[args.cmd](args)
    except ConfigError as exc:
        _err(f"配置错误：{exc}")
        return 2
    except fetch_mod.FetchError as exc:
        _err(f"下载失败：{exc}")
        return 3
    except asr_mod.AsrError as exc:
        _err(f"转写失败：{exc}")
        return 4
    except KeyboardInterrupt:
        _err("\n已中断")
        return 130
    except Exception as exc:  # noqa: BLE001
        from .llm import LLMError

        if isinstance(exc, LLMError):
            _err(f"提炼失败：{exc}")
            return 5
        raise


if __name__ == "__main__":
    raise SystemExit(main())
