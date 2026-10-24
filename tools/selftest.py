"""离线自检：不联网、不调模型，验证字幕解析 / 分块 / JSON 抽取 / 引用校验。

    python tools/selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

try:  # GBK 控制台下 ✓ 会直接抛 UnicodeEncodeError，降级成 ? 即可
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from v2i.distill import _check_citations  # noqa: E402
from v2i.fetch import parse_subtitle  # noqa: E402
from v2i.llm import _extract_json  # noqa: E402
from v2i.normalize import build_chunks  # noqa: E402
from v2i.util import fmt_ts, parse_ts  # noqa: E402

AUTO_VTT = """WEBVTT
Kind: captions
Language: zh

00:00:01.000 --> 00:00:03.500
<c>大家好</c>

00:00:03.500 --> 00:00:06.000
<c>大家好</c>
今天聊聊流水线

00:00:06.000 --> 00:00:08.000
今天聊聊流水线

00:00:08.000 --> 00:00:11.250
先说结论&nbsp;它不复杂

00:00:11.250 --> 00:00:14.000
先说结论 它不复杂
"""

SRT = """1
00:00:01,000 --> 00:00:03,000
第一句。

2
00:00:03,200 --> 00:00:05,400
第二句，带逗号。
"""


def test_ts():
    assert fmt_ts(431.2) == "07:11"
    assert fmt_ts(4031) == "01:07:11"
    assert parse_ts("07:11") == 431
    assert parse_ts("01:07:11") == 4031
    print("✓ 时间码")


def test_vtt(tmp: Path):
    path = tmp / "auto.zh.vtt"
    path.write_text(AUTO_VTT, encoding="utf-8")
    segs = parse_subtitle(path)
    assert segs, "VTT 没有解析出内容"
    # 滚动字幕应被合并：5 条原始 cue -> 明显更少
    assert len(segs) <= 3, f"去重失败，仍有 {len(segs)} 段"
    assert all("<" not in s["text"] for s in segs), "内联标签没有清掉"
    assert "&nbsp;" not in " ".join(s["text"] for s in segs)
    assert segs[0]["start"] == 1.0
    assert all(b["start"] >= a["start"] for a, b in zip(segs, segs[1:])), "时间不单调"
    print(f"✓ VTT 解析（{len(segs)} 段，已去滚动重复）")


def test_srt(tmp: Path):
    path = tmp / "manual.zh.srt"
    path.write_text(SRT, encoding="utf-8")
    segs = parse_subtitle(path)
    assert len(segs) == 2
    assert segs[1]["start"] == 3.2
    print("✓ SRT 解析")


def test_chunks():
    segments = [
        {"id": i, "start": i * 10.0, "end": i * 10.0 + 9.5,
         "text": "这是一句测试用的中文内容，用来验证分块逻辑是否正确。" * 3}
        for i in range(20)
    ]
    chunks = build_chunks(segments, chunk_chars=600, overlap=1)
    assert len(chunks) > 1
    assert all("[00:" in c["annotated"] or "[0" in c["annotated"] for c in chunks)
    assert all(c["end"] >= c["start"] for c in chunks)
    assert chunks[0]["start"] == 0.0
    # 覆盖度：最后一块应覆盖到接近末尾
    assert chunks[-1]["end"] >= segments[-1]["end"] - 1
    print(f"✓ 分块（{len(chunks)} 块，带时间锚点）")


def test_json():
    fenced = '```json\n{"a": 1, "b": [1, 2],}\n```'
    assert _extract_json(fenced)["a"] == 1, "带尾逗号/围栏的 JSON 没修好"
    noisy = '好的，结果如下：{"gist": "x", "points": []} 希望有帮助'
    assert _extract_json(noisy)["gist"] == "x"
    nested = '{"a": {"b": "}"}, "c": 2}'
    assert _extract_json(nested)["c"] == 2, "字符串里的花括号会骗过解析器"
    print("✓ JSON 抽取（围栏 / 尾逗号 / 嵌套花括号）")


def test_citations():
    chunks = [{"start": 0.0, "end": 60.0}, {"start": 60.0, "end": 120.0}]
    text = "观点一[00:12]，观点二[01:30]，乱写的[09:99]，越界[05:00]。"
    bad = _check_citations(text, chunks)
    assert "00:12" not in bad
    assert "01:30" not in bad
    assert "05:00" in bad
    assert "09:99" in bad
    print(f"✓ 引用校验（越界命中：{bad}）")


def main():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        test_ts()
        test_vtt(tmp_path)
        test_srt(tmp_path)
        test_chunks()
        test_json()
        test_citations()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
