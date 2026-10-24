"""转写层（ASR）：faster-whisper，CPU int8 / GPU float16 自适应。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .config import Config
from .util import clean_cjk, fmt_ts, read_json, write_json


class AsrError(RuntimeError):
    pass


def _prepare_env() -> None:
    # 国内直连 huggingface.co 基本不可用，默认走镜像（用户显式设置则不覆盖）
    mirror = os.environ.get("V2I_HF_MIRROR", "https://hf-mirror.com")
    os.environ.setdefault("HF_ENDPOINT", mirror)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    # 镜像站不支持 Xet CAS 协议，不关掉会 401（hf-xet >= 1.x 默认开启）
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def load_model(cfg: Config):
    _prepare_env()
    from faster_whisper import WhisperModel

    device = cfg.device
    compute = cfg.compute_type(device)
    print(f"  加载模型 {cfg.asr_model}（device={device}, compute_type={compute}）", flush=True)
    try:
        return WhisperModel(cfg.asr_model, device=device, compute_type=compute,
                            download_root=None, local_files_only=False), device, compute
    except Exception as exc:  # noqa: BLE001
        if device == "cuda":
            print(f"  ! GPU 初始化失败（{exc}），回落 CPU int8", flush=True)
            return WhisperModel(cfg.asr_model, device="cpu", compute_type="int8"), "cpu", "int8"
        raise AsrError(f"模型加载失败：{exc}") from exc


def transcribe(audio: Path, cfg: Config, workdir: Path, force: bool = False) -> dict:
    audio = Path(audio)
    if not audio.exists():
        raise AsrError(f"音频不存在：{audio}")

    cached = read_json(workdir / "transcript.json")
    if cached and cached.get("segments") and not force:
        print("  已存在 transcript.json，跳过转写", flush=True)
        return cached

    model, device, compute = load_model(cfg)
    started = time.time()

    # 中文（用户指定时）给个提示词，能显著减少繁体与半角标点
    zh_prompt = "以下是普通话内容，请用简体中文转写，标点使用全角符号。" \
        if (cfg.language or "").lower().startswith("zh") else None

    def run(vad: bool):
        kwargs = dict(
            vad_filter=vad,
            vad_parameters={"min_silence_duration_ms": 500} if vad else None,
            beam_size=cfg.beam_size,
            language=cfg.language,
            initial_prompt=zh_prompt,
            condition_on_previous_text=False,   # 长音频防"复读机"
            word_timestamps=cfg.word_timestamps,
        )
        if cfg.batch_size and cfg.batch_size > 1:
            from faster_whisper import BatchedInferencePipeline

            return BatchedInferencePipeline(model=model).transcribe(
                str(audio), batch_size=cfg.batch_size, **kwargs
            )
        return model.transcribe(str(audio), **kwargs)

    if cfg.batch_size and cfg.batch_size > 1:
        print(f"  批量推理 batch_size={cfg.batch_size}", flush=True)

    total = 0.0
    segments: list[dict] = []
    info = None
    for use_vad in (True, False):
        segments_iter, info = run(use_vad)
        total = float(getattr(info, "duration", 0) or 0)
        segments = []
        last_report = 0.0
        for seg in segments_iter:
            text = clean_cjk((seg.text or "").strip())
            if not text:
                continue
            segments.append({
                "id": len(segments),
                "start": round(float(seg.start), 2),
                "end": round(float(seg.end), 2),
                "text": text,
            })
            now = time.time()
            if total and now - last_report > 10:
                last_report = now
                print(f"  转写中 {seg.end / total * 100:5.1f}%  {fmt_ts(seg.end)}/{fmt_ts(total)}", flush=True)
        if segments:
            break
        # 纯音乐/演唱会被 Silero VAD 判成"无人声"，此时关掉 VAD 再来一遍
        if use_vad:
            print("  VAD 过滤后无内容（可能是音乐/演唱），关闭 VAD 重试", flush=True)

    if not segments:
        raise AsrError("转写结果为空：音频可能没有语音，或语言检测失败（可试 --language zh）")

    elapsed = time.time() - started
    meta = read_json(workdir / "meta.json", {}) or {}
    # 本地文件没有元数据时长，用 ASR 探测到的时长回填
    if not meta.get("duration") and total:
        meta["duration"] = round(total, 1)
        meta["duration_text"] = fmt_ts(total)
        write_json(workdir / "meta.json", meta)
    transcript = {
        "meta": meta,
        "source": {"kind": "asr", "audio": str(audio), "lang": getattr(info, "language", None)},
        "asr": {
            "engine": "faster-whisper",
            "model": cfg.asr_model,
            "device": device,
            "compute_type": compute,
            "language": getattr(info, "language", None),
            "language_probability": round(float(getattr(info, "language_probability", 0) or 0), 3),
            "duration": round(total, 2),
            "elapsed_sec": round(elapsed, 1),
            "speed_x": round(total / elapsed, 2) if elapsed > 0 else None,
        },
        "segments": segments,
        "text": " ".join(s["text"] for s in segments).strip(),
    }
    write_json(workdir / "transcript.json", transcript)
    speed = transcript["asr"]["speed_x"]
    print(f"  转写完成：{len(segments)} 段，耗时 {elapsed:.1f}s（{speed}x 实时）", flush=True)
    return transcript
