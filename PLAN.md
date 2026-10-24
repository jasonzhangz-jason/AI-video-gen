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

