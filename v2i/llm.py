"""大模型客户端：OpenAI 兼容协议（DeepSeek / OpenAI / Moonshot / SiliconFlow / Ollama）。"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .config import Config


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    failures: int = 0

    def add(self, prompt: int, completion: int) -> None:
        self.prompt_tokens += int(prompt or 0)
        self.completion_tokens += int(completion or 0)
        self.calls += 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def _extract_json(text: str) -> dict:
    """从模型输出里挖出第一个完整 JSON 对象（容忍 ```json 围栏、前后废话）。"""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise LLMError(f"模型没有返回 JSON：{text[:200]}")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = text[start:i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    fixed = re.sub(r",\s*([}\]])", r"\1", blob)
                    return json.loads(fixed)
    raise LLMError(f"JSON 不完整：{text[:200]}")


@dataclass
class LLM:
    cfg: Config
    usage: Usage = field(default_factory=Usage)
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self):
        self.cfg.resolve_llm(required=True)
        self.session.headers.update({
            "Authorization": f"Bearer {self.cfg.api_key}" if self.cfg.api_key else "Bearer local",
            "Content-Type": "application/json",
        })

    @property
    def endpoint(self) -> str:
        return self.cfg.base_url.rstrip("/") + "/chat/completions"

    def chat(self, messages: list[dict], json_mode: bool = False,
             stage: str = "",
             model: str | None = None, max_tokens: int | None = None,
             temperature: float | None = None) -> str:
        payload = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error = None
        started = time.time()
        for attempt in range(3):
            try:
                resp = self.session.post(self.endpoint, json=payload, timeout=(15, 300))
                if resp.status_code == 200:
                    data = resp.json()
                    usage = data.get("usage") or {}
                    self.usage.add(usage.get("prompt_tokens"), usage.get("completion_tokens"))
                    self._record(usage, stage, time.time() - started)
                    choices = data.get("choices") or []
                    if not choices:
                        raise LLMError(f"响应没有 choices：{data}")
                    return choices[0]["message"]["content"] or ""
                if resp.status_code in (401, 403):
                    raise LLMError(
                        f"鉴权失败（{resp.status_code}）：检查 API key 是否正确、是否有该模型权限。"
                        f" 响应：{resp.text[:200]}"
                    )
                if resp.status_code == 402:
                    raise LLMError(f"余额不足（402）：{resp.text[:200]}")
                if resp.status_code == 404:
                    raise LLMError(f"模型或路径不存在（404）：model={payload['model']} url={self.endpoint}")
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error = LLMError(f"HTTP {resp.status_code}：{resp.text[:200]}")
                    time.sleep(3 * (attempt + 1))
                    continue
                raise LLMError(f"HTTP {resp.status_code}：{resp.text[:300]}")
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                time.sleep(3 * (attempt + 1))
            except LLMError:
                raise
        self.usage.failures += 1
        self._record({}, stage, time.time() - started, ok=False)
        raise LLMError(f"调用失败（已重试 3 次）：{last_error}")

    def _record(self, usage: dict, stage: str, seconds: float, ok: bool = True) -> None:
        if not getattr(self, "log_calls", True):
            return
        try:
            from . import usage as usage_mod

            usage_mod.log_call(
                task=getattr(self, "task", "") or "",
                stage=stage or getattr(self, "stage", "") or "chat",
                model=getattr(self.cfg, "model", "") or "",
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                seconds=seconds,
                ok=ok,
            )
        except Exception:  # 记账失败不能影响主流程
            pass

    def chat_json(self, messages: list[dict], **kwargs) -> dict:
        text = self.chat(messages, json_mode=True, **kwargs)
        try:
            return _extract_json(text)
        except Exception:
            # 再给一次机会，把上一次的错误输出回喂
            repair = messages + [
                {"role": "assistant", "content": text[:2000]},
                {"role": "user", "content": "上面的输出不是合法 JSON。请只输出一个合法 JSON 对象，不要任何解释、不要代码围栏。"},
            ]
            return _extract_json(self.chat(repair, json_mode=True, **kwargs))


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"缺少 prompt 文件：{path}")
    return path.read_text(encoding="utf-8")
