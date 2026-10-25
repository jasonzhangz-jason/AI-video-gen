# 视频 → 文字 → 洞察：流水线调研与方案设计

> 状态：**方案稿，未写代码**（按你的要求，本轮只做调研 + 方案）
> 日期：2026-10-24 ｜ 适用环境：Windows + CPU-only（详见第 1 节实测）

---

## 0. 结论速览（TL;DR）

1. **这条链路上每一层都有成熟开源件，没有"一个项目刚好等于你要的 CLI 流水线"**：`yt-dlp`（下载）→ `faster-whisper`（ASR）→ 任意 OpenAI 兼容大模型（提炼），三段都是事实标准，粘合层只有几百行。
2. **最接近的成品是 [BiliNote](https://github.com/JefferyHcool/BiliNote)（7.6k★，MIT）和 [AI-Video-Transcriber](https://github.com/wendy7756/AI-Video-Transcriber)（3.4k★，Apache-2.0）**，但它们都是 Web 产品，prompt / 输出格式 / 队列都绑死在产品流程里，拿来当"素材库"比当"底座"合适。
3. **推荐路线：薄壳自研（约 800–1200 行 Python），CLI 优先，模块可替换**。收益：CPU 也能跑、产物是 Markdown/JSON（可直接进 Obsidian）、prompt 与输出模版完全可控、换模型换 ASR 只改配置。
4. **两个"省一半时间"的关键设计**（都是从存量项目里学来的）：
   - **字幕优先，ASR 兜底**：有原生字幕（YouTube 人工/AI 字幕、B 站 CC）就直接取，跳过下载音频+转写，速度差 10 倍以上（AI-Video-Transcriber 的 "Subtitle-First Architecture"）。
   - **map-reduce 两层提炼**：长视频先分块出"结构化笔记"，再归并成摘要/观点/文章，避免一次性塞爆上下文，且天然支持 2 小时播客。
5. **你机器上没有 ffmpeg、没有 NVIDIA GPU、Python 是 3.14**——这三件事直接决定选型：`imageio-ffmpeg` 补 ffmpeg、`int8` 量化 CPU 推理、**建议另装 Python 3.12 跑 ASR 环境**（ctranslate2 的 wheel 覆盖不到 3.14 的概率较高）。

---

## 1. 你的环境体检（本轮实测）

| 项 | 实测结果 | 对方案的影响 |
|---|---|---|
| OS / Shell | Windows，PowerShell **5.1.26100**（不是 pwsh 7） | 网络请求别用 `Invoke-WebRequest`（TLS 报 `SEC_E_NO_CREDENTIALS`），一律走 Python |
| Python | **3.14.6**，`C:\Users\86188\AppData\Local\Python\pythoncore-3.14-64\python.exe`，pip 26.1.2 | ⚠️ 3.14 太新，`ctranslate2` / `av` / `onnxruntime` 的 wheel 未必覆盖 → 建议单独装 3.11/3.12 |
| Node | v24.10.0（另有 pnpm 可用） | 若要做本地 Web UI，前端不用另装环境 |
| yt-dlp | ✅ 已存在：`...\pythoncore-3.14-64\Scripts\yt-dlp.exe` | 下载层零成本，但建议用 Python API 而非 subprocess |
| ffmpeg | ❌ PATH 中不存在，全盘未找到 | 用 `imageio-ffmpeg`（pip 装，自带静态 ffmpeg）或 `winget install Gyan.FFmpeg` |
| GPU | ❌ 无 `nvidia-smi` → 纯 CPU | ASR 选 `small`/`medium`/`large-v3-turbo` + `int8`，长视频要有心理预期 |
| uv / conda | ❌ 都没有 | 装 `uv` 是当前最省事的 Python 版本管理器（一条命令装 3.12） |
| 网络 | Python/pip 直连 `pypi.org` ✅（HTTP 200）；`raw.githubusercontent.com` ❌ 卡死；HuggingFace 下载模型大概率慢 | 模型走 `hf-mirror.com`（`HF_ENDPOINT`）；pip 用清华/阿里镜像 |
| venv | 本沙箱下 `python -m venv` 的 `ensurepip` 失败 | 用 `uv venv`，或 `--without-pip` + `get-pip.py`，或系统 pip `--user` |

> 已顺手清掉上一轮中断留下的空 `.venv`（507KB、无 pip，不可用）。`video2insight/` 目录现在只有本文件。

---

## 2. 存量开源盘点（GitHub，star 数取自 2026-10-24 API）

### 2.1 三层"标准件"（直接用，不必造）

| 项目 | ★ | License | 定位 | 用法 |
|---|---|---|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 201k | Unlicense | 1800+ 站点下载，字幕/章节/元数据 | 作为 Python 库调用，`bestaudio` + `writeinfojson` |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 26k | MIT | CTranslate2 版 Whisper，内置 Silero VAD、词级时间戳、`BatchedInferencePipeline` | 核心 ASR；CPU 上 int8 比原版 whisper 快 4 倍左右 |
| [whisperX](https://github.com/m-bain/whisperX) | 24.8k | BSD-2 | 词级对齐 + 说话人分离（pyannote） | 只在"多人访谈/播客要区分谁在说"时引入 |
| [markitdown](https://github.com/microsoft/markitdown) | 191k | MIT | 多格式→Markdown，含 YouTube 字幕直取 | 可借它的"字幕直取"思路，不必依赖 |

### 2.2 端到端同类产品（借鉴对象，不是底座）

| 项目 | ★ | License | 它做了什么 | 对我们的价值 / 代价 |
|---|---|---|---|---|
| [BiliNote](https://github.com/JefferyHcool/BiliNote) | 7.6k | MIT | 视频→AI 笔记，FastAPI + React 19，多 LLM（OpenAI/DeepSeek/Qwen），Docker 一键部署，支持 B 站/YouTube，带截图笔记 | **交互与输出形态的最佳参考**；但前后端耦合、要跑服务、CPU 下 ASR 慢，当 CLI 底座不合适 |
| [AI-Video-Transcriber](https://github.com/wendy7756/AI-Video-Transcriber) | 3.4k | Apache-2.0 | 30+ 平台，**字幕优先 + Whisper 兜底**，转写与下载并行，本地文件也支持，带 Web UI | **架构参考价值最高**（尤其"字幕优先"与并行下载）；但目标是"归档"，不是"提炼" |
| [VideoLingo](https://github.com/Huanshere/VideoLingo) | 18.9k | Apache-2.0 | Netflix 级字幕切割/翻译/对齐/配音，全流程自动化 | 要做"翻译+配音"时的参考；它的**字幕断句算法**值得抄思路（按时长/语义切，不死按字数） |
| [KrillinAI](https://github.com/krillinai/KrillinAI) | 13.9k | Apache-2.0 | 视频翻译配音 + 面向创作者的内容工作台（TS 全栈） | 工程化/发布形态参考（Docker、任务队列、多平台适配） |
| [VidBee](https://github.com/nexmoe/VidBee) | 11k | MIT | 桌面版多平台下载器（YouTube/TikTok/X…） | 下载 UX 与平台适配参考；不含提炼 |
| [BiliSum](https://github.com/lycohana/BiliSum) | 646 | MIT | B 站/YouTube 视频摘要 + 知识库 | 中文场景（B 站）参考 |
| [LocalVid-Summarizer](https://github.com/sunglasses233/LocalVid-Summarizer) | 31 | MIT | 全本地 Whisper + 本地 LLM，隐私优先 | "离线/不联网"模式参考 |
| [bibigpt-skill](https://github.com/JimmyLv/bibigpt-skill) | 131 | — | 给 Claude Code / Codex 用的视频摘要 Agent Skill | 若你想把流水线做成 Agent 工具，接口设计可参考 |
| [pyvideotrans](https://github.com/jianchang512/pyvideotrans) | 19.5k | **GPL-3.0** | 视频翻译/配音 | ⚠️ GPL 有传染性，**只可读思路，不可抄代码** |
| [NarratoAI](https://github.com/linyqh/NarratoAI) | 11.5k | MIT | 影视解说：拆条 + 脚本生成 | "长视频拆成观点/脚本"的 prompt 思路参考 |

### 2.3 调研结论

- **不缺轮子，缺的是"粘合 + 提炼"这一层**：现有项目要么只做下载（VidBee）、只做转写（faster-whisper）、只做翻译（VideoLingo），要么把提炼做成了 Web 产品（BiliNote / AI-Video-Transcriber）。
- **"摘要/观点/文章"三件套几乎没有项目认真做**：大多数只出一份摘要或一份笔记；把"论点—论据—时间戳—可信度"结构化，并支持"改写成文章"，是这块的差异点。
- **许可证友好**：MIT/Apache-2.0 占绝大多数，可放心读代码、抄架构；唯独 pyvideotrans 是 GPL，绕开。

---

## 3. 三条路线对比

| | A. 直接用现成 | **B. 薄壳自研（推荐）** | C. Fork BiliNote 做产品 |
|---|---|---|---|
| 上手时间 | 1–2 小时（Docker） | 2–3 天出可用版 | 2–4 周 |
| 自定义 prompt / 输出 | 弱（改配置/改源码） | **完全可控** | 中（要在其 pipeline 里改） |
| CPU 可用性 | 依赖其默认模型，常偏重 | 可针对 CPU 调小模型 + 量化 | 同 A |
| 批处理 / 断点续跑 | 看项目 | 自己掌控（manifest + 缓存） | 有队列，但绑定其任务模型 |
| 维护成本 | 低（跟上游） | 中（自己维护 ~1k 行） | 高（跟随上游 + 自己的改动） |
| 适合谁 | 想立刻用、不挑格式 | **想要稳定产出自己的文档** | 要给别人用、要 Web UI |

**推荐 B**，理由：你的真实需求是"把公开视频变成能用的文字资产"，而不是"再做一个视频总结网站"；B 路线能把复杂度压在 1k 行内，每一层都能单独替换和验证。

---

## 4. 推荐架构（B 路线）

### 4.1 数据流

```
URL / 本地文件 / 播放列表
        │
   ┌────▼─────────────┐
   │ 1 fetch          │  yt-dlp：取元数据 + 字幕；无字幕才下 bestaudio
   │ (subtitle-first) │  产物: meta.json / source.m4a / subs.zh.srt
   └────┬─────────────┘
        │  有字幕 ────────────────┐
        ▼                         │
   ┌──────────────────┐           │
   │ 2 transcribe     │  faster-whisper（VAD + int8）
   │ (ASR fallback)   │  产物: transcript.json（段级时间戳）
   └────┬─────────────┘           │
        ▼                         ▼
   ┌───────────────────────────────┐
   │ 3 normalize                   │  清洗、断句、按语义/时长分块，
   │ (chunk & align)               │  每块携带 [start,end] 时间锚点
   └────┬──────────────────────────┘
        ▼
   ┌───────────────────────────────┐
   │ 4 distill (map-reduce)        │  map: 每块 → 结构化笔记(JSON)
   │                               │  reduce: 汇总 → 摘要/观点/文章
   └────┬──────────────────────────┘
        ▼
   ┌───────────────────────────────┐
   │ 5 render                      │  Markdown(+frontmatter) / JSON / SRT
   └───────────────────────────────┘
```

### 4.2 目录与产物（幂等、可续跑）

```
video2insight/
  v2i/
    cli.py            # 子命令: run / fetch / transcribe / distill / batch / doctor
    config.py         # env + toml，模型与 provider 配置
    fetch.py          # yt-dlp 封装（含字幕优先、cookies、限速、重试）
    asr.py            # faster-whisper 封装（设备/量化自适应、VAD、语言检测）
    normalize.py      # 断句、分块、时间轴对齐、中英混排清洗
    llm.py            # OpenAI 兼容客户端（重试/超时/JSON 修复/降级模型）
    prompts/          # map_notes.md / reduce_summary.md / reduce_points.md / reduce_article.md
    distill.py        # map-reduce 编排、引用校验
    render.py         # Markdown/SRT/JSON 渲染
  runs/<video_id>/
    meta.json         # 标题、作者、时长、链接、章节、下载参数
    source.m4a        # 仅当没字幕时存在
    subs.zh.srt       # 原始字幕（如有）
    transcript.json   # 统一后的段级文本（唯一真源）
    transcript.srt / transcript.md
    notes/chunk-001.json ...   # map 阶段产物（可单独重跑）
    out/summary.md | points.md | article.md | digest.json
    manifest.json     # 每阶段耗时、模型、token 数、成本、状态
```

**关键：`transcript.json` 是唯一真源。** 一旦转写完成，后面所有重试（换 prompt、换模型、换输出形态）都不再碰音频，这是省时间的关键。

### 4.3 CLI 设计（草案）

```bash
v2i run <url> [--mode summary|points|article|all] [--out DIR]
             [--model deepseek-chat] [--lang zh] [--force] [--no-asr]
v2i batch urls.txt --mode summary --concurrency 2
v2i doctor                      # 环境自检：ffmpeg/模型/网络/API key
v2i transcribe <file> --model medium --compute-type int8
```

约定：每阶段独立可跑（`fetch`/`transcribe`/`distill`），`run` 只是串起来；`--force` 之前一律命中缓存。

---

## 5. 关键技术选择

### 5.1 下载层（yt-dlp）

| 决策点 | 建议 | 理由 |
|---|---|---|
| 接口 | Python API（`YoutubeDL`），不是 subprocess | 能拿结构化 info，能挂 progress hook |
| 格式 | `bestaudio/best`，`--no-playlist` 默认 | 只要音频，省带宽；m4a 可直接被 PyAV 解码，**不需要 ffmpeg** |
| 字幕 | 先探 `subtitles`（人工）→ 再 `automatic_captions`（自动） | 自动字幕质量一般但"不花钱不花时间"，作为兜底仍划算 |
| 风控 | `cookiefile` 可配；YouTube 遇 bot 检测挂 [bgutil-ytdlp-pot-provider](https://github.com/Positive-LLC/bgutil-ytdlp-pot-provider) | 2025 年后 YouTube 无 PO token 常 403；B 站 1080P+ 需登录 cookie |
| 重试 | `retries=5`、`fragment_retries`、`socket_timeout=30`、限速 `ratelimit` | 国内直连 YouTube 抖动大 |
| ffmpeg | 用 `imageio-ffmpeg` 的静态二进制路径喂给 `ffmpeg_location` | 本机没装 ffmpeg，而它只在"合并音视频/转码"时才需要 |

### 5.2 转写层（faster-whisper）

| 决策点 | 建议 | 说明 |
|---|---|---|
| 设备/精度 | 无 GPU：`device=cpu`, `compute_type=int8`；有 GPU：`cuda` + `float16` | 本机 CPU-only，别用 `float32` 浪费内存 |
| 模型 | CPU 默认 `small`；要质量用 `medium`；有 GPU 用 `large-v3` / `turbo` | `turbo`（large-v3-turbo）在 CTranslate2 里可用，速度/质量折中最好 |
| 批量 | `BatchedInferencePipeline(batch_size=8)` | 官方 README 基准：13 分钟音频，GPU fp16 约 17s，批处理 int8 约 16s |
| VAD | 必开 `vad_filter=True`，`min_silence_duration_ms=500` | 去掉静音段，长播客能省 20–40% 时间，还能避免"重复幻觉" |
| 时间戳 | 默认段级；`word_timestamps` 仅在需要做"字幕级对齐/关键词定位"时开 | 词级时间戳会让 CPU 慢 20–30% |
| 语言 | `language=None` 自动检测；中文场景加 `initial_prompt` 塞专有名词表 | Whisper 对中文人名/术语易错，prompt 纠错性价比最高 |
| 中文替代 | 可选接 **SenseVoice / FunASR Paraformer**（中文更准）或 **必剪 Bcut 接口**（免费、快、无需算力） | BiliNote 这类中文项目常用 Bcut 兜底；但属外部服务，作为可选插件 |

> 现实预期（纯 CPU，`small`+int8）：1 小时视频约需 8–20 分钟转写；`medium` 约 25–50 分钟。这就是为什么"字幕优先"值钱。

### 5.3 分块与对齐

- 按**句末标点 + 停顿**切，单块目标 **1200–2000 汉字**（约 1.5k–2.5k token），重叠 1 句防止观点被切断。
- 每块保留 `[start, end]` 秒级锚点，后续所有引用都从这里取时间戳（**引用可核查**是这个产品的信任基础）。
- 中英混排：不要按字符数硬切，按标点 + 词边界；数字/专有名词不拆。

### 5.4 提炼层（LLM）

**两段式（map-reduce），不要一把梭：**

1. **map**：每块 → 严格 JSON
   ```json
   {
     "chunk_id": 3, "start": 412.0, "end": 688.5,
     "gist": "本段在讲什么（≤40字）",
     "points": [{"claim": "论点", "evidence": "论据/例子", "ts": 431.2, "type": "claim|fact|opinion|prediction"}],
     "quotes": [{"text": "原话", "ts": 455.0}],
     "entities": ["人名/机构/产品"],
     "open_questions": ["未展开的问题"]
   }
   ```
2. **reduce**：只喂 map 结果（体积是原文的 1/8–1/10），按 `--mode` 出不同产物：
   - `summary`：一句话题眼 / 300 字摘要 / 5–8 条要点 / 时间轴 / 3 条金句 / 行动项
   - `points`：观点清单（论点 → 论据 → 时间戳 → 类型 → 我的质疑/反方），按"主线/支线"分组
   - `article`：800–1500 字成文（标题 + 导语 + 3–5 个小标题 + 结尾），每个事实后带 `[mm:ss]`
   - `all`：三份都出，共享一次 map

**工程约束：**
- 温度 0.2–0.4；`response_format=json_object`（或 function calling），解析失败走"截取 + 修复"再重试一次。
- 每块重试 2 次、指数退避；连续失败降级到备用模型，仍失败则整条任务标 `partial` 而**不丢已有产物**。
- **反幻觉三道闸**：① prompt 里禁止引入原文没有的事实；② reduce 阶段要求"每条要点必须能在 map 结果里找到 ts"；③ 渲染前抽查：引用的 ts 必须落在某段 transcript 的区间内，否则丢弃该引用。
- 长视频上限：先按"总 token 预算"反推块大小；2 小时以上播客建议先出"分段摘要 + 目录"，再按需深读某段（避免一次性全网撒）。

### 5.5 模型与 provider

统一走 **OpenAI 兼容协议**（`base_url` + `api_key` + `model`），一份代码吃下：DeepSeek、Moonshot/Kimi、Qwen(DashScope 兼容模式)、智谱、SiliconFlow、OpenAI、以及本机 **Ollama**（`http://127.0.0.1:11434/v1`）。

| 场景 | 推荐 | 备注 |
|---|---|---|
| 默认（性价比） | `deepseek-chat` | 中文长文提炼稳、便宜；`deepseek-reasoner` 只在"观点抽取"这种硬任务上开 |
| 最高质量 | `gpt-5.x` / `claude-*`（若已有额度） | 观点类任务提升明显，摘要类提升有限 |
| 完全离线 | Ollama + `qwen3:8b/14b` | 8B 够做摘要，观点抽取建议 14B+ |
| Embedding（可选，做知识库检索） | `bge-m3`（本地）或 `text-embedding-3-small` | 第二阶段再加，别在 MVP 里引入 |

---

## 6. 成本与性能估算（量级，按本机 CPU 假设）

| 视频时长 | 有字幕（仅 LLM） | 无字幕（CPU small） | 无字幕（GPU large-v3） |
|---|---|---|---|
| 10 分钟 | ~20 秒 | 2–4 分钟 | ~20 秒 |
| 1 小时 | ~1 分钟 | 10–20 分钟 | 2–3 分钟 |
| 3 小时播客 | 2–3 分钟 | 40–60 分钟 | 6–8 分钟 |

LLM 花费（1 小时中文视频，约 1 万字 ≈ 1.2 万 token）：
map 阶段约 2–3 万输入 token + reduce 约 1 万输入 / 3–5k 输出 → **DeepSeek 约 ¥0.05–0.15 / 条**，贵价模型约 ¥1–3 / 条。
（价格为量级参考，以官网现价为准。）

**结论：本地 ASR 是免费的但费时间；LLM 是花小钱买质量。二者解耦后，"只重跑提炼"几乎是零成本，这是幂等设计最大的红利。**

---

## 7. 里程碑（含验收标准）

| 阶段 | 内容 | 工作量 | 验收 |
|---|---|---|---|
| **M0 环境** | 装 uv → Python 3.12 → venv → `yt-dlp/faster-whisper/requests/imageio-ffmpeg`；`doctor` 自检 | 0.5 天 | `v2i doctor` 全绿；模型能下载（走 hf-mirror） |
| **M1 取词** | fetch（字幕优先 + 音频兜底）+ normalize + transcript.json/srt | 0.5 天 | 3 个 URL（YT 有字幕 / B 站无字幕 / 播客 RSS）出正确 transcript |
| **M2 ASR** | faster-whisper 接入、设备自适应、VAD、语言检测、断点续跑 | 1 天 | 1 小时视频 CPU 端到端跑通，段级时间戳正确 |
| **M3 提炼** | llm 客户端 + map/reduce + 三种模版 + 引用校验 | 1 天 | 同一 transcript 出 summary/points/article 三份，引用 ts 全部可回溯 |
| **M4 工程化** | batch、并发、缓存 manifest、失败重试、成本统计 | 1 天 | 10 条 URL 批量跑，断网/429 不炸，重跑只补失败项 |

**MVP 边界（明确不做）**：不做视频画面理解/截图、不做配音、不做多语翻译、不做 Web UI、不做向量库。这些都是二期，且都能在现有接口上加。

---

## 8. 风险与对策

| 风险 | 概率 | 对策 |
|---|---|---|
| YouTube bot 检测 / 403 | 高 | PO token 插件 + cookies 文件；失败自动降级到"仅取字幕" |
| B 站需登录才能高清晰度 | 高 | 引导用户导出 cookies.txt；或用 B 站 CC 字幕（常免费） |
| Python 3.14 无 ctranslate2 wheel | 中高 | 独立 3.12 环境（uv），ASR 与主程序解耦 |
| CPU 转写太慢 | 高 | 默认 `small`；提示"有字幕就别 ASR"；支持云端 ASR 作为可选后端 |
| 模型下载慢/失败 | 高 | `HF_ENDPOINT=https://hf-mirror.com`；模型缓存在 `~/.cache/huggingface` 可复用 |
| LLM 幻觉/编造 | 中 | 三道闸（5.4）+ 引用校验 + 只允许"改写不复述原文事实" |
| 长音频 OOM | 低 | VAD 切片 + 流式 segment 迭代（faster-whisper 本身就是生成器） |
| 版权/合规 | — | 仅处理公开可访问内容；产物标注来源与链接；不做二次分发/公开镜像 |
| 字幕本身质量差 | 中 | 自动字幕做"清洗 + 术语表纠错"；关键结论抽样回听（可选，人工） |

---

## 9. 已定稿的选型（你已拍板）

| 决策 | 定稿 | 落地含义 |
|---|---|---|
| 形态 | **纯 CLI** | 不做 Web UI/队列；`v2i run/batch/doctor` 三个入口先跑通 |
| ASR | **本机 CPU + faster-whisper int8** | 默认 `small`，`--model medium` 可升；VAD 必开；字幕优先省掉大部分转写 |
| LLM | **DeepSeek**（OpenAI 兼容） | `base_url=https://api.deepseek.com/v1`，`deepseek-chat` 默认；key 走 `.env` 的 `DEEPSEEK_API_KEY` |
| 产物 | Markdown + JSON | `out/summary.md`、`out/points.md`、`out/article.md`、`out/digest.json` |

### 实施顺序（确认后按此开工）

1. **M0 环境**：`uv python install 3.12` → 建 venv → 装 `yt-dlp / faster-whisper / requests / imageio-ffmpeg` → `v2i doctor` 自检（ffmpeg、模型缓存、hf-mirror、DeepSeek key 连通性）。
2. **M1 取词**：`fetch.py`（字幕优先 + `bestaudio` 兜底 + cookies 支持）→ `normalize.py` → `transcript.json` + `transcript.srt`。
3. **M2 转写**：`asr.py` 接 faster-whisper（CPU int8 / VAD / 语言检测 / 可选 batched）。
4. **M3 提炼**：`llm.py`（重试、JSON 修复、降级）+ `map_notes` / `reduce_*` 四份 prompt + 引用时间戳校验。
5. **M4 工程化**：`batch`、`manifest.json` 成本统计、失败重跑只补缺口。

**MVP 验收线**：给 3 个 URL（YouTube 有字幕 / B 站无字幕 / 小宇宙播客），一条命令产出三份 Markdown，所有观点带 `[mm:ss]` 且能回溯到 transcript。

**需要你提供**：`DEEPSEEK_API_KEY`（放 `.env`，不进版本库）；B 站高清晰度需 `cookies.txt`（可选）。

---

## 10. 实现状态（2026-10-24 晚，M0–M4 已落地）

代码在 `video2insight/`，`README.md` 有安装与用法，本文件保留为设计底稿。

### 10.1 计划 vs 实际

| 计划里的判断 | 实际结果 |
|---|---|
| 可能要单独装 Python 3.12 | **不需要**：ctranslate2 4.8.3 / av / onnxruntime / tokenizers 都已有 cp314 wheel，直接用系统 Python 3.14.6 |
| venv 隔离 | **venv 在本沙箱建不起来**（ensurepip 被拒），改为 `pip --target vendor` + `PYTHONPATH`，`v2i.cmd` 负责设好 |
| 只要音频就不需要 ffmpeg | 成立：m4a 由 PyAV 直接解码；`imageio-ffmpeg` 提供静态 ffmpeg 仅作兜底 |
| CPU `small` 1 小时约 10–20 分钟 | 实测 **3.6× 实时**（3 分 32 秒音频转写 59s），比预期快 |
| 字幕优先 | 成立；但 B 站把弹幕 `danmaku(xml)` 也挂在 subtitles 下，已排除，避免把弹幕当字幕 |

### 10.2 踩到的四个坑（都已修进代码）

1. **av 19 不兼容**：av 19 删了 `metadata_errors`，faster-whisper 1.2.1 直接 `TypeError` → 锁 `av==16.1.0`。
2. **HF 镜像 + Xet**：`hf-xet` 默认开启，镜像站不支持 CAS 协议，报 401 → 设 `HF_HUB_DISABLE_XET=1`。
3. **清华源 pip 解析不到包**：同一台机器上 `pip -i tuna` 对任何包都报 `from versions: none`，换**阿里云源**正常；而 `files.pythonhosted.org` 被限速到 30KB/s。
4. **Silero VAD 把演唱/纯音乐判成无人声** → 转写为空；已加"VAD 无结果就关 VAD 重试"。

### 10.3 已验证 / 未验证

- ✅ 真实视频端到端：B 站 `BV1GJ411x7h7`（3 分 32 秒）→ 音频下载 5.4MB → 转写 64 段 → 分块（尾块过小时自动合并）→ 三份 Markdown，全程 62s。
- ✅ 离线自检 `tools/selftest.py`：VTT 滚动去重、SRT、分块、JSON 抽取（围栏/尾逗号/嵌套花括号）、引用校验。
- ✅ 字幕路径渲染（`transcript: 字幕直取（zh-Hans）`）、`distill --force` 缓存命中与重跑。
- ⚠️ **提炼用的是 mock LLM**（`tools/mock_llm.py`）：链路、字段、渲染、token 统计都通了，但 prompt 质量要等你给 `DEEPSEEK_API_KEY` 后跑真实视频才算验收。
- ⚠️ YouTube 在本网络不可达（超时），cookies / PO token 逻辑未实测；B 站无 cookies 时只能拿到"无字幕 → 走 ASR"这条路径。

### 10.4 下一步（按价值排序）

1. 加 `DEEPSEEK_API_KEY`，用 2–3 条真实视频**调 prompt**（这是决定产出质量的唯一环节）。
2. 用真实字幕视频验一次字幕直取（YouTube 需网络，B 站需找有 CC 的视频）。
3. `points.md` 的"论点承重排序"与"盲区"在真实内容上多半要再拧两轮 prompt。
4. 二期再考虑：截图笔记、时序对齐的引用回链、Obsidian vault 写入、本地 Web UI。

---

## 附：不用写代码就能验证的三条路（可选）

```powershell
# 0) Python 3.12 环境（推荐）
winget install --id astral-sh.uv -e
uv python install 3.12
uv venv D:\code\reasonix-ds-workspace\video2insight\.venv --python 3.12
uv pip install --python D:\code\reasonix-ds-workspace\video2insight\.venv\Scripts\python.exe yt-dlp faster-whisper imageio-ffmpeg

# 1) 只验"取词"：先看有没有字幕
yt-dlp --list-subs "<URL>"

# 2) 只验"转写"（有 ffmpeg 的话可省掉 audio-only 参数）
yt-dlp -f bestaudio -o "demo.%(ext)s" "<URL>"
# 然后 python -c "from faster_whisper import WhisperModel; ..." 跑一段 30s 音频看质量

# 3) 只验"提炼"：把 transcript 前 3000 字丢给任意大模型，看输出模版是否合口味
```

> 建议顺序：**先用手工的三段验证输出模版（尤其是 map 的 JSON 字段和 reduce 的排版），模版定下来再写代码**——prompt 才是这条流水线的护城河，代码只是管道。

### 10.5 Web GUI（2026-10-24 追加）

- `v2i web` 起一个本地单页：标准库 `http.server` + 原生 JS，无 Flask / npm / CDN 依赖。
- 复用 CLI 的 `_run_one`，任务串行跑（单 worker 队列），日志用 `redirect_stdout` 捕获并推断阶段/进度。
- API：`/api/status`、`/api/runs`、`/api/job`、`/api/run`、`/api/file`、`/api/start`。
- 支持刷新重连（`/api/status` 暴露 `active_job`）与 `#run=<目录>` 直达；产物区自带极简 Markdown 渲染（标题/列表/引用/表格/时间码高亮）。
- 截图见 `docs/gui-*.png`；冒烟验证：经 GUI 起任务 → 队列 → 转写 → 提炼 → 三份产物，全流程 42.8s（模型已缓存）。

### 10.6 一键启动 + 本地文件上传（2026-10-24 追加）

- `start.cmd`：双击即用——缺依赖自动装（阿里云源）、缺 `.env` 自动建、起服务后自动开浏览器；`v2i.cmd` 无参数双击等同于 `start.cmd`。
- GUI 支持把本地音频/视频拖进来：前端 `fetch` 原始字节上传（标准库没有 multipart 解析器，索性不用），`/api/upload` 流式落盘到 `runs/_uploads/`，之后走与 CLI `transcribe` 完全相同的 `_run_local`。
- 默认值写死为"字幕优先 + small"，界面上直接标明：`默认：字幕优先 · small · int8`。
- 没配 key 时的行为修正：转写结果保留、只跳过提炼并打印原因（原先会让整个任务失败，白跑一遍 ASR）。
- 顺手修掉一个 Windows 坑：`ThreadingHTTPServer` 默认 `allow_reuse_address=True`，在 Windows 上会**重复绑定同一端口**，两个实例抢连接；已改为先探测端口 + 禁止复用。

### 10.7 文案提取 + 文件浏览（2026-10-24 追加）

- 新增第四种产物 `copy`：`out/copy.md`（带 frontmatter 的成稿）+ `out/copy.txt`（纯文本，直接粘贴）。
- 两种档位：有 key 时让模型按 `prompts/reduce_copy.md` 做"去口水词 + 段落化"的润色（严格禁止改事实、禁止加编号）；没 key 时退化为确定性整理（`to_paragraphs`：去句首口头语、按 ~260 字分段），**文案永远有**。
- 长稿（>12000 字）自动按 9000 字分片润色后保序拼接，不截断丢内容。
- GUI 结果区新增「所有文件」页签：`/api/files` 列目录（跳过 `notes/chunk-*.json` 只给计数），文本可预览、任意文件可 `/api/download` 下载（带 `Content-Disposition`，流式 256KB/块）。
- 文本页统一加「复制全文」按钮（`navigator.clipboard`，localhost 属安全上下文）。
- 顺手修了一个真 bug：`showFile(file)`（文件选择器）与 `showFile(dir, name)`（产物渲染）**函数名撞车**，后者被覆盖导致"点历史结果一片空白"；已改名 `selectFile`。

### 10.8 文件内嵌预览（2026-10-24 追加）

- `/api/preview`：流式返回 + **Range 支持**（206/Content-Range），截图里音频能拖进度条就是靠它；Content-Type 按后缀映射（audio/mp4、video/mp4、image/png…）。
- 前端改为弹层预览（`preview-overlay`）：文本走 Markdown/JSON/SRT 渲染，图片 `<img>`，音视频用原生 `<audio>/<video>`，二进制给下载引导；Esc 或点遮罩关闭。
- 文件行按类型打标签（TXT/IMG/AUDIO/VIDEO/BIN/DIR）并整行可点，可预览的行才有 pointer；顶部加一行操作提示。
- 顺手清掉一个失效按钮：`预览` 按钮的绑定在改用整行点击后成了死链，已删除。

### 10.9 文案润色强度三档（2026-10-25 追加）

- `cfg.copy_style`：`raw` / `light` / `script`，来源优先级 CLI `--copy-style` > GUI 下拉 > `.env` 的 `V2I_COPY_STYLE` > 默认 `light`。
- `raw` 完全不调模型（纯 `to_paragraphs` 确定性整理），`light` 用 `reduce_copy.md`，`script` 用 `reduce_copy_script.md`（口播稿：短句、钩子、允许重排但不改事实）。
- 缓存按风格区分：`copy.json` 里带 `style` 字段，风格不匹配就重跑，避免切了档位还命中旧结果。
- 三档共用同一套渲染，`copy.md` 的 frontmatter 与首行会标出当前档位与是否润色（`原样 / 轻度整理 / 口播稿` × `已润色 / 未润色`）。
- 没 key 时 `light`/`script` 会自动退回 `raw` 并在日志与文档里说明，不会静默假装润色过。

### 10.10 多平台支持（2026-10-25 追加）

- 新增 `v2i/platforms.py`：13 个平台登记（id / 名称 / 域名含短链 / 需要 cookie 还是代理 / 一句话说明），提供 `match()` 反查与 `check()` 校验。
- GUI：平台下拉（默认自动识别，贴链接时按域名提示）+「先探测」按钮（不下载，直接看标题/时长/字幕/格式）。
- 新增 `/api/platforms`、`/api/probe`；`probe` 也做成了 CLI 子命令（`v2i probe`）。
- 新增 `--cookies-from-browser`（chrome/edge/firefox）与 GUI 的 cookies/代理输入，任务级覆盖 `.env`，不必改配置重启。
- `fetch.friendly_error()`：把 yt-dlp 的英文报错翻译成"下一步做什么"（bot 校验 / 新鲜 cookie / DPAPI 解密失败 / 不支持的 URL 形式 / 403 / 超时）。
- 实测结论：B 站与音频直链端到端可用；YouTube 可达但触发 bot 校验；抖音要求 fresh cookies 且只认 `/video/<id>`；其余平台受 cookie/代理限制未实测通过——已在 README 写成矩阵，不夸大。
- 顺手修掉 ASR 的一个日志噪音：关闭 VAD 重试时会把同一句提示打印两次。

### 10.11 登录态 cookies 上传（2026-10-25 追加）

- 新增 `POST /api/cookies`（原始字节）、`GET /api/cookies`（状态）、`POST /api/cookies/clear`；文件固定存 `项目/.cookies/cookies.txt`。
- `Config.from_env()` 增加自动发现：`V2I_COOKIES` 优先，否则 `.cookies/cookies.txt` 存在即生效——所以 **GUI 上传后 CLI 也立即用得上**，不需要改 `.env` 或重启。
- 上传做格式校验（Netscape 头或制表符 7 列），JSON 导出直接 400 并说明原因；旧文件自动备份为 `.bak`。
- 任务日志会打印 `使用 cookies：cookies.txt（N B）`，出问题时能立刻确认到底带没带上。
- 实测：上传 → 状态显示「已上传 · N 条 · 时间」→ `v2i fetch` 日志出现 `使用 cookies` → 清除后恢复「未上传」；`Config.from_env().cookies` 指向 `.cookies/cookies.txt`。
- 注意：yt-dlp 会回写站点下发的新 cookie，文件会随使用变大，属预期行为。

### 10.12 探测失败可点击的操作（2026-10-25 追加）

- `fetch.error_action()` 把报错归类成 `cookies_file` / `close_browser` / `proxy` / `url`，`/api/probe` 一并返回 `action`。
- 前端按 action 渲染小按钮：`上传 cookies` / `改用 cookies.txt` / `去填代理` / `支持哪种链接？`；点击后展开高级选项、滚动并高亮对应控件（`cookie-row.flash`），cookies 类直接唤起文件选择。
- 分类要同时认中英两种措辞——因为 `friendly_error()` 会先把英文翻成中文，早期版本只看英文关键字导致 action 丢失（实测抖音 `/shipin/`、X 都漏判，已修）。
- 实测：YouTube/X → `proxy`；抖音 `/video/` → `cookies_file`；抖音 `/shipin/` → `url`；B 站 → `ok`，按钮渲染与点击行为（展开+高亮）均验证通过。

### 10.13 长链接溢出修复（2026-10-25 追加）

- 现象：逐字稿页整页被撑宽（实测 `#doc` scrollWidth 3638px vs clientWidth 824px，页面横向滚动）。
- 两处原因叠加：① `transcript.md` 的"来源"直接写了 B 站分享链接，带 `vd_source`/`trackid` 等参数长达 800+ 字符，且是一个不可断行的 token；② `.doc`/页脚/任务条都没有 `overflow-wrap`，长 token 直接顶出容器。
- 修法：
  - `util.display_url()` 统一剥掉 query/fragment（render 与 normalize 共用），逐字稿来源变成 `https://www.bilibili.com/video/BV.../`；
  - CSS 对 `.doc`、`.footer`、`.job-meta span` 加 `overflow-wrap: anywhere` / `word-break: break-all`；
  - `v2i render` 增加"重写 transcript.md"，老产物不用重跑 ASR 也能修好。
- 验证：1400px 下逐个点过 11 个页签，全部 `docScroll == docClient`；缩到 584px 视口时 `bodyScroll 569 < window 584`，无横向滚动。

### 10.14 任务进度条里的 URL 展示（2026-10-25 追加）

- 现象：任务卡里的链接是完整分享地址（800+ 字符），在窄窗口下要么换行成 5 行、要么把卡片撑宽。
- 修法：
  - 前端 `shortUrl()` 只渲染 `origin + pathname`，完整地址放进 `title` 悬停可见；
  - `.job-meta .job-url` 单独占一行（`flex: 1 1 100%`）+ `overflow:hidden / text-overflow:ellipsis / nowrap`，超长就省略号，永不变形；
  - 本地文件任务同样走这条样式（显示文件名）；
  - 后端 `fetch` 的日志也改成打 `display_url(url)`，日志里不再出现 800 字符的链接。
- 验证：真实跑一条带完整跟踪参数的 B 站任务，卡片内 URL 渲染为 `https://www.bilibili.com/video/BV1ih8q6rEMP/`；1400px 下 `bodyScroll 1369 < 1384`，模拟 480px 视口 `bodyScroll 465 < 480`，`text-overflow: ellipsis` 生效。

### 10.15 左侧菜单 + 文案改写页（2026-10-25 追加）

- 界面拆成两页：`新建任务`（原页面）与 `文案改写`；左侧固定导航（`<nav class="sidenav">`），窄屏自动变成顶部横排。
- 新接口 `POST /api/rewrite{text,style}`：`light` 用 `prompts/rewrite_light.md`，`script` 用 `prompts/rewrite_script.md`；输出是纯文本（不走 JSON），前端按空行切段渲染。
- 长文按 6000 字切片、逐段改写后保序拼接；返回 `in_chars/chars/tokens/model`，界面直接显示"228 字 → 280 字 · 200 tokens"。
- `文案改写`页支持「从任务导入」：读该任务的 `transcript.md`，剥掉标题、元信息行和 `[mm:ss]` 前缀后填入输入框。
- 深链 `#page=rewrite` 可直达；缺 key 时接口返回明确错误，页面用红字提示而不是弹窗。
- 验证：临时起 mock LLM 实例（8796 端口）跑通整条 UI 流程（输入 228 字 → 输出 6 段 280 字 → 复制可用），随后关掉临时实例，主实例（8765）保持真实配置。

### 10.16 启动后配置 key + 配置热加载（2026-10-25 追加）

- 新增 `v2i/settings.py`：运行时设置存 `.runtime/settings.json`，`apply()` 每次把白名单键刷进 `os.environ`（并撤销已删除的键）。
- `Config.from_env()` 开头调用 `settings.apply()`，所以**改完设置、下一条任务立即用新配置**，不用重启。
- 修掉一个潜伏 bug：`load_dotenv` 原来会把**空值**也写进 `os.environ`，导致之后往 `.env` 补 key 永远被旧的空串挡住；现在跳过空值。
- 修掉第二个 bug：`resolve_llm()` 只认 provider 自带的变量名（`DEEPSEEK_API_KEY`），完全忽略设置页写的 `V2I_API_KEY`；现在 `V2I_API_KEY` 优先。
- 新增接口：`GET /api/settings`（掩码展示 + 当前生效值）、`POST /api/settings`（白名单 patch，空串=删除）、`POST /api/settings/test`（真发一次 `pong`）。
- GUI 新增「设置」页：provider/模型/BaseURL/Key/代理 + 保存 + 测试连接 + 清除 key；状态点旁增加「去设置」直达。
- 实测热加载：无 key → `ready=False`；POST 一个测试 key → **未重启** `ready=True`、`api_key` 显示 `sk-t…1234`；改模型 → `status.model` 立即变成 `deepseek-reasoner`；`test` 接口返回 401（证明确实拿新 key 发了请求）；清除后回到 `False`。

### 10.17 菜单升级为【系统配置】（2026-10-25 追加）

- 左菜单第三项由「设置」改名【系统配置】，页面从"只配 LLM"扩成三组：大模型 / 语音转写 / 网络与其他。
- 可热加载的键扩到 13 个：`V2I_API_KEY`、`V2I_PROVIDER`、`V2I_MODEL`、`V2I_BASE_URL`、`V2I_PROXY`、`V2I_COOKIES`、`V2I_COOKIES_FROM_BROWSER`、`V2I_ASR_MODEL`、`V2I_ASR_DEVICE`、`V2I_ASR_COMPUTE`、`V2I_LANGUAGE`、`V2I_COPY_STYLE`、`V2I_CHUNK_CHARS`（白名单，防止变成任意环境变量注入）。
- `GET /api/settings` 的 `effective` 扩展为：LLM 四项 + `asr{model,device_setting,device,compute_setting,compute,language}` + `chunk_chars/copy_style/proxy/project/runs_dir`；「当前生效」区据此渲染，显示的是**解析后的真实值**而不是原始配置。
- 保存语义：空字符串=删除该键并回退默认，所以"跟随默认"选项可用。
- 实测热加载：一次 POST 同时改 key/model/asr/language/chunk/copy_style → 未重启 `/api/status` 立刻显示 `asr.model=base, device=cpu, compute=int8`，`effective.chunk_chars=900`、`copy_style=script`、`ready=True`；清 key 后 `ready=False`。


### 10.18 文案改写后的差异对比（2026-10-25 追加）

- 新增 `POST /api/diff{a,b}`：句子级 `SequenceMatcher` 出块（equal/delete/insert/replace），对 replace 块再做**逐字 diff**（`_inline_diff`，块长 ≤4000 字），前端渲染成"整段里只有几个词飘红/飘绿"。
- 统计口径单独算：相似度与增删字数都走**逐字** `SequenceMatcher`（≤12000 字），否则"每句都改一点"会被句级 matcher 判成相似度 0——第一版就是这个坑，实测 29% 改成 94% 才是真实感受。
- UI：结果卡里加「改写结果 / 差异对比」分段切换，切到对比时按需请求并缓存（`state.diff`），重新改写会清缓存；右侧显示统计。
- 验证：mock 实例跑通整条链路（输入 115 字 → 输出 104 字，diff 标出 `嗯、` `今天呢` `就是说，` `然后第二点，` 四处删除，相似度 94% · 删 12 字 / 增 1 字）；真实实例 API 校验 `ratio=0.391, removed=21, added=7`。

### 10.19 并排对比（2026-10-25 追加）

- 结果卡的三段开关：`改写结果 / 差异对比 / 并排对比`；后两者共用同一份 `/api/diff` 结果（`state.diff` 缓存），切来切去不再请求。
- 并排用**单容器 CSS Grid**（`grid-template-columns: 1fr 1fr`，每个 op 输出左右两个 cell），行高由 Grid 自动对齐，天然同步滚动；不用两个独立滚动面板，省掉 JS 同步逻辑。
- 逐字级 diff 在左右两栏各渲染一半：左栏只保留 equal + delete，右栏只保留 equal + insert，所以"改了哪几个字"在两栏里位置一一对应。
- 实测：1400px 下两栏各 456px，4 个 cell、4 处 `<del>` / 1 处 `<ins>`，横向无溢出；窄屏降字号与内边距，仍保持两栏对齐。

### 10.20 文案改写历史（2026-10-25 追加）

- 新增 `v2i/history.py`：`.runtime/rewrite_history.json` 存最近 60 条，字段含 `input/output` 全文（可再编辑）、风格、模型、token、字数、时间；写入用临时文件原子替换，进程内加锁（HTTP 是多线程）。
- `/api/rewrite` 成功后自动落一条；新增 `GET /api/rewrite/history`（列表，轻量摘要）、`GET /api/rewrite/history?id=`（全文）、`POST /api/rewrite/history/delete`、`POST /api/rewrite/history/clear`。
- GUI「文案改写」页底部加「改写历史」卡片：`时间 · 风格 · 入→出 字数` + 输出摘要，右侧「查看 / 再编辑 / 删除」；查看会把结果回放到结果区（含并排/差异可用），再编辑把原始输入放回输入框。
- 实测（真实 DeepSeek，非 mock）：210 字 → 207 字 / 532 tokens，历史列表出现 1 条，点「查看」结果区显示 `历史 · 10-25 00:44:23 · 210 字 → 207 字 · deepseek-chat`，正文 3 段。

### 10.21 保留并展示原视频（2026-10-25 追加）

- 新增 `cfg.keep_video`（`--keep-video` / GUI 勾选 / `V2I_KEEP_VIDEO=1`）：为真时 yt-dlp 取 `bv*[height<=720]+ba/b[height<=720]/best` 并 `merge_output_format=mp4`，产物落 `source.mp4`（默认只下 bestaudio，体积约 1/3）。
- `fetch.download_audio()` 改为 `download_media()`，返回 `(路径, 是否视频)`；`source.json` 增加 `media` 字段与 `kind=video`。转写直接把 mp4 交给 faster-whisper（PyAV 解音轨），不重复下载。
- 前端：文件列表对 `.mp4/.webm/.mkv` 标 `VIDEO`（已有 `_file_kind` + `CONTENT_TYPES` 映射），点击弹层用原生 `<video controls>` 播放，`/api/preview` 的 Range 支持让进度条可拖。
- 实测：3 分 32 秒视频 → `source.mp4` 12.6 MB（1280 宽），转写 64 段（4.95× 实时）；GUI 文件列表识别为 `VIDEO`，弹层 `<video>` `duration=212.3s / videoWidth=1280`，标题栏显示大小与下载入口。

### 10.22 老任务补下原视频（2026-10-25 追加）

- 新增任务类型 `kind="video"`：`POST /api/fetch_video{dir}` 入队，worker 里 `_fetch_video_job()` 读 `meta.json` 的 `webpage_url`，强制 `keep_video=True` 重下，成功后回写 `source.json`（`kind/media/audio`），逐字稿不动。
- `_run_summary()` 增加 `has_video`（看 `source.json` 的 media 后缀，兜底扫 `source.*`）；结果卡据此决定是否显示「补下原视频」按钮——已有视频或本地文件任务自动隐藏。
- 前端点按钮 → 入队 → 轮询 `/api/job` → 完成后 `openRun(dir)` 重渲染文件列表。走任务队列而不是同步请求，长视频不会把 HTTP 连接吊死，进度也能在日志里看到。
- 实测：对只有 `source.m4a` 的老任务补下载，2.8 秒拿到 14.1 MB mp4，`/api/files` 立刻出现 `kind=video`，`has_video` 由 false 变 true。

### 10.23 两个开关提到模式行（2026-10-25 追加）

- 「保留原视频（界面里可播放）」和「忽略缓存，全部重跑」从高级选项移到模式勾选那一排（和「文案」同位置），高级选项里删除；id 不变（`keepVideo` / `force`），JS 无需改。
- 视觉上区分"要产出什么"和"怎么跑"：模式是实线蓝底 pill，两个开关是**虚线灰字** pill，中间加一条竖分隔线（`.modes-sep`）；勾上后虚线变实线、淡蓝底。
- 实测 1400px 下 6 个 pill 单行（行高 35px），无横向溢出；窄屏自动换行。

### 10.24 历史任务页（2026-10-25 追加）

- 左菜单新增【历史任务】（排在【新建任务】之后），页面 = 列表 + 详情：列表按时间倒序，带 `VIDEO` / `部分跳过` 徽标、时长/取词方式/产物数/耗时/token；支持关键词搜索与「只看有视频的」过滤。
- 详情没有重写一套渲染：结果卡（`#resultCard`）在切换页面时**搬 DOM**——进历史页时 append 到 `#histDetailSlot`，回别页时按记录的 `{parent, next}` 原位插回；`openRun()` 及所有 tab/文件/预览逻辑完全复用，零改动。
- 点列表行 → `openHistoryRun(dir)`：标记 active、调用 `openRun`、滚动到卡片。
- 实测：4 条记录正确渲染，点第一条后 11 个 tab、正文 5 段、行高亮；切回新建页结果卡自动归位。

### 10.25 新建任务页去掉历史栏（2026-10-25 追加）

- 删掉 `#page-new` 右侧的「历史」`<aside class="side-col">`（含 `#runs` / `#refreshRuns`），历史只保留在【历史任务】页；`.layout` 由 `minmax(0,1fr) 320px` 改为单列。
- JS 侧做了空引用保护：`loadRuns` 的失败提示、`renderRuns` 的写 DOM、`#refreshRuns` 的监听都判空；`state.runs` 仍在 `renderRuns` 开头赋值，所以历史页的搜索/过滤不受影响。
- 实测：`#runs`、`.side-col` 均不存在，主列宽 1198px（原 860）；历史页 4 条、点开 11 个 tab、切回新建页结果卡回到 `#page-new`，控制台零异常。

### 10.26 历史任务：分页 + 删除（2026-10-25 追加）

- 分页：每页 5/10/20/50 可切换（默认 10），页码窗口 ±2 带省略号，底部显示「第 x / y 页 · 每页 n 条」；搜索/过滤会重置到第 1 页。纯前端切片，`/api/runs` 保持一次返回全量（几十条级别足够）。
- 删除：行内红色「删除」按钮（`stopPropagation` 不触发查看），确认框后调 `POST /api/run/delete`；后端把目录 **move 到 `.trash/<原名>-<时间戳>`** 而不是 `rmtree`，误删可捞回；`.trash/` 已加 gitignore。删的正好是当前打开的详情时，一并隐藏结果卡。
- 实测：临时造 6 条占位任务把总数推到 10 → 每页 5 出现 `上一页 1 2 下一页 / 第 1/2 页`，翻到第 2 页 5 条、页码 2 高亮；点删除（自动确认）后行数 5→4、计数 10→9；控制台零异常。测完把占位任务和 `.trash` 残留都清掉，列表回到 4 条真实记录。

### 10.27 新建任务页回到默认态（2026-10-25 追加）

- 现象：从【历史任务】点开某条详情再切回【新建任务】，结果卡还留在页面上，看着像"新建页默认在展示历史"。
- 修法：`switchPage("new")` 时若本会话没有跑过任务（`state.jobId` 为空），隐藏 `#resultCard` 并把 `#emptyState`（怎么用）重新显示；本会话跑过任务则保留结果卡。
- 顺带修掉一个旧问题：`openRun()` 只把「怎么用」卡隐藏、从不恢复，导致看过任意结果后它永久消失；现在随页面切换恢复。
- 实测：历史页点开一条（cardHidden=false、卡片已在 `#histDetailSlot`）→ 切回新建页（cardHidden=true、emptyState 可见、两个 section 都正常），控制台零异常。

### 10.28 「怎么用」改成 tips 浮层（2026-10-25 追加）

- 删掉页面底部的「怎么用」大卡片（`#emptyState`），卡片头右上角加 `怎么用 ?` 按钮（`#howtoBtn`）+ 绝对定位浮层（`#howtoPop`），右上角对齐、宽 360px、带阴影。
- 交互：点按钮开合（`aria-expanded` 同步、按钮高亮），点浮层外或按 Esc 关闭；窄屏浮动改为 `min(86vw, 360px)`。
- `switchPage` 里对 `#emptyState` 的显示/隐藏逻辑随之删除，`openRun` 的死引用也清掉（元素已不存在）。
- 实测：默认 `hidden=true`；点击后 3 条步骤、宽 360、右边缘与按钮对齐；点空白处关闭；控制台零异常。页面现在只剩一张表单卡。

### 10.29 模型调用记账页（2026-10-25 追加）

- 新增 `v2i/usage.py`：每次 LLM 调用写一行到 `.runtime/llm_calls.jsonl`（任务、阶段、模型、输入/输出 token、耗时、估算费用）；价格表内置 deepseek / moonshot / gpt-4o / qwen 常见型号（元/百万 token，美元按 7.2 折算），可用 `V2I_PRICE_TABLE` / `V2I_PRICE_OVERRIDE` 覆盖。
- `LLM.chat(..., stage=)` 增加阶段参数并在成功/失败时记账（`_record`，记账异常不影响主流程）；`llm.task` 由调用方设置：提炼是任务目录名，文案改写是「（文案改写）」，连通性测试是「（系统配置 · 连通性测试）」。
- 阶段标签：`map #n`、`reduce:summary/points/article`、`copy`、`copy i/n`、`rewrite i/n`、`ping`。
- `GET /api/usage` 聚合：按任务汇总 + 逐次明细；对早于记账的任务用 `manifest.distill.usage` 兜底（标记 fallback，明细里显示「提炼（汇总，无逐次记录）」）。
- GUI 新增【模型调用】页：5 个汇总块（任务数/调用次数/输入/输出/估算费用）+ 任务表，点行展开逐次调用明细（时间/阶段/模型/输入/输出/费用）。
- 实测：ping 9/2 tokens ¥0.000034，改写 295/19 ¥0.000742，历史任务兜底 5 次 6401/4114 ¥0.045714，合计 7 次 6705/4135 ¥0.0465；展开行 6 列正常，控制台零异常。

### 10.30 菜单顺序调整（2026-10-25 追加）

- 【模型调用】移到【系统配置】正上方，最终顺序：新建任务 / 历史任务 / 文案改写 / 模型调用 / 系统配置。只改 HTML 顺序，事件绑定按 `data-page` 走，无逻辑影响；实测导航四项文本顺序正确、模型调用页正常渲染。

### 10.31 cookies 一键获取（2026-10-25 追加）

- 新增 `POST /api/cookies/browser {browser}`：用 `yt_dlp.cookies.extract_cookies_from_browser` 读取本机浏览器登录态，先写 `.cookies/cookies.pending.txt` 并返回 `{count, domain_count, domains}`（不返回 cookie 值，只给数量和域名）。
- `POST /api/cookies/browser/confirm` 才把 pending 改名为 `cookies.txt`；`/cancel` 丢弃 pending。不确认就不覆盖现有配置。
- 失败路径带可行动提示：Chrome `Could not copy Chrome cookie database`（浏览器在跑）→ 提示完全退出 Chrome；Edge `Failed to decrypt with DPAPI` → 建议扩展导出；Firefox 实测可用（37 条 / 9 个域名）。
- GUI：cookies 行新增「一键从浏览器获取」，展开一个虚线面板：三个浏览器按钮 + 读取结果 + 确认/不用了；面板 `grid-column: 1 / -1` 占满整行（第一版挤在网格第一列，已被截图抓出来修掉）。
- 踩坑：这三个路由一开始插在 `do_POST` 顶部、早于 body 解析，导致 `UnboundLocalError` 直接把连接掐断（客户端只看到"连接被意外关闭"）；移到 body 解析之后再无问题。

### 10.32 字幕优先并入模式行（2026-10-25 追加）

- 「字幕优先（有字幕就不转写）」从高级选项移到与「保留原视频 / 忽略缓存」同一排 pill（顺序：产物四项 → 分隔线 → 字幕优先 / 保留原视频 / 忽略缓存），id 仍是 `preferSubs`，JS 无需改动。
- 实测 1400px 下 7 个 pill 单行（35px），无横向溢出；高级选项里已无该复选框。

### 10.33 「保留原视频」默认勾选（2026-10-25 追加）

- 前端 `#keepVideo` 加 `checked`，新任务默认下载 mp4；CLI 的 `--keep-video` 仍保持"显式开启"（`cfg.keep_video` 默认 False），避免脚本里悄悄多下几百 MB。
- 实测 DOM `keepVideo.checked === true`，与「字幕优先」一起显示为实线 pill。

### 10.34 cookies 明细勾选导入（2026-10-25 追加）

- `/api/cookies/browser` 现在返回 `items`：每条 cookie 的 `{i, domain, name, path, expires, secure, http_only}`（**不返回值**），前端渲染成可勾选列表。
- `/api/cookies/browser/confirm` 接受 `{keep:[i,...]}`，只把勾选的写进 `cookies.txt`（`keep` 为空/缺省=全部）；实现用 `MozillaCookieJar` 重新装载 pending 再按索引过滤。
- 列表支持：全选/取消（只影响当前过滤结果）、按域名/名称过滤、实时计数「已选 n / m」；一条不选时确认按钮隐藏并提示。
- 踩坑：`.ck-row` 是 `label`，被 `.adv-grid label { flex-direction: column }` 覆盖成竖排（特异性 0,1,1 > 0,1,0），改成 `.grab-list-body .ck-row` 才排成四列。
- 实测：Firefox 37 条 → 选 6 条（youtube + x.com）导入后 cookies 只剩 6 条；再全选导入恢复 37 条；控制台零异常。

### 10.35 高级选项红色加粗（2026-10-25 追加）

- `.advanced summary` 由灰色小字改成 `#c0392b` + `font-weight:700`，`::marker` 同步染色，展开后颜色再深一档（`#a32f22`），hover 也加深；`padding:3px 0` 让点击区大一点。
- 实测 computed style：`rgb(192, 57, 43)` / `700` / `13px`。

### 10.36 所有文件悬浮说明（2026-10-25 追加）

- `renderFiles` 给每行加 `data-tip`，由 `FILE_TIPS` 正则表映射到中文业务含义（摘要卡/观点清单/长文/文案/文案纯文本/结构化数据/逐字稿/字幕/元数据/运行记录/分块/取词来源/原视频/原始音轨/notes 分块要点/日志）。
- 自己实现了一个 `#tip` 浮层（`position:fixed`，深色底，`pointer-events:none`），`mouseover` 委托 + 越界翻转到上方 + 滚动即隐藏；不用原生 `title`，避免 1 秒延迟和样式不可控。
- 行光标改成 `cursor: help`（可点击行仍是 pointer）。
- 实测：`manifest.json` hover 出「运行记录：每个阶段的做法、耗时、token 用量」，浮层宽 286px、位置随行、零异常。

### 10.37 去掉表单下的提示段落（并修类名冲突）（2026-10-25 追加）

- 症状：开始按钮下方出现一条深色圆角气泡，里面是"有原生字幕的视频会直接取字幕…"的文字。
- 根因：10.36 给悬浮提示新写的 `.tip { position:fixed; background:#1f2937; … }` **撞上了正文里已有的 `<p class="tip">`**（原本是灰色小字说明），于是整段文字被渲染成浮层样式。
- 处理：删掉那条提示段落（内容已在「怎么用」tips 里覆盖），并把自定义浮层改名为 `.hover-tip`（元素 id 仍是 `#tip`，JS 不用改），避免以后再撞名。
- 实测：页面无 `p.tip`；浮层仍正常（`position:fixed`、`rgb(31,41,55)`、hover `manifest.json` 显示运行记录说明）；控制台零异常。

### 10.38 ASR 模型与登录态 cookies 提到一级（2026-10-25 追加）

- 新增 `.primary-opts` 区块（置于产物勾选行与「开始」之间，顶部一条虚线分隔）：左边 ASR 模型下拉，右边整条 cookies 行（状态 + 一键获取 + 上传 + 清除）及其浏览器选择面板。
- 高级选项收缩为：语言 / 文案风格 / 浏览器 Cookies / 代理 / LLM 模型。
- 踩坑：第一次用"先算索引再删两段"的脚本改 HTML，删完第一段后第二段索引失效，把 advanced 区块切坏（重复 cookie 行 + 丢了 浏览器 Cookies/代理 字段）；改为按 `<details class="advanced">` … `</details>` 整体重写才恢复。
- 实测：`asrModel` 与 `cookieStatus` 的 `closest("details")` 均为 null、`closest(".primary-opts")` 均命中；控制台零异常。

### 10.39 cookies 字段与 ASR 字段统一展示（2026-10-25 追加）

- 登录态 cookies 由"一整行按钮"改成和 ASR 模型同构的字段：`label` 在上面（同样的 12px 灰字），下面是 38px 高的 `.cookie-box`（状态文字 + 右侧「一键获取 / 上传 / 清除」三个文字按钮）。
- ASR 的 select 同步改成 `width:232px; height:38px`，两个字段现在同高（38 vs 38）、同一基线，`sameRow=true`。
- 浏览器选择面板 `#cookieGrabPanel` 用 `flex-basis:100%` 在下面独占一行（实测宽 1160px）。
- 顺带修：探测失败时"去处理 cookies"的动作原本展开高级选项 + 高亮 `.cookie-row`，现在指向一级的 `.cookie-field`，并把 `.flash` 动画挂到 `.cookie-box`。

### 10.40 两个字段改为垂直排列（2026-10-25 追加）

- `.primary-opts` 由 `flex-direction: row` 改成 `column`，ASR 模型与登录态 cookies 上下各占一行、整宽、左边缘对齐（实测两者 x 都是 197、宽 1160）。
- ASR 下拉由 232px 改成 `width:100%`，与 cookies 框等宽；面板仍在其下方整宽展开。

### 10.41 两个字段收回高级选项（2026-10-25 追加）

- 撤销 10.38/10.39/10.40 的一级展示：`.primary-opts` 整块删除，ASR 模型作为普通 `<label>` 回到 `adv-grid` 第一格；登录态 cookies 作为 `.cookie-field`（`grid-column: 1/-1`）放在网格末尾，仍是「标签 + 整宽 `.cookie-box`」的形态。
- 高级选项当前顺序：ASR 模型 / 语言 / 文案风格 / 浏览器 Cookies / 代理 / LLM 模型 / 登录态 cookies。
- 探测失败跳转逻辑恢复为先展开 `details.advanced` 再高亮 `.cookie-field`（字段又藏起来了，不展开会闪不到）。
- 实测：`asrModel` 与 `cookieStatus` 的 `closest("details")` 均命中、`.primary-opts` 已不存在；展开后 cookies 框整宽 1160px、位于 ASR 下方。

### 10.42 修复「文案」取消不掉 + 零产物提示（2026-10-25 追加）

- 现象：把摘要卡/观点清单/长文都取消后，再点「文案」取消，勾选会弹回来。
- 根因：init 里有个"至少保留一个模式"的守卫——`change` 时若 `input[name="mode"]:checked` 为 0 就 `el.checked = true` 强制回选；当文案是最后一个被勾的项时，它必然被弹回。
- 处理：删掉这个守卫，四个产物允许全部取消；改为在模式行末尾用 `.modes.no-mode::after` 显示红色「↑ 至少选一份产物」，提交时原有的 alert 文案补上「文案」。
- 踩坑：新增的 change 监听第一次插到了 `#srcSwitch` 的**另一处**绑定（不在 init 里，永不执行），导致提示不出现；按「启动」注释定位后重新插入才生效。
- 实测：依次取消三个后点「文案」→ `copy:false`（不再弹回）；全部取消时 `no-mode` 类生效、`::after` 内容为「↑ 至少选一份产物」。

### 10.43 cookies 列表加表头与排序（2026-10-25 追加）

- 列表上方新增 `.grab-cols` 表头：域名 / 名称 / 过期三个按钮，点击切换升序/降序（`▲`/`▼`），当前排序列高亮（`.ck-col.on`）。
- 排序实现：`paintCookieRows()` 按 `state.cookieSort` 排序后重绘；`expires` 里的「会话」固定排最后，其余按日期字符串比较；同值按原始索引稳定排序。
- 勾选状态改为 `state.cookieSelected`（Set of index），排序/重绘后**选择不丢**（实测取消 2 条 → 已选 35/37，排序两次后仍是 35/37）；确认导入也改成读这个 Set。
- 每个单元格补了 `title`：域名带 path、名称标注 HttpOnly/Secure、过期时间原值，长内容悬停可见。
- 实测：按名称升序首行为 `__cf_bm`、降序首行为 `ZFY`，caret 与高亮同步，控制台零异常。

### 10.44 cookies 列表展示值（2026-10-25 追加）

- `/api/cookies/browser` 的 `items` 增加 `value` 字段；列表新增「值」列（等宽字体、超长省略、`title` 给全文），表头同样可排序。
- 列宽改为 `18px | 1.1fr | 0.9fr | 1.6fr | 72px`，表头与行保持一致。
- ⚠️ 安全口径变化：之前设计是"值不出服务器"，现在为了让人看清每条 cookie 的内容（排查登录态问题），值会进入页面 DOM；截图、录屏、共享屏幕会连带暴露会话凭证。导入逻辑仍走服务端 pending 文件，不依赖前端回传值。
- 实测：37 条正常渲染（BIDUPSID / PSTM / BAIDUID…），表头四项，控制台零异常。

### 10.45 修复「清除 cookies」后的 501（2026-08-14 追加）

- 现象：点「清除」后状态栏变成一坨 HTML —— `501 Unsupported method ('{}GET')`。
- 根因：`do_POST` 里 `/api/cookies/clear` 分支在**读取请求体之前**就返回了；HTTP/1.1 keep-alive 连接上残留的 `{}` 被服务器当成下一个请求行，于是紧随其后的 `GET /api/cookies` 变成非法方法 `{}GET`。
- 处理：`do_POST` 一进来就 `self.close_connection = True`，POST 响应后关闭连接、不复用；简单粗暴但彻底消除这类"body 没读完导致流错位"的问题（本地工具，多开一条 TCP 无感）。
- 实测：点清除 → 状态栏正常变「未上传 —— …」，控制台零异常；随后 `/api/cookies` 正常返回。

### 10.46 抖音链接自动归一（2026-08-14 追加）

- 报错「这个链接形式不被支持」来自 yt-dlp 的 `Unsupported URL`，不是本地校验；抖音 `/shipin/` 合集页、部分分享页 yt-dlp 不认。
- 新增 `resolve_douyin()`：在 `probe()` 与 `fetch()` 入口先跑一次——
  - `?modal_id=<id>` 直接改写成 `/video/<id>`（最常见的分享链接形态）；
  - 其它抖音链接 GET 一次页面（带 cookies 里的 douyin cookie + 桌面 UA），从跳转后的 URL、`/video/<id>`、`"aweme_id":"<id>"`、`aweme_id=<id>` 里找视频 id；
  - 找不到就原样返回，不阻断主流程。
- 报错文案改成"可用形态清单"（/video/<id>、v.douyin.com 短链、带 modal_id 的分享链接），不再只提 /shipin/。
- 实测：`/video/` 原样通过；`?modal_id=` 正确改写；假 id 的 `/shipin/` 与假短链不崩、原样返回。

### 10.47 抖音 /user/...?modal_id= 链接支持 + cookie 现实（2026-08-14 追加）

- 用户给的 `https://www.douyin.com/user/<sec_uid>?from_tab_name=main&modal_id=<id>&vid=<id>` 现在能识别：`resolve_douyin()` 取 `modal_id` 拼成 `/video/<id>`，探针日志能看到「链接已转换（modal_id）」。
- 转换后 yt-dlp 的 Douyin 提取器能走到视频，但报 `Fresh cookies (not necessarily logged in) are needed`。
- 试过用 `POST https://ttwid.bytedance.com/ttwid/union/register/` 伪造 `ttwid` 注入 cookie 文件：**不够**，yt-dlp 仍判定不新鲜（它还要 msToken / odin_tt / csrf 之类真实访问种下的值）。结论：抖音只能靠"真实浏览器访问过 douyin.com"的 cookies。
- 报错文案据此改写：说明先在浏览器打开一次站点、再一键获取；Chrome/Edge 读不出来就走扩展导出或 Firefox。

### 10.48 抖音：cookies 够也进不去（a_bogus）→ 引导走本地文件（2026-08-14 追加）

- 用户一键导入 cookies 后仍报 fresh cookies。核对 `cookies.txt`：**55 条抖音 cookie 已就位**（sessionid / sid_guard / ttwid / odin_tt / s_v_web_id / UIFID …），不是"没导进来"。
- 真相：yt-dlp 2026.10.21 的 Douyin 提取器调 `aweme/v1/web/aweme/detail/`，该接口现在强制 `a_bogus` 签名。实测：
  - 带 cookies 直接请求 → `403 Blocked by ArgusSecurityPlugin Uifid Not Found`；
  - 伪造 `ttwid`（ttwid.bytedance.com 注册接口）→ 仍然不够；
  - 页面 HTML 里只有 dns-prefetch 和 CSP，真正的播放地址是运行时 XHR 拿的，抓不到。
- 结论：这不是 cookies 问题，是签名问题，换 cookie 无解。已在 `friendly_error()` 里对抖音单独给出诚实解释，`error_action()` 返回新增的 `local`，前端据此切到「本地文件」并自动弹文件选择框。
- 推荐工作流：抖音客户端/网页下载 mp4 → 拖进 v2i → 转写 + 提炼（这两步完全不依赖抖音接口）。

### 10.49 抖音外部下载器兜底（f2 / douyin-downloader）（2026-08-14 追加）

- 新增 `v2i/douyin.py`：探测 `f2` / `douyin-downloader`（PATH → `.tools/`），下载到 `<run>/downloaded/`，返回最新视频文件；支持 `V2I_DOUYIN_CMD` 自定义命令（占位符 `{url}` `{dir}`）。
- `fetch._douyin_fallback()`：probe 报 a_bogus/fresh-cookies 时自动接管；`probe()`/`_run_one()`/`_cmd_fetch()` 都会在"下载器可用"时绕过探测，用 `douyin-<id>` 作为工作目录名。
- 识别报错时 keys 兼容中文翻译（`a_bogus` / `签名` / `fresh cookies`），否则翻译后的文案会让判定失效（第一次就踩了）。
- 坑：Windows 下 `.cmd` 不能被 `subprocess` 直接执行，必须 `cmd /c` 包一层；测试桩最初用 ASCII 写死中文路径导致 copy 失败，改成先复制到 ASCII 路径。
- 实测（用桩程序模拟 f2）：`v2i run <抖音链接> --mode copy --copy-style raw` → 探测失败 → 外部下载器拿到 14.7MB mp4 → 转写 49s → 产出 copy.md/digest/transcript，全链路通；移除桩后优雅降级为提示安装。

### 10.50 抖音下载器可选（f2 / douyin-downloader / 自定义）（2026-08-14 追加）

- `v2i/douyin.py` 重构：`list_tools()` 报告三种工具的可用性与路径；`find_downloader(prefer)` 支持指定优先；`download(..., prefer=)` 按选择执行对应命令（f2 → `dy -u -p -k`，douyin-downloader → `-u -o`，custom → `V2I_DOUYIN_CMD` 模板）。
- `Config.douyin_downloader`（`V2I_DOUYIN_DOWNLOADER`）贯通 CLI（`--douyin-downloader auto|f2|douyin-downloader|custom`）、web（start body / upload query）与 fetch 兜底逻辑。
- 新增 `GET /api/douyin_tools`；高级选项新增「抖音下载器」下拉，选项带 `✓ 已安装 / 未安装` 标注，默认「自动」。
- 实测：桩 f2 存在时 `auto` 与 `prefer=f2` 都能命中，`prefer=douyin-downloader` 返回 None（不误用）；`/api/douyin_tools` 三种工具状态正确；下拉渲染 4 项、控制台零异常。

### 10.51 cookies 收进系统配置（2026-08-14 追加）

- `#cookieStatus / cookiePick / cookieGrab / cookieClear / cookieGrabPanel` 整块从【新建任务】的高级选项搬到【系统配置】页，独立成「登录态 cookies」分组；提示语改为"所有任务共用这一份、改动立即生效"。
- 高级选项原位留一条 `adv-hint`：「登录态 cookies 已移到 系统配置 统一管理」，点一下 `switchPage("settings")` 并滚到该字段。
- 探测失败的 cookies 动作也改成跳【系统配置】再高亮（不再展开高级选项）；`switchPage("settings")` 时顺带 `loadCookies()` 刷新状态。
- 实测：`details.advanced` 里已无 cookie 字段、系统配置里可见且状态正确（93 条）；跨页跳转后 `#page-settings` 显示、字段可见；控制台零异常。

### 10.52 查看已导入的 cookies 列表（2026-08-14 追加）

- 新增 `GET /api/cookies/list`：`MozillaCookieJar` 解析 `cookies.txt`，返回 `{present,count,items[]}`，字段与"从浏览器读取"的列表一致。
- cookies 行新增「查看」：展开 `#cookieViewPanel`（复用 `.grab-list` 样式），四列表头可排序（域名/名称/值/过期，会话排最后）、按域名名称过滤、显示「共 N 条 / 命中 M 条」，纯只读。
- 实测：93 条正确渲染、排序与过滤正常、控制台零异常。

### 10.53 系统配置三个按钮统一为 primary（2026-08-14 追加）

- 「测试大模型连接」「清除 API Key」由 `ghost` 改为 `primary`，与「保存并生效」完全一致：同色 `rgb(59,98,246)`、同高 47px、同字重 600、同圆角 10px、同样的 hover 加深与 disabled 半透明。
- 清除仍保留二次确认弹窗（破坏性操作不变）。
- 实测：三个按钮 computed style 逐项一致（高度/圆角/字重/前景色），控制台零异常。

### 10.55 在 Python 3.14 上把 f2 装起来（绕开编译）（2026-08-14 追加）

- 用户 `pip install f2` 失败：f2 锁死 `pydantic==2.9.*` / `pyyaml==6.0.2`，这两个版本没有 cp314 wheel，本机还没有 MSVC/能用的 link.exe（日志里 `link: extra operand` 说明命中的是 GNU link）。
- 可行装法（已实测）：`pip install --target .tools/f2 --no-deps f2`，再装放宽版本的依赖
  （`pydantic>=2.12`、`pyyaml>=6.0.3` 都有 cp314 wheel，其余 aiofiles/httpx/rich/websockets… 也都有）。f2 0.0.1.7 在新版 pydantic 下能正常启动。
- `_run()` 会为 `.tools/` 下的 `--target` 安装自动注入 `PYTHONPATH`（f2.exe 的包在上两级目录），所以不必改系统环境变量。
- f2 的真实命令对齐：`f2 dy -u URL -p DIR -M one -k <cookie 字符串> [-P 代理]`（`-k` 要字符串不是文件路径，新增 `_cookie_string()` 从 cookies.txt 过滤 douyin 域拼接；支持传代理）。
- 实测到 403：f2 能算出 a_bogus 并发出请求，但抖音返回 403（风控/IP/登录态问题），不是命令拼装问题；后续需要新鲜登录 cookies 或代理再试。

### 10.56 复现 403 的结论（2026-08-14 追加）

- 用当前 93 条 cookies（含 `sessionid` / `ttwid` / `msToken`）跑 f2，两个视频 id（modal_id 与 vid）都稳定 `403 Forbidden`，不是命令拼装或 cookie 传递问题。
- f2 已是最新版 `0.0.1.7`（PyPI 无更新），发布于 2024-11；抖音的 a_bogus 与 App 参数迭代快，旧版签名被风控识别。
- `--auto-cookie firefox` 也会失败（需要交互确认 + browser_cookie3 读不到）。
- 结论：抖音这条路当前无解，除非换更新更频繁的下载器（douyin-downloader 一类）或使用官方客户端下载后走「本地文件」。
- 代码侧保持"装了就能用"：`/api/douyin_tools` 检测到 f2 显示已安装，命令模板与代理透传都已就绪，等下载器本身能过风控即可自动接管。

### 10.57 再试 douyin-downloader v2（仍被风控）（2026-08-14 追加）

- 拉取 `jiji262/douyin-downloader` v2.0.0（较新的 a_bogus 实现）到 `.tools/douyin-downloader/`，依赖装进 `deps/`（aiohttp 3.14.5 等全部有 cp314 轮子）。
- 用它跑同一个视频（配置里带 ttwid/odin_tt/sessionid）：`Success 0 / Failed 1`，原因「抖音未返回作品详情（可能触发了风控验证）」。
- f2 403、douyin-downloader 风控 → 两个独立实现同时失败，指向 **IP/风控层**，不是签名或 cookie 传递问题。
- 顺手把 run.py 版接进 `douyin.py`：自动识别 `.tools/douyin-downloader/douyin-downloader-main/run.py`，按 cookies 生成 `downloader.config.yml`（含 proxy），`PYTHONPATH` 指向 `deps/`；`/api/douyin_tools` 现在两种工具都显示已安装。
- 结论：代码路径已就绪，缺的是能过风控的出口 IP（代理）或换网络环境。

### 10.58 浏览器自动化打通抖音（2026-08-14 追加）

- 新增 `tools/douyin_browser.mjs`：启动本机 Chrome（`--headless=new` + 临时 profile + 随机调试端口），连接到 **page target** 的 WS（browser 级 target 收不到 Network 事件，第一次就栽在这），`Network.enable` 后导航到视频页。
- 关键点：**让页面自己算 a_bogus**。页面加载时会主动请求 `/aweme/v1/web/aweme/detail/`，我们只监听 `Network.responseReceived` 再 `Network.getResponseBody` 读 JSON，从 `aweme_detail.video.play_addr / download_addr / bit_rate[].play_addr` 取真实 mp4 地址。
- 下载：带 `Referer: https://www.douyin.com/` + 桌面 UA 拉取，实测 `200 video/mp4`、7.97MB、magic `ftypisom`；把 `desc` 写进 `downloaded/douyin-meta.json`，`fetch` 用它当标题（不再是 douyin-video）。
- 集成：`douyin.py` 新增 `browser` 工具（`browser_ready()` 检查 node + Chrome），auto 顺序改为 浏览器 → f2 → douyin-downloader → 自定义，且**逐个尝试直到成功**（之前只挑第一个）；GUI 下拉显示「浏览器自动化（本机 Chrome + CDP）✓ 已安装」。
- 实测整链路：`v2i run <抖音链接> --mode copy --copy-style raw` → 浏览器下载 8.0MB → 转写 2.8×（65s 视频）→ 产出 copy.md/digest/transcript，用时 39s。
- 收尾：脚本结束删临时 profile（验证跑完 `%TEMP%` 无 `v2i-cdp-*` 残留）。
- 代价：每次多 15–25s 启动 Chrome + 载页面；headless 可能被站点识别（这里未触发），失败时自动回退到 f2。

### 10.59 分享文案直接可用 + 探针不再误报（2026-08-14 追加）

- 用户贴的是整段抖音分享（`Fhb:/ O@X.Zz #话题 https://v.douyin.com/xxx/ 复制此链接…`）。新增 `util.extract_url()`，CLI（run/fetch/probe）与 web（`/api/start`、`/api/probe`）入口统一先抽链接。
- 短链解析：`resolve_douyin` 跟跳转拿到 `/video/7700226586464777209`；工作目录名与 `meta.id` 改用解析后的 id（之前短链没有数字，目录叫 `douyin-video`）。
- 实跑整链路（39.5MB、4 分 21 秒）：抽出短链 → 解析 → 浏览器自动化下载 → 转写 → 提炼，95 秒完成，产出 copy.md/digest/transcript。
- `/api/probe` 不再对抖音报"失败"：命中"签名不可用但下载器可用"时返回 `{ok:true, note:"抖音签名接口不可用，将用「浏览器自动化…」下载后再转写", via:"browser"}`，避免用户以为链接不支持。
- 抖音报错文案同步改为"会用浏览器自动化下载，无需处理"，不再让人去装 f2。

### 10.60 前端识别分享文案（2026-08-14 追加）

- URL 输入框改为「粘贴视频链接，或整段分享文案」；`input` 事件里用正则抽链接，命中时在下方显示 `已从分享文案中识别到链接：<code>…</code> · 会用它解析`，纯链接则隐藏该提示。
- 「先探测」和「开始」都改用抽出来的链接提交（后端也会再抽一次，双保险）。
- 实测：整段抖音分享（含 `Fhb:/`、话题、`复制此链接…`）贴进去 → 提示出现；走 GUI 任务（`POST /api/start`）跑完 98.7s，产出 copy.md/digest/transcript，目录 `douyin-7700226586464777209`。

### 10.61 探针针对抖音/cookie 平台优化（2026-08-14 追加）

- 新增 `fetch.cookie_domains()` / `fetch.missing_cookie_domain()`：读 `cookies.txt` 域名，和 `platforms` 的 hosts 比对，判断该平台有没有登录态。
- `/api/probe` 两条快捷路径：
  1. **抖音 + 有下载器**：不再调 yt-dlp（原来要等 5–10 秒才失败），直接 `resolve_douyin` 拿视频 id，返回 `{ok:true, note:"…将用「浏览器自动化…」下载后再转写", resolved}`；实测 **0.6s**。
  2. **需要 cookies 但缺失**：直接返回 `{ok:false, error:"快手 需要登录态 cookies，但当前 cookies.txt 里没有 kuaishou.com 的条目——到「系统配置 → 登录态 cookies」…", action:"cookies_file"}`，前端渲染成红字 + 「上传 cookies」按钮。
- 前端 `probeUrl()` 支持 `note` 字段（不再拼 `undefined · undefined`）。
- 实测：抖音分享文案 → 0.6s 绿字 + 解析链接；快手链接 → 0.0s 红字 + 精确缺失域名 + 按钮；控制台零异常。

### 10.62 抽出独立音轨 audio.m4a（2026-08-14 追加）

- 新增 `fetch.extract_audio(media, workdir, cfg)`：用 `imageio-ffmpeg` 的内置 ffmpeg 执行 `-vn -c:a aac -b:a 128k`，产出 `audio.m4a`；本身就是音频的直接返回原文件；没有 ffmpeg 返回 None 不影响主流程。
- 触发点三处：yt-dlp 下载到视频、外部下载器（浏览器自动化/f2）拿到视频、本地拖入的视频文件（`web._run_local`，转写改读抽出来的音轨）。
- `source.json` 增加 `audio_file` 字段；文件列表自动识别为 `AUDIO`，中文含义「从视频里抽出的音轨，可直接播放或拿去配音/搬运」；`FILE_LABELS` 增加 `audio.m4a → 音频`。
- 实测：39.5MB 抖音视频 → `audio.m4a` 4.02MB，magic `ftypM4A`，耗时 1–2 秒；`/api/files` kind=audio，GUI 显示 `AUDIO audio.m4a 4.0 MB`。

### 10.63 音频抽取做成可选开关（2026-08-14 追加）

- `Config.extract_audio`（默认 True，`V2I_EXTRACT_AUDIO=0` 关闭）；`fetch.extract_audio()` 第一道判断就读它。
- GUI 模式行新增「顺带抽出音频」pill（默认勾选）；CLI 加 `--no-extract-audio`；`/api/start` 与上传 query 都透传 `extract_audio`。
- 实测：同一 39.5MB 视频，开关关闭时 `extract_audio()` 返回 None（不产出文件），开启时产出 4.02MB `audio.m4a`；模式行 8 个 pill 仍单行（35px）。

### 10.64 所有文件媒体优先排序（2026-08-14 追加）

- `_list_files()` 末尾按 kind 排序：`video → audio → image → text → binary → dir`，同 kind 内按路径字母序，目录（notes/）始终垫底。
- 实测：抖音任务的「所有文件」首行是 `VIDEO downloaded/douyin-video.mp4`，第二行 `AUDIO audio.m4a`，之后才是 chunks.json / manifest.json 这些文本。
- 排序在后端做，`/api/files` 和 GUI 一致，不需要前端二次处理。

### 10.65 人声/伴奏分离（audio-separator）（2026-08-14 追加）

- Python 3.14 上**能装**：`audio-separator 0.47.1` + `torch 2.14.1` + `onnxruntime 1.31.0` 都有 cp314 wheel；只有 Windows 专属依赖 `diffq-fixed` 编不出来（缺 pyx/编译链），实测用 `--no-deps` 装主体 + 手动补依赖可绕开（MDX/onnx 路径不需要 diffq）。
- 新增 `v2i/separate.py`：`available()/command()` 找 `.tools/separator/bin/audio-separator.exe`；`_env()` 注入 `PYTHONPATH`、把 imageio 的静态 ffmpeg 复制成 `.tools/bin/ffmpeg.exe` 并前置到 PATH（否则 audio-separator 调 ffmpeg 报 WinError 2）、`--model_file_dir` 指到可写的 `.tools/separator/models`（默认 `/tmp` 在 Windows 上 PermissionError）、并把 `cfg.proxy` 注入 `HTTP(S)_PROXY`。
- 集成：`Config.separate`（`V2I_SEPARATE`）、GUI 模式行「分离人声/伴奏」pill（默认关）、`/api/start` 与上传 query 透传、`fetch` 在抽出音频后按开关调用，产出 `vocals.mp3` / `instrumental.mp3` 并写入 `source.json` 的 `stems`；失败只打日志不中断。
- **当前卡点**：模型下载走 `raw.githubusercontent.com`，本机被重置（`ConnectionResetError 10054`）。命令、ffmpeg、proxy 透传都验证过（用错误代理会明确报 ProxyError），只差一个能出网的代理。

### 10.66 任务平台标签 + 历史页按平台筛选（2026-08-14 追加）

- `_run_summary()` 用 `meta.webpage_url / manifest.url` 跑 `platforms.match()`，返回 `platform`（id）与 `platform_label`（如「抖音」「哔哩哔哩 B站」；无 URL 的本地任务标「本地文件」）。
- 历史页新增 `.hist-select` 平台下拉（全部平台 + 各平台 + 本地文件），与搜索/只看有视频/分页联动；每行加绿色平台徽标；结果卡头部 meta 也会带上平台名。
- 实测：5 条任务（4 抖音 + 1 B 站）→ 选「抖音」后 4 条、计数「共 5 条，命中 4 条」，徽标全部为「抖音」，控制台零异常。

### 10.67 文案改写风格扩到 10 种（2026-08-15 追加）

- 新增 8 个 prompt：小红书笔记、公众号长文、知乎回答、新闻通稿、微博短文案、访谈问答、带货口播、故事化叙述；加上原有轻度整理/口播稿共 10 种，统一带「事实不许改 / 只输出正文 / 不静默删广告」硬约束，仅微博允许大幅压缩。
- 新增 `v2i/rewrite_styles.py` 登记表（id → 中文名 + prompt 名），`/api/rewrite/styles` 返回列表，前端下拉改为接口驱动（新增风格只改一个文件 + 一行登记）。
- `_rewrite` 用登记表校验与选 prompt（未知 style 回退 light）；改写历史的风格名也走登记表（`style_label`）。
- 实测四种：小红书 219 字（emoji+话题标签+互动提问）、微博 97 字（<180 字含标签）、新闻通稿 150 字（第三人称"有观点认为"）、访谈问答 166 字（Q1/Q2 格式）；历史列表正确显示「访谈问答 / 新闻通稿 / 微博短文案」。

### 10.68 失败/中断任务重试（2026-08-15 追加）

- `_run_job` 失败时写 `manifest.status="failed"` + `error`，成功时写 `"done"`；`_run_summary` 暴露 `status / error / can_retry`。
- `/api/retry` 现在支持两种入参：`{dir}`（历史任务按目录重跑：读 meta 的 `webpage_url`、manifest 的 `modes`，`force=true`；本地任务若源文件还在也能重跑）和原来的 `{url,...}`（同参数再跑一次）。
- 前端：任务卡片失败时显示「重试这次任务」+ 提示；历史任务行显示「失败 / 中断」徽标 + 「重试」按钮（`status != done` 且 `can_retry`），点完切回新建任务页并接管进度。
- 顺带修两个真 bug：
  1. `_retry` 用了不存在的 `self._cfg` → 500（改成 `self._cfg_from(body)`）；
  2. `pollJob` 里 `if (job.error) throw` 把"任务失败"误判成"请求失败"，界面显示「连接中断」而不是「失败」——改成只有拿不到 `status` 时才当请求错误。
- 另外 `read_json` 改用 `utf-8-sig`，带 BOM 的 json 不再炸整个 `/api/runs`。
- 实测：坏链接 → 卡片显示「失败」+ 重试按钮；点重试新 job 正常入队；`{dir}` 重试真实任务 76 秒跑完、产出齐全；中断任务行显示「中断」+「重试」。

### 10.69 代理测延迟（2026-08-15 追加）

- 新增 `v2i/net.py`：对 4 个目标各打一次请求（gstatic 204 / DeepSeek API / raw.githubusercontent / douyin），返回 `{label, ok, ms, detail}`，`summary` 形如「2/4 通 · 最快 180ms · 不通：抖音」。
- `POST /api/proxy/test`（body `{proxy}`，留空则用已保存的 `V2I_PROXY`）；GUI 在代理输入框右侧加「测试延迟」，结果按项显示 ✓/✗ 与毫秒。
- 失败原因做了归类：`ProxyError → 代理连不上（可能要账号密码）`、`InvalidSchema → 协议不支持（socks5 需 PySocks）`、超时、连接被重置、TLS 失败。
- vendor 里补了 `PySocks`，`socks5://` 现在能测（之前只报 InvalidSchema）。
- 实测用户给的 `ip5.934618.xyz:44889`：TCP 端口是开的（62.210.124.132，法国 OVH），但 HTTP CONNECT 与 SOCKS5 握手都被对端直接关闭，4 个目标全不通 → 大概率需要 `user:pass@` 或换协议/端口。
- 另外验证了分离质量（用 whisper 反向检验）：vocals 与原稿相似度 0.95、instrumental 0.97，`corr(orig, vocals)=1.000`、`corr(orig, instr)=0.999` → 这一版 MDX 权重在这段音频上**没有真正分离**（伴奏里人声仍在），需要换 demucs 或补齐 MDX 的 model-data 配置再试；分离耗时 170s / 4分21秒音频。

### 10.70 分离质量复核：MDX 配置并不缺，之前是误判（2026-08-15 追加）

- 复核 `Kim_Vocal_2.onnx`：onnxruntime 能加载，输入 `[batch,4,3072,256]`，是标准 MDX-Net；`download_checks.json` 已通过 jsdelivr 落地，权重通过 ghfast/ghproxy 镜像下载，**没有缺失的 model-data 配置**。
- 用「人声+BGM」的片段（B站视频 20s）重测：`corr(vocals, instrumental)=0.041`、`corr(vocals+instr, mix)=1.000`、对伴奏轨跑 whisper 相似度 **0.0**（只出乱码），对人声轨 0.96 → 分离是正常工作的。
- 之前那条抖音音频（纯口播、几乎无 BGM）测出 `corr=0.998`、伴奏相似度 0.97，是**内容特性**：没有独立伴奏可分离，模型给出的残差里仍有人声。不是配置问题。
- 顺手加质量提示：`separate.stem_similarity()`（用 av 解码，不引新依赖）在两轨相关系数 >0.9 时打印「这条音频可能本来就没有独立伴奏，伴奏里会有人声残留」。
- 耗时基准：20s 片段 21s（含转码），4 分 21 秒音频 170s，约 1.5× 实时。

### 10.71 分离前先试分，没伴奏直接跳过（2026-08-15 追加）

- `separate._has_companiment()`：从第 20 秒切 45 秒，用同一个模型试分一次，算 `stem_similarity()`；`corr > 0.9` 判定"没有独立伴奏"，直接返回 `{"skipped": True, "corr": ...}` 不跑全片。
- 切片段失败/试分出错时一律**保守地跑全片**，不会因为探测失败而丢掉分离。
- `separate()` 现在统一返回 dict（可含 `skipped`/`corr`），`fetch.separate_media()` 过滤出 `vocals/instrumental`，跳过时只打日志"（没有独立伴奏或未安装 audio-separator，跳过分离）"。
- 实测：
  - 纯口播 4分21秒：**31.3s 就跳过**（原来 170s），日志「试分结论：两轨相关 1.00，这条音频没有独立伴奏，跳过全片分离」；
  - 带 BGM 的 60 秒片段：试分 31.9s 得到 corr=0.035 → 继续跑全片，总耗时 78.6s，产出 vocals/instrumental 各 1.44MB。

### 10.72 「分离前不试分」开关（2026-08-15 追加）

- `Config.separate_probe`（默认 True，`V2I_SEPARATE_PROBE=0` 关闭）；`separate(probe=...)` 控制是否先试分；`fetch.separate_media` 透传。
- GUI 高级选项新增复选框「分离前不试分（直接跑全片，省约 30 秒）」+ 说明行「默认先切 45 秒试分…」；`/api/start` 与上传 query 透传 `separate_probe`；CLI 加 `--no-separate-probe`。
- 实测：默认 `separate_probe=True`，`V2I_SEPARATE_PROBE=0` 时为 False；复选框默认不勾、无 JS 异常。

