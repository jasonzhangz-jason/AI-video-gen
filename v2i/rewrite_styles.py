"""文案改写的风格登记表：id → (中文名, prompt 文件名)。

新增风格只需要：加一个 prompt 文件 + 在这里登记一行。
"""

REWRITE_STYLES: dict[str, tuple[str, str]] = {
    "light": ("轻度整理", "rewrite_light"),
    "script": ("口播稿", "rewrite_script"),
    "xiaohongshu": ("小红书笔记", "rewrite_xiaohongshu"),
    "wechat": ("公众号长文", "rewrite_wechat"),
    "zhihu": ("知乎回答", "rewrite_zhihu"),
    "news": ("新闻通稿", "rewrite_news"),
    "weibo": ("微博短文案", "rewrite_weibo"),
    "qa": ("访谈问答", "rewrite_qa"),
    "sales": ("带货口播", "rewrite_sales"),
    "story": ("故事化叙述", "rewrite_story"),
}


def label(style: str) -> str:
    return REWRITE_STYLES.get(style, (style, ""))[0]


def prompt_for(style: str) -> str | None:
    entry = REWRITE_STYLES.get(style)
    return entry[1] if entry else None


def options() -> list[dict]:
    return [{"id": key, "label": value[0]} for key, value in REWRITE_STYLES.items()]
