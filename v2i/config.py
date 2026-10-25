"""配置：.env + 环境变量 + 命令行覆盖，三层合一。"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import settings as runtime_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# GUI 上传的 cookies.txt 默认落在这里（有就自动生效，命令行也一样）
DEFAULT_COOKIES = PROJECT_ROOT / ".cookies" / "cookies.txt"


def default_cookies() -> Path | None:
    return DEFAULT_COOKIES if DEFAULT_COOKIES.exists() else None

# 常见 OpenAI 兼容端点。model 只是默认值，可用 V2I_MODEL / --model 覆盖。
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "env": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "env": "OPENAI_API_KEY",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2-0905-preview",
        "env": "MOONSHOT_API_KEY",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "env": "SILICONFLOW_API_KEY",
    },
    "ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen3:8b",
        "env": None,  # 本地不需要 key
    },
}

_LLM_KEYS = ("V2I_MODEL", "V2I_BASE_URL", "V2I_API_KEY")


def load_dotenv(*paths: Path) -> None:
    """极简 .env 解析：KEY=VALUE，忽略注释与空行，不覆盖已有环境变量。"""
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            # 空值不要写进 os.environ：否则之后往 .env 补 key 会被旧的空值挡住
            if key and value and key not in os.environ:
                os.environ[key] = value


def _env(name: str, default=None):
    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else default


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    # 运行目录
    runs_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "runs")

    # ASR
    asr_model: str = "small"
    asr_device: str = "auto"
    asr_compute: str = "auto"
    language: str | None = None
    beam_size: int = 5
    batch_size: int = 0          # >1 时启用 BatchedInferencePipeline（GPU 更有意义）
    word_timestamps: bool = False

    # 下载
    extract_audio: bool = True        # 拿到视频时顺带抽出 audio.m4a
    douyin_downloader: str = ""       # 抖音兜底下载器：auto / f2 / douyin-downloader / custom
    keep_video: bool = False          # 下载原视频（可在界面里播放），否则只下音频
    cookies: Path | None = None
    cookies_from_browser: str | None = None   # chrome | edge | firefox | brave …
    proxy: str | None = None
    ffmpeg: str | None = None
    prefer_subs: bool = True
    max_height: int = 0

    # 文案风格：raw（原样）/ light（轻度整理）/ script（口播稿）
    copy_style: str = "light"

    # 分块
    chunk_chars: int = 1600
    overlap_sentences: int = 1

    # LLM
    provider: str = "deepseek"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    workers: int = 2

    def __post_init__(self):
        self.resolve_llm(required=False)

    # ---- 派生 ----
    def resolve_llm(self, required: bool = True) -> None:
        preset = PROVIDERS.get(self.provider, {})
        key_env = preset.get("env")
        self.base_url = self.base_url or _env("V2I_BASE_URL") or preset.get("base_url", "")
        if not self.api_key:
            # V2I_API_KEY 由「设置」页写入，优先级高于 provider 自带的 key 变量
            self.api_key = _env("V2I_API_KEY") or (_env(key_env) if key_env else "") or ""
        if not self.model:
            self.model = _env("V2I_MODEL") or preset.get("model", "")
        if required and not self.api_key and key_env:
            raise ConfigError(
                f"缺少 {key_env}。到界面「设置」里填 API key（立即生效），或写进 {PROJECT_ROOT / '.env'}，"
                f"或设置环境变量 {key_env}；也可用 --provider ollama 走本地模型。"
            )
        if required and not self.base_url:
            raise ConfigError(f"provider={self.provider} 没有 base_url，请设置 V2I_BASE_URL。")

    @property
    def device(self) -> str:
        if self.asr_device != "auto":
            return self.asr_device
        try:
            import ctranslate2

            return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            return "cpu"

    def compute_type(self, device: str) -> str:
        if self.asr_compute != "auto":
            return self.asr_compute
        return "float16" if device == "cuda" else "int8"

    @property
    def ffmpeg_exe(self) -> str | None:
        if self.ffmpeg:
            return self.ffmpeg
        found = shutil.which("ffmpeg")
        if found:
            return found
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None

    # ---- 构造 ----
    @classmethod
    def from_env(cls, **overrides) -> "Config":
        # 先刷运行时设置（热加载），再读 .env；设置页改完不需要重启
        runtime_settings.apply()
        load_dotenv(PROJECT_ROOT / ".env", Path.cwd() / ".env")
        cfg = cls(
            asr_model=_env("V2I_ASR_MODEL", "small"),
            asr_device=_env("V2I_ASR_DEVICE", "auto"),
            asr_compute=_env("V2I_ASR_COMPUTE", "auto"),
            language=_env("V2I_LANGUAGE", None),
            provider=_env("V2I_PROVIDER", "deepseek"),
            model=_env("V2I_MODEL", None),
            max_tokens=int(_env("V2I_MAX_TOKENS", "4096")),
            temperature=float(_env("V2I_TEMPERATURE", "0.3")),
            workers=int(_env("V2I_WORKERS", "2")),
            chunk_chars=int(_env("V2I_CHUNK_CHARS", "1600")),
            copy_style=_env("V2I_COPY_STYLE", "light"),
            cookies=Path(_env("V2I_COOKIES")) if _env("V2I_COOKIES") else default_cookies(),
            keep_video=_env("V2I_KEEP_VIDEO", "").lower() in ("1", "true", "yes", "on"),
            douyin_downloader=_env("V2I_DOUYIN_DOWNLOADER", ""),
            extract_audio=_env("V2I_EXTRACT_AUDIO", "1").lower() not in ("0", "false", "no", "off"),
            cookies_from_browser=_env("V2I_COOKIES_FROM_BROWSER", None),
            proxy=_env("V2I_PROXY", None),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        cfg.resolve_llm(required=False)
        return cfg
