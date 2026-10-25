"""人声 / 伴奏分离：调用 audio-separator（UVR + onnx 模型）把音轨拆成两轨。

重依赖（torch + onnxruntime，解压后 1GB+），默认只看 `.tools/separator/`，
没装就返回 None，由上层决定是否提示。模型第一次运行会自动下载（~50MB）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DIR = PROJECT_ROOT / ".tools" / "separator"
DEFAULT_MODEL = "Kim_Vocal_2.onnx"
PROBE_SECONDS = 45        # 先拿一小段试分，判断值不值得跑全片
PROBE_MAX_CORR = 0.9      # 两轨相关超过它就认为"没有独立伴奏"   # MDX-Net 人声模型（UVR 生态里最常用），~67MB

INSTALL_HINT = (
    "没有找到 audio-separator。想分离人声/伴奏可以装一份：\n"
    "  pip install --target .tools\\separator \"audio-separator[cpu]\"\n"
    "（约 1.5GB，含 torch；模型首次运行会自动下载）"
)


def _local_exe() -> Path | None:
    for name in ("audio-separator.exe", "audio-separator", "audio-separator.cmd"):
        path = LOCAL_DIR / "bin" / name
        if path.exists():
            return path
    return None


def command() -> list[str] | None:
    """返回可执行命令前缀（含 PYTHONPATH 已在 _env 里处理）。"""
    exe = _local_exe()
    if exe:
        return [str(exe)]
    found = shutil.which("audio-separator")
    if found:
        return [found]
    if (LOCAL_DIR / "audio_separator").is_dir():
        return [sys.executable, "-m", "audio_separator.cli"]
    return None


def available() -> bool:
    return command() is not None


def _env(proxy: str = "", ffmpeg: str = "") -> dict:
    env = dict(os.environ)
    if LOCAL_DIR.is_dir():
        env["PYTHONPATH"] = str(LOCAL_DIR) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
    env.setdefault("PYTHONUTF8", "1")

    # audio-separator 内部用 ffmpeg 解码；把 imageio 的静态 ffmpeg 放进 PATH
    if ffmpeg and Path(ffmpeg).exists():
        shim_dir = PROJECT_ROOT / ".tools" / "bin"
        shim_dir.mkdir(parents=True, exist_ok=True)
        shim = shim_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not shim.exists():
            try:
                shutil.copyfile(ffmpeg, shim)
            except OSError:
                pass
        env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")

    # 模型从 GitHub release 下载；国内网络建议给代理
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    return env


MODEL_MIRRORS = (
    "https://ghfast.top/https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/{name}",
    "https://ghproxy.net/https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/{name}",
    "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/{name}",
)
CHECKS_MIRRORS = (
    "https://cdn.jsdelivr.net/gh/TRvlvr/application_data@main/filelists/download_checks.json",
    "https://ghfast.top/https://raw.githubusercontent.com/TRvlvr/application_data/main/filelists/download_checks.json",
    "https://raw.githubusercontent.com/TRvlvr/application_data/main/filelists/download_checks.json",
)


def _download(urls: tuple[str, ...], target: Path, label: str) -> bool:
    """从镜像列表里逐个试，成功即返回（国内 raw.githubusercontent 常被重置）。"""
    import requests

    if target.exists() and target.stat().st_size > 1000:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    for url in urls:
        try:
            print(f"    下载 {label}：{url.split('/')[2]} …", flush=True)
            with requests.get(url, stream=True, timeout=180,
                              headers={"User-Agent": "Mozilla/5.0"}) as resp:
                resp.raise_for_status()
                tmp = target.with_suffix(target.suffix + ".part")
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_content(1 << 20):
                        if chunk:
                            fh.write(chunk)
                tmp.replace(target)
            return True
        except Exception as exc:  # noqa: BLE001 - 换下一个镜像
            print(f"    失败（{type(exc).__name__}），换下一个镜像", flush=True)
    return False


def ensure_model(model: str = DEFAULT_MODEL) -> bool:
    """模型不齐时自动从镜像下载（含 audio-separator 需要的 download_checks.json）。"""
    model_dir = LOCAL_DIR / "models"
    checks = _download(CHECKS_MIRRORS, model_dir / "download_checks.json", "模型清单")
    if not checks:
        return False
    weight = model_dir / model
    return _download(tuple(u.format(name=model) for u in MODEL_MIRRORS), weight, model)


def stem_similarity(vocals: Path, instrumental: Path) -> float | None:
    """两轨相关系数；越接近 1 说明"分"出来的其实是同一段音频。"""
    try:
        import numpy as np

        v = _decode_mono(vocals)
        i = _decode_mono(instrumental)
        n = min(len(v), len(i))
        if n < 1024:
            return None
        return float(np.corrcoef(v[:n], i[:n])[0, 1])
    except Exception:  # noqa: BLE001 - 只是提示，不影响主流程
        return None


def _decode_mono(path: Path):
    """用 av（faster-whisper 自带）把音频解成单声道 float32，避免额外依赖。"""
    import av
    import numpy as np

    chunks = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        for frame in container.decode(stream):
            arr = frame.to_ndarray()
            chunks.append(arr.mean(axis=0) if arr.ndim == 2 else arr)
    if not chunks:
        raise ValueError("解码为空")
    return np.concatenate(chunks).astype("float32")


def _has_companiment(audio: Path, workdir: Path, ffmpeg: str,
                     model: str, proxy: str, timeout: int) -> tuple[bool, float | None]:
    """抽 45 秒试分一次，判断这条音频到底有没有独立伴奏。

    返回 (是否需要跑全片, 试分得到的两轨相关系数)。
    """
    import subprocess as sp

    probe_dir = workdir / "probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    clip = probe_dir / "clip.wav"
    if not clip.exists():
        conv = sp.run([ffmpeg, "-y", "-loglevel", "error", "-ss", "00:00:20",
                       "-t", str(PROBE_SECONDS), "-i", str(audio),
                       "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(clip)],
                      capture_output=True, timeout=300)
        if conv.returncode != 0 or not clip.exists():
            return True, None      # 切不出来就老老实实跑全片

    out_dir = probe_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = command() + [str(clip), "--model_filename", model,
                       "--output_dir", str(out_dir),
                       "--model_file_dir", str(LOCAL_DIR / "models"),
                       "--output_format", "WAV"]
    try:
        proc = sp.run(cmd, capture_output=True, text=True, timeout=max(120, timeout // 3),
                      encoding="utf-8", errors="replace", env=_env(proxy, ffmpeg))
    except (OSError, sp.TimeoutExpired):
        return True, None
    if proc.returncode != 0:
        return True, None

    produced = sorted(p for p in out_dir.rglob("*") if p.suffix.lower() in (".wav", ".mp3", ".flac"))
    vocals = next((p for p in produced if "instrument" not in p.stem.lower() and "vocal" in p.stem.lower()), None)
    instr = next((p for p in produced if "instrument" in p.stem.lower()), None)
    if not vocals or not instr:
        return True, None
    corr = stem_similarity(vocals, instr)
    if corr is None:
        return True, None
    return corr <= PROBE_MAX_CORR, corr


def separate(audio: Path, workdir: Path, model: str = DEFAULT_MODEL,
             timeout: int = 1800, proxy: str = "", ffmpeg: str = "",
             probe: bool = True) -> dict:
    """把人声/伴奏拆到 workdir/vocals.mp3 与 workdir/instrumental.mp3。"""
    base = command()
    if not base:
        return None
    audio = Path(audio)
    if not audio.exists():
        return None

    out_dir = workdir / "separated"
    out_dir.mkdir(parents=True, exist_ok=True)
    # libsndfile 认不了 m4a/aac，先统一转成 wav 再喂给分离器
    input_audio = audio
    if audio.suffix.lower() not in (".wav", ".flac", ".mp3", ".ogg"):
        if not ffmpeg or not Path(ffmpeg).exists():
            return None
        wav = workdir / "separate-input.wav"
        if not wav.exists():
            conv = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(audio),
                 "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(wav)],
                capture_output=True, timeout=600,
            )
            if conv.returncode != 0 or not wav.exists():
                return None
        input_audio = wav

    model_dir = LOCAL_DIR / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    if not (model_dir / model).exists():
        ensure_model(model)

    # 先花 ~20 秒试分 45 秒，判断有没有必要跑全片（口播类常常没有独立伴奏）
    if probe and ffmpeg and Path(ffmpeg).exists():
        need, probe_corr = _has_companiment(input_audio, workdir, ffmpeg, model, proxy, timeout)
        if not need:
            print(f"    试分结论：人声/伴奏两轨相关 {probe_corr:.2f}，这条音频没有独立伴奏，跳过全片分离", flush=True)
            return {"skipped": True, "reason": "no_companiment", "corr": probe_corr}
    cmd = base + [
        str(input_audio),
        "--model_filename", model,
        "--output_dir", str(out_dir),
        "--model_file_dir", str(model_dir),
        "--output_format", "MP3",
        "--output_bitrate", "192k",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace", env=_env(proxy, ffmpeg))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        tail = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()][-4:]
        for line in tail:
            print(f"    {line[:160]}", flush=True)
        return None

    produced = sorted(p for p in out_dir.rglob("*") if p.suffix.lower() in (".mp3", ".wav", ".flac"))
    if not produced:
        return None

    result: dict[str, Path] = {}
    for path in produced:
        low = path.stem.lower()
        # 注意顺序：伴奏文件名里也可能带模型名的 "vocal"（如 Kim_Vocal_2），先判伴奏
        if "instrument" in low or "accompan" in low or "karaoke" in low:
            kind = "instrumental"
        elif "vocal" in low:
            kind = "vocals"
        else:
            continue
        target = workdir / f"{kind}.mp3"
        try:
            shutil.copyfile(path, target)
            result[kind] = target
        except OSError:
            pass

    if "vocals" in result and "instrumental" in result:
        corr = stem_similarity(result["vocals"], result["instrumental"])
        result["corr"] = corr
        if corr is not None and corr > PROBE_MAX_CORR:
            print(f"    提示：人声轨与伴奏轨相关系数 {corr:.2f}（接近 1），"
                  f"这条音频可能本来就没有独立伴奏，伴奏里会有人声残留", flush=True)
    return result
