"""开发用 mock LLM：兼容 OpenAI /v1/chat/completions，用于在没有 API key 时验证提炼链路。

只做一件事：根据 system prompt 判断当前是 map / summary / points / article，
返回结构合法、带真实时间码的假数据。**不要用于生产**。

    python tools/mock_llm.py --port 8765
    set V2I_BASE_URL=http://127.0.0.1:8765/v1
    set V2I_PROVIDER=ollama
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TS_RE = re.compile(r"\[(\d{1,2}:\d{2})\]")

SAMPLE_REWRITE = """短视频越来越难做，原因不是竞争激烈。

真正变的是平台的推荐机制。过去平台按关注分发，你有粉丝就有播放；现在平台按完播率分发，粉丝多也未必有人看。规则一换，过去那套涨粉经验就失效了。

第二个变化是用户的耐心。前三秒留不住人，后面做得再好，观众也看不到。于是开头三秒成了新的生死线。

第三个变化是同质化。一个爆款出现，一周内就有上千个模仿者，观众很快就腻了。

所以能活下来的创作者，不是最会模仿的，而是能持续提供独特信息增量的人。与其追热点，不如把一个小领域挖深，做别人不愿意做的脏活累活。

最后一句：工具只是放大器，它放大的是你的判断力，不是你的懒惰。"""

MAP_TEMPLATE = {
    "gist": "（mock）这一段在讲一个具体问题",
    "points": [
        {"claim": "（mock）作者给出了一条可被反驳的判断", "evidence": "（mock）原文举了一个例子",
         "ts": "", "type": "claim"},
        {"claim": "（mock）这是一条可验证的事实", "evidence": "", "ts": "", "type": "fact"},
    ],
    "quotes": [{"text": "（mock）这是一句最锋利的原话", "ts": ""}],
    "entities": ["（mock）某产品", "（mock）某人"],
    "open_questions": ["（mock）作者没有展开的一点"],
}

SUMMARY = {
    "title": "（mock）一条视频讲了什么",
    "one_liner": "（mock）一句话概括这条视频的核心结论",
    "summary": "（mock）这是一段三百字左右的摘要，用来验证渲染链路是否正常。它应该包含具体信息而不是空话。",
    "key_points": [
        {"point": "（mock）第一个要点", "ts": ""},
        {"point": "（mock）第二个要点", "ts": ""},
        {"point": "（mock）第三个要点", "ts": ""},
    ],
    "timeline": [
        {"ts": "", "event": "（mock）提出问题"},
        {"ts": "", "event": "（mock）给出方法"},
    ],
    "quotes": [{"text": "（mock）金句一条", "ts": ""}],
    "actions": ["（mock）可以立刻做的一件事"],
    "caveats": ["（mock）数据没有出处"],
}

POINTS = {
    "thesis": "（mock）核心主张：结论依赖一个未经验证的前提",
    "points": [
        {"claim": "（mock）论点一", "evidence": "（mock）论据一", "ts": "",
         "type": "claim", "strength": "medium", "counter": "（mock）反例一"},
        {"claim": "（mock）论点二", "evidence": "", "ts": "",
         "type": "opinion", "strength": "weak", "counter": "（mock）反例二"},
    ],
    "structure": [{"part": "提出问题", "from": "", "to": "", "gist": "（mock）抛出问题"}],
    "blind_spots": ["（mock）作者回避了成本问题"],
    "verdict": "（mock）成立一半：方法可行，结论过度外推",
}

ARTICLE = """# （mock）标题：把结论建立在可核查的事实上

导语：这篇文章用来验证渲染链路，所有时间码都来自真实逐字稿。

## 一、先把问题说清楚

作者认为，问题不在于工具本身，而在于使用方式[00:12]。

## 二、方法比结论更重要

他在视频里给了一个具体做法[00:47]，但没有给出数据来源。

## 最后

读完记住三件事：看时间码、看论据、看利益相关。
"""


def _ts_list(text: str) -> list[str]:
    seen: list[str] = []
    for ts in TS_RE.findall(text or ""):
        if ts not in seen:
            seen.append(ts)
    return seen or ["00:00"]


def _fill(obj, timestamps: list[str], ts_key_holder=None):
    """递归把空 ts 填上时间码。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("ts", "from", "to") and isinstance(value, str) and not value:
                obj[key] = timestamps[0]
            elif key in ("from", "to") and isinstance(value, str) and not value:
                obj[key] = timestamps[0]
            else:
                _fill(value, timestamps)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, dict) and "ts" in item and not item.get("ts"):
                item["ts"] = timestamps[i % len(timestamps)]
            else:
                _fill(item, timestamps)


def respond(payload: dict) -> str:
    messages = payload.get("messages") or []
    system = messages[0].get("content", "") if messages else ""
    user = " ".join(m.get("content", "") for m in messages[1:]) if len(messages) > 1 else ""
    stamps = _ts_list(user)

    if "结构化笔记" in system:
        data = json.loads(json.dumps(MAP_TEMPLATE))
        _fill(data, stamps)
        return json.dumps(data, ensure_ascii=False)

    if "摘要卡" in system:
        data = json.loads(json.dumps(SUMMARY))
        _fill(data, stamps)
        return json.dumps(data, ensure_ascii=False)

    if "观点清单" in system:
        data = json.loads(json.dumps(POINTS))
        structure = data.get("structure") or []
        for i, part in enumerate(structure):
            part["from"] = stamps[0]
            part["to"] = stamps[min(i + 1, len(stamps) - 1)]
        data["structure"] = structure
        _fill(data, stamps)
        return json.dumps(data, ensure_ascii=False)

    if "中文编辑" in system or "口播稿作者" in system:
        return SAMPLE_REWRITE

    if "长文" in system:
        body = ARTICLE
        # 把正文里的时间码换成真实存在的时间码，顺便验证引用校验
        counter = 0

        def sub(match):
            nonlocal counter
            value = stamps[counter % len(stamps)]
            counter += 1
            return f"[{value}]"

        return TS_RE.sub(sub, body)

    return json.dumps({"echo": "mock", "stamps": stamps[:3]}, ensure_ascii=False)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
            content = respond(payload)
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"error": str(exc)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = json.dumps({
            "id": "mock-1",
            "object": "chat.completion",
            "model": payload.get("model", "mock"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock LLM listening on http://127.0.0.1:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
