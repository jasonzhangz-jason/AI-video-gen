"""平台登记表：给 GUI 的下拉、链接校验、以及"这个平台要注意什么"的提示。

下载能力本身来自 yt-dlp（支持 1800+ 站点），这里只做三件事：
1. 让用户明确选平台（而不是猜链接能不能跑）；
2. 用域名快速校验"选的平台"和"贴的链接"是否匹配（短链域名也收录）；
3. 标注该平台通常需要什么（cookie / 代理），避免失败时摸不着头脑。
"""
from __future__ import annotations

from urllib.parse import urlsplit

PLATFORMS: list[dict] = [
    {
        "id": "auto", "label": "自动识别", "hosts": [],
        "note": "按链接域名自动匹配平台",
    },
    {
        "id": "bilibili", "label": "哔哩哔哩 B站",
        "hosts": ["bilibili.com", "b23.tv", "bili2233.cn"],
        "needs": [], "note": "音频无需 cookie；1080P 以上需登录 cookie。弹幕不算字幕，会自动跳过",
    },
    {
        "id": "douyin", "label": "抖音",
        "hosts": ["douyin.com", "iesdouyin.com", "v.douyin.com"],
        "needs": ["cookies"], "note": "多数视频需要登录 cookie（V2I_COOKIES），短链 v.douyin.com 也可以直接贴",
    },
    {
        "id": "kuaishou", "label": "快手",
        "hosts": ["kuaishou.com", "chenzhongtech.com", "gifshow.com"],
        "needs": ["cookies"], "note": "部分内容需要 cookie；v.kuaishou.com 短链支持",
    },
    {
        "id": "xiaohongshu", "label": "小红书",
        "hosts": ["xiaohongshu.com", "xhslink.com"],
        "needs": ["cookies"], "note": "通常需要登录 cookie；xhslink.com 短链支持",
    },
    {
        "id": "weibo", "label": "微博",
        "hosts": ["weibo.com", "weibo.cn", "m.weibo.cn"],
        "needs": ["cookies"], "note": "部分视频需登录；对外分享的秒拍链接也可能可用",
    },
    {
        "id": "xigua", "label": "西瓜视频",
        "hosts": ["ixigua.com"],
        "needs": [], "note": "通常无需 cookie",
    },
    {
        "id": "youtube", "label": "YouTube",
        "hosts": ["youtube.com", "youtu.be", "m.youtube.com"],
        "needs": ["proxy"], "note": "国内需代理；遇到 bot 检测时加 cookie 或装 bgutil PO token 插件",
    },
    {
        "id": "x", "label": "X（Twitter）",
        "hosts": ["x.com", "twitter.com", "t.co"],
        "needs": ["proxy", "cookies"], "note": "国内需代理；敏感内容需要登录 cookie",
    },
    {
        "id": "tiktok", "label": "TikTok",
        "hosts": ["tiktok.com", "vm.tiktok.com"],
        "needs": ["proxy"], "note": "国内需代理；区域限制内容可能需要 cookie",
    },
    {
        "id": "instagram", "label": "Instagram",
        "hosts": ["instagram.com", "instagr.am"],
        "needs": ["proxy", "cookies"], "note": "多数内容需要登录 cookie",
    },
    {
        "id": "vimeo", "label": "Vimeo",
        "hosts": ["vimeo.com"],
        "needs": ["proxy"], "note": "国内需代理",
    },
    {
        "id": "podcast", "label": "播客 / 音频直链",
        "hosts": ["podcasts.apple.com", "xiaoyuzhoufm.com", "overcast.fm"],
        "needs": [], "note": "小宇宙、Apple Podcasts 等页面可直接解析；纯 mp3 直链也能下",
    },
]

_BY_ID = {p["id"]: p for p in PLATFORMS}


def get(platform_id: str | None) -> dict | None:
    return _BY_ID.get(platform_id or "")


def host_matches(url: str, platform: dict) -> bool:
    hosts = platform.get("hosts") or []
    if not hosts:
        return True
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in hosts)


def match(url: str) -> dict | None:
    """按域名反查平台（用于"自动识别"）。"""
    for platform in PLATFORMS:
        if platform["id"] == "auto":
            continue
        if host_matches(url, platform):
            return platform
    return None


def check(platform_id: str | None, url: str) -> tuple[bool, str]:
    """校验所选平台与链接是否匹配；短链域名已收录，所以正常不会误报。"""
    if not platform_id or platform_id == "auto":
        found = match(url)
        return True, (f"识别为 {found['label']}" if found else "未识别的站点，将交给 yt-dlp 通用解析")
    platform = get(platform_id)
    if platform is None:
        return True, "未知平台，按通用解析处理"
    if host_matches(url, platform):
        return True, f"匹配 {platform['label']}"
    found = match(url)
    hint = f"看起来是 {found['label']} 的链接" if found else "域名不在该平台清单里"
    return False, f"所选平台是 {platform['label']}，但{hint}；仍可尝试解析"


def labels() -> dict[str, str]:
    return {p["id"]: p["label"] for p in PLATFORMS}
