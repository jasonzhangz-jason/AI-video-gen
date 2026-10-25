"""网络连通性检测：给代理配置一个"测延迟"的按钮用。

对几个关键目标各打一次轻量请求，返回毫秒数与成功/失败原因：
- gstatic 204：国际线路通不通（Google 的边缘节点，返回 204 很小）
- DeepSeek API：大模型调用线路
- raw.githubusercontent：模型/清单下载线路（国内经常被重置）
- douyin：抖音页面
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

TARGETS: tuple[tuple[str, str, str], ...] = (
    ("国际线路", "https://www.gstatic.com/generate_204", "GET"),
    ("DeepSeek API", "https://api.deepseek.com/v1/models", "GET"),
    ("GitHub 原始文件", "https://raw.githubusercontent.com/TRvlvr/application_data/main/filelists/download_checks.json", "HEAD"),
    ("抖音", "https://www.douyin.com/", "HEAD"),
)


def _proxy_dict(proxy: str) -> dict | None:
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    return {"http": proxy, "https": proxy}


def normalize_proxy(proxy: str) -> str:
    """统一成 URL：裸 host:port 补 http://，用户名密码原样保留。"""
    proxy = (proxy or "").strip()
    if proxy and "://" not in proxy:
        proxy = "http://" + proxy
    return proxy


def test_proxy(proxy: str = "", timeout: float = 8.0) -> dict:
    """返回 {proxy, results: [{label, url, ok, ms, detail}], ok: bool}。"""
    import requests

    proxies = _proxy_dict(proxy)
    results = []
    for label, url, method in TARGETS:
        started = time.perf_counter()
        ok = False
        detail = ""
        try:
            kwargs = {"timeout": timeout, "allow_redirects": True,
                      "headers": {"User-Agent": "Mozilla/5.0"}}
            if proxies:
                kwargs["proxies"] = proxies
            resp = requests.request(method, url, **kwargs)
            ok = resp.status_code < 400
            detail = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 - 失败原因直接给用户看
            name = type(exc).__name__
            text = str(exc)
            if "InvalidSchema" in name:
                detail = "协议不支持（socks5 需装 PySocks）"
            elif "ProxyError" in name:
                detail = "代理连不上（可能要账号密码）"
            elif "Timeout" in name or "timed out" in text:
                detail = "超时"
            elif "Connection" in name:
                detail = "连接被重置/拒绝"
            elif "SSL" in name:
                detail = "TLS 握手失败"
            else:
                detail = name
        ms = round((time.perf_counter() - started) * 1000)
        results.append({"label": label, "url": urlsplit(url).netloc, "ok": ok, "ms": ms, "detail": detail})

    return {
        "proxy": proxies["https"] if proxies else "（直连）",
        "results": results,
        "ok": any(r["ok"] for r in results),
    }


def summarize(result: dict) -> str:
    """一行摘要：3/4 通 · 最快 180ms · 抖音超时。"""
    results = result.get("results") or []
    good = [r for r in results if r["ok"]]
    if not results:
        return "没有可测试的目标"
    fastest = min((r["ms"] for r in good), default=None)
    failed = [r["label"] for r in results if not r["ok"]]
    parts = [f"{len(good)}/{len(results)} 通"]
    if fastest is not None:
        parts.append(f"最快 {fastest}ms")
    if failed:
        parts.append("不通：" + "、".join(failed))
    return " · ".join(parts)
