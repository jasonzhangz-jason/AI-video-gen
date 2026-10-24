/* v2i Web GUI —— 无框架、无外部依赖 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const state = { status: null, jobId: null, running: false, run: null, file: null, src: "url", platforms: [] };

  const FILE_LABELS = {
    "out/summary.md": "摘要卡",
    "out/points.md": "观点清单",
    "out/article.md": "长文",
    "out/copy.md": "文案",
    "out/copy.txt": "文案（纯文本）",
    "transcript.md": "逐字稿",
    "transcript.srt": "字幕",
    "out/digest.json": "结构化数据",
    "meta.json": "元数据",
    "manifest.json": "运行记录",
  };

  // ---------------------------------------------------------------- 工具

  async function api(path, options) {
    const res = await fetch(path, options);
    const text = await res.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { error: text }; }
    if (!res.ok && data && data.error) throw new Error(data.error);
    return data;
  }

  const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const basename = (p) => String(p || "").split(/[\\/]/).filter(Boolean).pop() || "";

  function fmtElapsed(sec) {
    if (sec == null) return "";
    const n = Number(sec);
    if (!isFinite(n)) return "";
    return n < 60 ? `${n.toFixed(0)} 秒` : `${Math.floor(n / 60)} 分 ${Math.round(n % 60)} 秒`;
  }

  // ---------------------------------------------------------------- Markdown

  function splitFrontmatter(md) {
    const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(md);
    if (!m) return { meta: "", body: md };
    return { meta: m[1], body: md.slice(m[0].length) };
  }

  function inline(text) {
    let s = escapeHtml(text);
    s = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, t, u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`);
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    s = s.replace(/\[(\d{1,2}:\d{2}(?::\d{2})?)\]/g, '<span class="ts">[$1]</span>');
    return s;
  }

  function renderMarkdown(md) {
    const { meta, body } = splitFrontmatter(md || "");
    const lines = body.replace(/\r\n/g, "\n").split("\n");
    const html = [];
    let i = 0;

    const isBlank = (s) => !s.trim();
    const startsBlock = (s) =>
      /^(#{1,6}\s|>|\s*[-*]\s+|\s*\d+\.\s+|\||```|---|\*\*\*|<)/.test(s.trim());

    const parseTable = (start) => {
      const rows = [];
      let end = start;
      while (end < lines.length && /^\s*\|/.test(lines[end])) {
        rows.push(lines[end].trim());
        end += 1;
      }
      const cells = (row) => row.replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
      const head = cells(rows[0] || "");
      const bodyRows = rows.slice(2).map(cells);
      const thead = `<thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead>`;
      const tbody = bodyRows.length
        ? `<tbody>${bodyRows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody>`
        : "";
      return { html: `<table>${thead}${tbody}</table>`, end };
    };

    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) { i += 1; continue; }

      // 代码块
      if (/^\s*```/.test(line)) {
        const lang = line.trim().slice(3).trim();
        const buf = [];
        i += 1;
        while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
        i += 1;
        html.push(`<pre class="raw"${lang ? ` data-lang="${escapeHtml(lang)}"` : ""}>${escapeHtml(buf.join("\n"))}</pre>`);
        continue;
      }

      // 标题
      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        const level = heading[1].length;
        html.push(`<h${level}>${inline(heading[2].trim())}</h${level}>`);
        i += 1;
        continue;
      }

      // 分隔线
      if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { html.push("<hr>"); i += 1; continue; }

      // 引用
      if (/^\s*>/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*>\s?/, ""));
          i += 1;
        }
        html.push(`<blockquote>${buf.map((b) => (b.trim() ? `<p>${inline(b)}</p>` : "")).join("")}</blockquote>`);
        continue;
      }

      // 表格
      if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
        const table = parseTable(i);
        html.push(table.html);
        i = table.end;
        continue;
      }

      // 无序列表
      if (/^\s*[-*]\s+/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*[-*]\s+/, ""));
          i += 1;
        }
        html.push(`<ul>${buf.map((b) => `<li>${inline(b)}</li>`).join("")}</ul>`);
        continue;
      }

      // 有序列表
      if (/^\s*\d+\.\s+/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
          i += 1;
        }
        html.push(`<ol>${buf.map((b) => `<li>${inline(b)}</li>`).join("")}</ol>`);
        continue;
      }

      // 直接透传 HTML（<sub> 之类）
      if (/^\s*</.test(line)) { html.push(line); i += 1; continue; }

      // 段落
      const buf = [line];
      i += 1;
      while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines[i])) {
        buf.push(lines[i]);
        i += 1;
      }
      html.push(`<p>${inline(buf.join(" "))}</p>`);
    }

    const metaBlock = meta
      ? `<pre class="frontmatter">${escapeHtml(meta)}</pre>`
      : "";
    return metaBlock + html.join("\n");
  }

  // ---------------------------------------------------------------- 状态

  async function loadStatus() {
    try {
      const st = await api("/api/status");
      state.status = st;
      renderStatus(st);
    } catch (err) {
      $("#statusChips").innerHTML = `<span class="chip warn">无法连接服务：${escapeHtml(err.message)}</span>`;
    }
  }

  function renderStatus(st) {
    const chips = [];
    const llm = st.llm || {};
    if (llm.ready) {
      chips.push(`<span class="chip ok">模型 ${escapeHtml(llm.model || llm.provider)}</span>`);
    } else {
      chips.push(`<span class="chip warn">未配置 ${escapeHtml(llm.env_hint || "API key")}</span>`);
    }
    const asr = st.asr || {};
    chips.push(`<span class="chip">ASR ${escapeHtml(asr.model || "-")} · ${escapeHtml(asr.device || "-")}/${escapeHtml(asr.compute || "-")}</span>`);
    if (st.queue) chips.push(`<span class="chip warn">队列 ${st.queue}</span>`);
    chips.push(`<span class="chip muted">v${escapeHtml(st.version || "")}</span>`);
    $("#statusChips").innerHTML = chips.join("");
    $("#queueHint").textContent = "";
    $("#envLine").textContent = `项目 ${st.project} ｜ 产物 ${st.runs_dir}`;
    if (!llm.ready) {
      $("#queueHint").innerHTML = `提示：填好 ${escapeHtml(llm.env_hint || "API key")} 后就能提炼 · <a href="#" id="goSettings">去设置</a>`;
      const go = $("#goSettings"); if (go) go.addEventListener("click", (e) => { e.preventDefault(); switchPage("settings"); });
    }
    const hint = $("#defaultHint");
    if (hint) hint.textContent = `默认：字幕优先 · ${asr.model || "small"} · ${asr.compute || "int8"}`;
  }

  // ---------------------------------------------------------------- 历史

  async function loadRuns() {
    try {
      const runs = await api("/api/runs");
      renderRuns(runs);
    } catch (err) {
      const rbox = $("#runs");
      if (rbox) rbox.innerHTML = `<p class="empty">读取失败：${escapeHtml(err.message)}</p>`;
    }
  }

  function renderRuns(runs) {
    state.runs = runs || [];
    const box = $("#runs");
    if (!box) return;
    if (!runs.length) {
      box.innerHTML = `<p class="empty">还没有跑过任务</p>`;
      return;
    }
    box.innerHTML = runs.map((r) => {
      const invalid = Object.values(r.invalid || {}).reduce((n, arr) => n + (arr ? arr.length : 0), 0);
      const bits = [];
      if (r.duration) bits.push(r.duration);
      bits.push(r.outputs.length ? `${r.outputs.length} 份产物` : "无产物");
      if (r.tokens) bits.push(`${r.tokens} tok`);
      if (r.elapsed) bits.push(fmtElapsed(r.elapsed));
      bits.push(r.mtime_text);
      const active = state.run && state.run.dir === r.dir ? " active" : "";
      return `
        <button class="run-item${active}" data-dir="${escapeHtml(r.dir)}" type="button">
          <span class="t">${escapeHtml(r.title)}</span>
          <span class="m">${escapeHtml(bits.join(" · "))}${invalid ? `<span class="badge warn">${invalid} 处引用待核</span>` : ""}</span>
        </button>`;
    }).join("");
    box.querySelectorAll(".run-item").forEach((el) => {
      el.addEventListener("click", () => openRun(el.dataset.dir));
    });
  }

  // ---------------------------------------------------------------- 结果

  async function openRun(dir) {
    try {
      const detail = await api(`/api/run?dir=${encodeURIComponent(dir)}`);
      if (detail.error) throw new Error(detail.error);
      state.run = detail;
      const s = detail.summary || {};
      $("#resultCard").hidden = false;
      $("#resultTitle").textContent = s.title || dir;
      const bits = [];
      if (s.uploader) bits.push(s.uploader);
      if (s.duration) bits.push(s.duration);
      if (s.source) bits.push(s.source === "subtitle" ? "字幕直取" : "本地转写");
      if (s.elapsed) bits.push(`用时 ${fmtElapsed(s.elapsed)}`);
      if (s.tokens) bits.push(`${s.tokens} tokens`);
      if ((s.skipped || []).length) bits.push(`未提炼：${s.skipped.join(" / ")}`);
      $("#resultMeta").textContent = bits.join(" · ");
      const vbtn = $("#fetchVideoBtn");
      if (vbtn) {
        vbtn.hidden = !(s.url && !s.has_video);
        vbtn.dataset.dir = dir;
      }

      const files = detail.files || [];
      const tabs = $("#tabs");
      const FILE_TAB = "__files__";
      tabs.innerHTML = files.map((f, idx) =>
        `<button class="tab${idx === 0 ? " active" : ""}" data-file="${escapeHtml(f)}" type="button">${escapeHtml(FILE_LABELS[f] || f)}</button>`
      ).join("") + `<button class="tab" data-file="${FILE_TAB}" type="button">所有文件</button>`;
      tabs.querySelectorAll(".tab").forEach((el) => {
        el.addEventListener("click", () => {
          tabs.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
          el.classList.add("active");
          if (el.dataset.file === FILE_TAB) renderFiles(dir);
          else showFile(dir, el.dataset.file);
        });
      });

      if (files.length) showFile(dir, files[0]);
      else $("#doc").innerHTML = `<p class="empty">这个任务还没有产物（可能只跑了下载或转写）</p>`;

      document.querySelectorAll(".run-item").forEach((el) => {
        el.classList.toggle("active", el.dataset.dir === dir);
      });
      // 地址栏记下当前任务，刷新/分享都能回到同一份结果
      history.replaceState(null, "", `#run=${encodeURIComponent(dir)}`);
    } catch (err) {
      alert(`打开失败：${err.message}`);
    }
  }

  function fmtSize(bytes) {
    const n = Number(bytes || 0);
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  async function renderFiles(dir) {
    const doc = $("#doc");
    doc.innerHTML = `<p class="empty">读取文件列表…</p>`;
    try {
      const data = await api(`/api/files?dir=${encodeURIComponent(dir)}`);
      const rows = (data.files || []).map((f) => {
        const size = f.kind === "dir" ? (f.note || "") : fmtSize(f.size);
        const actions = [];
        if (f.kind !== "dir") {
          actions.push(`<a class="ghost link" href="/api/download?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(f.path)}">下载</a>`);
        }
        const badge = { text: "TXT", image: "IMG", audio: "AUDIO", video: "VIDEO", binary: "BIN", dir: "DIR" }[f.kind] || "FILE";
        const clickable = f.kind !== "dir" && f.kind !== "binary";
        return `<li class="file-row${clickable ? " clickable" : ""}" data-path="${escapeHtml(f.path)}">
          <span class="fname"><span class="fkind k-${escapeHtml(f.kind)}">${badge}</span>${escapeHtml(f.path)}</span>
          <span class="fmeta">${escapeHtml(size)}${f.mtime ? " · " + escapeHtml(f.mtime) : ""}</span>
          <span class="factions">${actions.join("")}</span>
        </li>`;
      }).join("");
      doc.innerHTML = `<p class="hint file-hint">点行内任意位置预览，右侧可直接下载（文本 / 图片 / 音视频都支持）</p>` + `<ul class="file-list">${rows}</ul>`;
      doc.querySelectorAll(".file-row").forEach((row) => {
        const target = (data.files || []).find((x) => x.path === row.dataset.path);
        if (target && target.kind !== "dir") row.addEventListener("click", () => openPreview(dir, target));
      });
    } catch (err) {
      doc.innerHTML = `<p class="empty">读取失败：${escapeHtml(err.message)}</p>`;
    }
  }

  let previewEl = null;

  function closePreview() {
    if (!previewEl) return;
    previewEl.remove();
    previewEl = null;
    document.removeEventListener("keydown", onPreviewKey);
  }

  function onPreviewKey(e) {
    if (e.key === "Escape") closePreview();
  }

  async function openPreview(dir, file) {
    closePreview();
    const overlay = document.createElement("div");
    overlay.className = "preview-overlay";
    overlay.innerHTML = `
      <div class="preview-box" role="dialog" aria-modal="true">
        <div class="preview-head">
          <span class="preview-name">${escapeHtml(file.path)}</span>
          <span class="preview-meta">${escapeHtml(fmtSize(file.size))}${file.mtime ? " · " + escapeHtml(file.mtime) : ""}</span>
          <a class="ghost link" href="/api/download?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(file.path)}">下载</a>
          <button class="ghost preview-close" type="button">关闭</button>
        </div>
        <div class="preview-body"><p class="empty">加载中…</p></div>
      </div>`;
    document.body.appendChild(overlay);
    previewEl = overlay;
    document.addEventListener("keydown", onPreviewKey);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closePreview(); });
    overlay.querySelector(".preview-close").addEventListener("click", closePreview);

    const body = overlay.querySelector(".preview-body");
    const url = `/api/preview?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(file.path)}`;
    try {
      if (file.kind === "image") {
        body.innerHTML = `<img class="preview-img" src="${url}" alt="${escapeHtml(file.path)}">`;
      } else if (file.kind === "audio") {
        body.innerHTML = `<div class="preview-media"><audio controls preload="metadata" src="${url}"></audio>
          <p class="empty">${escapeHtml(file.path)}</p></div>`;
      } else if (file.kind === "video") {
        body.innerHTML = `<div class="preview-media"><video controls preload="metadata" src="${url}"></video></div>`;
      } else if (file.kind === "text") {
        const res = await fetch(url);
        const text = await res.text();
        if (!res.ok) throw new Error(text);
        if (file.path.endsWith(".json")) {
          let pretty = text;
          try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch (e) { /* 原样 */ }
          body.innerHTML = `<pre class="raw">${escapeHtml(pretty)}</pre>`;
        } else if (file.path.endsWith(".srt") || file.path.endsWith(".vtt")) {
          body.innerHTML = `<pre class="raw">${escapeHtml(text)}</pre>`;
        } else {
          body.innerHTML = `<article class="doc">${renderMarkdown(text)}</article>`;
        }
      } else {
        body.innerHTML = `<p class="empty">这类文件不能内嵌预览，点右上角下载。</p>`;
      }
    } catch (err) {
      body.innerHTML = `<p class="empty">预览失败：${escapeHtml(err.message)}</p>`;
    }
  }

  async function fetchVideoFor(dir) {
    const btn = $("#fetchVideoBtn");
    if (btn) { btn.disabled = true; btn.textContent = "下载中…"; }
    try {
      const r = await api("/api/fetch_video", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dir }),
      });
      if (r.error) throw new Error(r.error);
      await new Promise((resolve, reject) => {
        const tick = async () => {
          try {
            const job = await api(`/api/job?id=${encodeURIComponent(r.job_id)}`);
            if (job.status === "done") return resolve();
            if (job.status === "error") return reject(new Error(job.error || "下载失败"));
            setTimeout(tick, 900);
          } catch (err) {
            reject(err);
          }
        };
        tick();
      });
      await openRun(dir);
      loadRuns();
    } catch (err) {
      alert("补下视频失败：" + err.message);
      if (btn) { btn.disabled = false; btn.textContent = "补下原视频"; }
    }
  }

  async function showFile(dir, name) {
    state.file = name;
    const doc = $("#doc");
    try {
      const res = await fetch(`/api/file?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(name)}`);
      const text = await res.text();
      if (!res.ok) throw new Error(text);
      if (name.endsWith(".json")) {
        let pretty = text;
        try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch (e) { /* 原样 */ }
        doc.innerHTML = `<pre class="raw">${escapeHtml(pretty)}</pre>`;
      } else if (name.endsWith(".srt")) {
        doc.innerHTML = `<pre class="raw">${escapeHtml(text)}</pre>`;
      } else {
        doc.innerHTML = renderMarkdown(text);
      }

      // 文案/逐字稿：一键复制全文
      if (name.endsWith(".txt") || name.endsWith(".md")) {
        const bar = document.createElement("div");
        bar.className = "doc-actions";
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ghost";
        btn.textContent = "复制全文";
        btn.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(text);
            btn.textContent = "已复制";
          } catch (e) {
            btn.textContent = "复制失败";
          }
          setTimeout(() => (btn.textContent = "复制全文"), 1500);
        });
        bar.appendChild(btn);
        doc.prepend(bar);
      }
    } catch (err) {
      doc.innerHTML = `<p class="empty">读取失败：${escapeHtml(err.message)}</p>`;
    }
  }

  // ---------------------------------------------------------------- 任务

  function setRunning(on) {
    state.running = on;
    const btn = $("#startBtn");
    btn.disabled = on;
    btn.textContent = on ? "运行中…" : "开始";
  }

  function shortUrl(value) {
    try {
      const u = new URL(value);
      return u.origin + u.pathname;
    } catch (e) {
      return String(value || "");
    }
  }

  function renderJob(job) {
    $("#jobCard").hidden = false;
    const stateEl = $("#jobState");
    stateEl.textContent = job.status === "error" ? "失败" : (job.stage || job.status);
    stateEl.className = `job-state ${job.status}`;

    const bar = $("#bar");
    const pct = Math.max(0, Math.min(100, job.progress || 0));
    if (job.status === "queued" || (job.status === "running" && !pct)) {
      bar.classList.add("idle");
      bar.style.width = "32%";
    } else {
      bar.classList.remove("idle");
      bar.style.width = `${pct}%`;
    }

    const meta = [];
    if (job.url) meta.push(`<span class="job-url" title="${escapeHtml(job.url)}">${escapeHtml(shortUrl(job.url))}</span>`);
    if (job.file) meta.push(`<span class="job-url" title="${escapeHtml(job.file)}">${escapeHtml(job.file)}</span>`);
    if (job.elapsed != null) meta.push(`<span>用时 ${fmtElapsed(job.elapsed)}</span>`);
    if (job.status === "error" && job.error) meta.push(`<span style="color:var(--bad)">${escapeHtml(job.error)}</span>`);
    $("#jobMeta").innerHTML = meta.join("");

    const logs = $("#logs");
    const nearBottom = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 40;
    logs.textContent = (job.logs || []).join("\n");
    if (nearBottom) logs.scrollTop = logs.scrollHeight;
  }

  async function pollJob(id) {
    try {
      const job = await api(`/api/job?id=${encodeURIComponent(id)}`);
      if (job.error) throw new Error(job.error);
      renderJob(job);
      if (job.status === "done" || job.status === "error") {
        setRunning(false);
        loadStatus();
        await loadRuns();
        if (job.status === "done" && job.workdir) await openRun(basename(job.workdir));
        return;
      }
    } catch (err) {
      setRunning(false);
      $("#jobState").textContent = "连接中断";
      $("#jobState").className = "job-state error";
      $("#jobMeta").innerHTML = `<span style="color:var(--bad)">${escapeHtml(err.message)}</span>`;
      return;
    }
    setTimeout(() => pollJob(id), 900);
  }

  function uploadFile(file, params, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/upload?${params}`);
      xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
      };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || "{}"); } catch (err) { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(data.error || `上传失败（HTTP ${xhr.status}）`));
      };
      xhr.onerror = () => reject(new Error("上传失败：连接中断"));
      xhr.send(file);
    });
  }

  async function startJob(event) {
    event.preventDefault();
    if (state.running) return;

    const modes = Array.from(document.querySelectorAll('input[name="mode"]:checked')).map((el) => el.value);
    if (!modes.length) {
      alert("至少选一种产物：摘要 / 观点 / 长文");
      return;
    }
    const isFile = state.src === "file";
    const url = $("#url").value.trim();
    if (!isFile && !url) { $("#url").focus(); return; }
    if (isFile && !state.file) { alert("先选一个音频或视频文件"); return; }

    const options = {
      modes,
      asr_model: $("#asrModel").value || undefined,
      language: $("#language").value || undefined,
      model: $("#model").value.trim() || undefined,
      copy_style: $("#copyStyle") ? $("#copyStyle").value : undefined,
      platform: $("#platform") ? $("#platform").value : undefined,
      cookies_from_browser: $("#cookiesFrom") && $("#cookiesFrom").value ? $("#cookiesFrom").value : undefined,
      proxy: $("#proxy") && $("#proxy").value.trim() ? $("#proxy").value.trim() : undefined,
      prefer_subs: $("#preferSubs").checked,
      keep_video: $("#keepVideo") ? $("#keepVideo").checked : false,
      force: $("#force").checked,
    };

    setRunning(true);
    $("#jobCard").hidden = false;
    $("#jobState").textContent = isFile ? "上传中…" : "提交中…";
    $("#jobState").className = "job-state";
    $("#logs").textContent = "";
    $("#jobMeta").innerHTML = "";
    $("#bar").style.width = "2%";

    try {
      let res;
      if (isFile) {
        const params = new URLSearchParams();
        params.set("modes", modes.join(","));
        if (options.asr_model) params.set("asr_model", options.asr_model);
        if (options.language) params.set("language", options.language);
        if (options.model) params.set("model", options.model);
        if (options.copy_style) params.set("copy_style", options.copy_style);
        if (options.cookies_from_browser) params.set("cookies_from_browser", options.cookies_from_browser);
        if (options.proxy) params.set("proxy", options.proxy);
        params.set("prefer_subs", options.prefer_subs ? "1" : "0");
        params.set("keep_video", options.keep_video ? "1" : "0");
        params.set("force", options.force ? "1" : "0");

        const bar = $("#bar");
        bar.classList.remove("idle");
        res = await uploadFile(state.file, params.toString(), (ratio) => {
          bar.style.width = `${Math.max(2, ratio * 18)}%`;
          $("#jobState").textContent = `上传中 ${(ratio * 100).toFixed(0)}%`;
        });
      } else {
        res = await api("/api/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, ...options }),
        });
      }
      if (res.error) throw new Error(res.error);
      state.jobId = res.job_id;
      pollJob(res.job_id);
    } catch (err) {
      setRunning(false);
      $("#jobState").textContent = "提交失败";
      $("#jobState").className = "job-state error";
      $("#jobMeta").innerHTML = `<span style="color:var(--bad)">${escapeHtml(err.message)}</span>`;
    }
  }

  // ---------------------------------------------------------------- 素材来源

  function platformNote(p) {
    if (!p || p.id === "auto") return "按链接域名自动匹配平台";
    const needs = (p.needs || []).map((n) => (n === "cookies" ? "需 cookies" : n === "proxy" ? "需代理" : n));
    return `${p.note}${needs.length ? `（${needs.join(" · ")}）` : ""}`;
  }

  function detectPlatform(url) {
    let host = "";
    try { host = new URL(url).hostname.toLowerCase(); } catch (e) { return null; }
    return state.platforms.find((p) => p.id !== "auto" && (p.hosts || []).some((h) => host === h || host.endsWith("." + h))) || null;
  }

  function renderPlatformNote() {
    const id = $("#platform").value;
    const p = state.platforms.find((x) => x.id === id);
    const out = $("#probeOut");
    if (!out) return;
    out.className = "probe-out";
    out.textContent = platformNote(p);
  }

  async function loadCookies() {
    const el = $("#cookieStatus");
    if (!el) return;
    try {
      const s = await api("/api/cookies");
      if (!s.present) {
        el.className = "cookie-status warn";
        el.textContent = "未上传 —— 抖音 / 小红书 / 快手 / X / YouTube 受限时再传";
      } else {
        el.className = "cookie-status ok";
        el.textContent = `已上传 · ${s.count} 条 cookies · ${s.mtime}${s.netscape ? "" : "（格式可能不对）"}`;
      }
    } catch (err) {
      el.className = "cookie-status";
      el.textContent = "读取失败：" + err.message;
    }
  }

  function uploadCookies(file) {
    const el = $("#cookieStatus");
    el.className = "cookie-status";
    el.textContent = "上传中…";
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/cookies");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) { /* ignore */ }
      if (data.error) {
        el.className = "cookie-status warn";
        el.textContent = "失败：" + data.error;
      } else {
        loadCookies();
      }
    };
    xhr.onerror = () => {
      el.className = "cookie-status warn";
      el.textContent = "上传失败：连接中断";
    };
    xhr.send(file);
  }

  async function clearCookies() {
    const el = $("#cookieStatus");
    try {
      await fetch("/api/cookies/clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      loadCookies();
    } catch (err) {
      el.className = "cookie-status warn";
      el.textContent = "清除失败：" + err.message;
    }
  }

  async function loadPlatforms() {
    try {
      const data = await api("/api/platforms");
      state.platforms = data.platforms || [];
      const select = $("#platform");
      select.innerHTML = state.platforms
        .map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.label)}</option>`)
        .join("");
      select.value = "auto";
      renderPlatformNote();
    } catch (err) {
      console.warn("平台列表加载失败", err);
    }
  }

  async function probeUrl() {
    const url = $("#url").value.trim();
    const out = $("#probeOut");
    if (!url) { out.className = "probe-out err"; out.textContent = "先贴一个链接"; return; }
    out.className = "probe-out";
    out.textContent = "探测中…";
    const params = new URLSearchParams({ url });
    params.set("platform", $("#platform").value || "auto");
    if ($("#cookiesFrom") && $("#cookiesFrom").value) params.set("cookies_from_browser", $("#cookiesFrom").value);
    if ($("#proxy") && $("#proxy").value.trim()) params.set("proxy", $("#proxy").value.trim());
    try {
      const r = await api(`/api/probe?${params.toString()}`);
      if (r.ok) {
        const subs = (r.subtitles || []).length ? `字幕 ${r.subtitles.slice(0, 3).join(" / ")}` : "无字幕 → 走转写";
        out.className = "probe-out ok";
        out.textContent = `✓ ${r.title} · ${r.duration} · ${subs} · ${r.formats} 个格式`;
      } else {
        out.className = "probe-out err";
        const action = r.action || "";
        let btn = "";
        if (action === "cookies_file" || action === "close_browser") {
          const label = action === "close_browser" ? "改用 cookies.txt" : "上传 cookies";
          btn = ` <button class="ghost mini" type="button" data-action="${action}">${label}</button>`;
        } else if (action === "proxy") {
          btn = ` <button class="ghost mini" type="button" data-action="proxy">去填代理</button>`;
        } else if (action === "url") {
          btn = ` <button class="ghost mini" type="button" data-action="url">支持哪种链接？</button>`;
        }
        out.innerHTML = `✗ ${escapeHtml(r.error || "探测失败")}${btn}`;
        out.querySelectorAll("[data-action]").forEach((b) => {
          b.addEventListener("click", () => runProbeAction(b.dataset.action));
        });
      }
    } catch (err) {
      out.className = "probe-out err";
      out.textContent = `✗ ${err.message}`;
    }
  }

  function runProbeAction(action) {
    const out = $("#probeOut");
    if (action === "proxy") {
      const adv = document.querySelector(".advanced");
      if (adv) adv.open = true;
      const el = $("#proxy");
      if (el) {
        el.focus();
        el.scrollIntoView({ block: "center", behavior: "smooth" });
      }
      return;
    }
    if (action === "url") {
      out.className = "probe-out";
      out.innerHTML = "抖音：<code>https://www.douyin.com/video/&lt;id&gt;</code> 或 <code>v.douyin.com</code> 短链（<b>/shipin/ 不支持</b>）；小红书：<code>xhslink.com</code> 短链或笔记页。";
      return;
    }
    // cookies 类
    const adv = document.querySelector(".advanced");
    if (adv) adv.open = true;
    const row = document.querySelector(".cookie-row");
    if (row) {
      row.scrollIntoView({ block: "center", behavior: "smooth" });
      row.classList.add("flash");
      setTimeout(() => row.classList.remove("flash"), 1600);
    }
    const input = $("#cookieFile");
    if (input) input.click();
  }

  // ---------------------------------------------------------------- 页面切换 / 文案改写

  function switchPage(name) {
    const page = ["new", "history", "rewrite", "settings"].includes(name) ? name : "new";
    document.querySelectorAll(".nav-item").forEach((b) => {
      b.classList.toggle("active", b.dataset.page === page);
    });
    [["new", "#page-new"], ["history", "#page-history"], ["rewrite", "#page-rewrite"], ["settings", "#page-settings"]].forEach(([key, sel]) => {
      const el = $(sel);
      if (el) el.hidden = key !== page;
    });
    if (page === "history") {
      mountResultCard($("#histDetailSlot"));
      renderHistoryPage();
    } else {
      unmountResultCard();
    }
    if (page === "new") {
      // 回到新建任务时保持"干净"：只有本会话真的跑过任务才展示结果卡，
      // 浏览历史任务留下的详情不带过来（避免看起来像新建页默认在展示历史）
      if (!state.jobId) {
        const card = $("#resultCard");
        if (card) card.hidden = true;
      }
      if ((location.hash || "").startsWith("#page=")) history.replaceState(null, "", "#page=new");
    } else {
      history.replaceState(null, "", `#page=${page}`);
    }
    if (page === "rewrite") {
      fillImportOptions();
      loadRewriteHistory();
    }
    if (page === "settings") loadSettings();
  }

  function toggleHowto(force) {
    const pop = $("#howtoPop");
    const btn = $("#howtoBtn");
    if (!pop || !btn) return;
    const show = force === undefined ? pop.hidden : !!force;
    pop.hidden = !show;
    btn.setAttribute("aria-expanded", show ? "true" : "false");
    btn.classList.toggle("active", show);
  }

  async function loadSettings() {
    try {
      const s = await api("/api/settings");
      $("#setPath").textContent = s.path;
      const eff = s.effective || {};
      const asr = eff.asr || {};
      const providers = ["deepseek", "openai", "moonshot", "siliconflow", "ollama"];
      $("#setProvider").value = providers.includes(eff.provider) ? eff.provider : "deepseek";
      $("#setModel").value = eff.model || "";
      $("#setBaseUrl").value = eff.base_url || "";
      $("#setProxy").value = eff.proxy || "";
      $("#setAsrModel").value = asr.model || "";
      $("#setAsrDevice").value = asr.device_setting || "";
      $("#setAsrCompute").value = asr.compute_setting || "";
      $("#setLanguage").value = asr.language || "";
      $("#setChunk").value = eff.chunk_chars || 1600;
      $("#setCopyStyle").value = eff.copy_style || "";
      $("#setKey").value = "";
      const hint = $("#setKeyHint");
      if (eff.api_key) {
        hint.className = "hint ok";
        hint.textContent = `已配置：${eff.api_key}（留空表示不修改）`;
      } else {
        hint.className = "hint warn";
        hint.textContent = "当前没有可用的 key：转写能跑，提炼和改写会用不了。";
      }
      const rows = [
        ["大模型", `${eff.provider} / ${eff.model}`],
        ["Base URL", eff.base_url],
        ["API Key", eff.api_key || "（未配置）"],
        ["状态", eff.ready ? "可用" : "缺 key"],
        ["转写", `${asr.model} · ${asr.device}/${asr.compute}${asr.language ? " · " + asr.language : " · 自动语言"}`],
        ["分块", `${eff.chunk_chars} 字`],
        ["文案风格", { raw: "原样", light: "轻度整理", script: "口播稿" }[eff.copy_style] || eff.copy_style],
        ["代理", eff.proxy || "（未设置）"],
        ["项目", eff.project],
        ["产物目录", eff.runs_dir],
      ];
      $("#setEffective").innerHTML = rows.map(([k, v]) =>
        `<li class="file-row"><span class="fname">${escapeHtml(k)}</span><span class="fmeta">${escapeHtml(String(v == null ? "" : v))}</span></li>`
      ).join("");
    } catch (err) {
      $("#setHint").textContent = "读取失败：" + err.message;
    }
  }

  async function saveSettings() {
    const patch = {
      V2I_PROVIDER: $("#setProvider").value,
      V2I_MODEL: $("#setModel").value.trim(),
      V2I_BASE_URL: $("#setBaseUrl").value.trim(),
      V2I_PROXY: $("#setProxy").value.trim(),
      V2I_ASR_MODEL: $("#setAsrModel").value,
      V2I_ASR_DEVICE: $("#setAsrDevice").value,
      V2I_ASR_COMPUTE: $("#setAsrCompute").value,
      V2I_LANGUAGE: $("#setLanguage").value,
      V2I_CHUNK_CHARS: $("#setChunk").value.trim(),
      V2I_COPY_STYLE: $("#setCopyStyle").value,
    };
    const key = $("#setKey").value.trim();
    if (key) patch.V2I_API_KEY = key;
    const hint = $("#setHint");
    hint.className = "hint";
    hint.textContent = "保存中…";
    try {
      const s = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ patch }),
      });
      if (s.error) throw new Error(s.error);
      hint.className = "hint ok";
      hint.textContent = "已保存并生效（无需重启）";
      await loadSettings();
      await loadStatus();
    } catch (err) {
      hint.className = "hint err";
      hint.textContent = err.message;
    }
  }

  async function testConnection() {
    const hint = $("#setHint");
    hint.className = "hint";
    hint.textContent = "测试中…";
    try {
      const r = await api("/api/settings/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (r.ok) {
        hint.className = "hint ok";
        hint.textContent = `连通 ✓ ${r.reply} · ${r.model}`;
      } else {
        hint.className = "hint err";
        hint.textContent = r.error;
      }
    } catch (err) {
      hint.className = "hint err";
      hint.textContent = err.message;
    }
  }

  async function clearKey() {
    if (!confirm("清除运行时保存的 API key？（会回退到 .env / 环境变量）")) return;
    const hint = $("#setHint");
    try {
      await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ patch: { V2I_API_KEY: "" } }),
      });
      hint.className = "hint";
      hint.textContent = "已清除";
      await loadSettings();
      await loadStatus();
    } catch (err) {
      hint.className = "hint err";
      hint.textContent = err.message;
    }
  }

  function fillImportOptions() {
    const select = $("#rwImport");
    if (!select) return;
    const runs = state.runs || [];
    const current = select.value;
    select.innerHTML = '<option value="">选择一条任务…</option>' + runs
      .filter((r) => r.has_transcript)
      .map((r) => `<option value="${escapeHtml(r.dir)}">${escapeHtml(String(r.title).slice(0, 26))}</option>`)
      .join("");
    if (current) select.value = current;
  }

  function updateCharCount() {
    const el = $("#rwInput");
    if (el && $("#rwCount")) $("#rwCount").textContent = `${el.value.length} 字`;
  }

  async function importTranscript(dir) {
    if (!dir) return;
    const hint = $("#rwHint");
    hint.className = "hint";
    hint.textContent = "读取逐字稿…";
    try {
      const res = await fetch(`/api/file?dir=${encodeURIComponent(dir)}&name=transcript.md`);
      const md = await res.text();
      if (!res.ok) throw new Error(md);
      const lines = md.split("\n")
        .filter((l) => l.trim() && !l.startsWith("# ") && !l.startsWith("- "))
        .map((l) => l.replace(/^\*\*\[\d{1,2}:\d{2}(?::\d{2})?\]\*\*\s*/, "").trim())
        .filter(Boolean);
      $("#rwInput").value = lines.join("\n\n");
      updateCharCount();
      hint.textContent = `已导入 ${lines.length} 段`;
    } catch (err) {
      hint.className = "hint err";
      hint.textContent = "导入失败：" + err.message;
    }
  }

  function setRewriteView(view) {
    document.querySelectorAll("#rwView .seg").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === view);
    });
    $("#rwOut").hidden = view !== "out";
    $("#rwDiff").hidden = view !== "diff";
    const sbs = $("#rwSbs");
    if (sbs) sbs.hidden = view !== "sbs";
    if (view !== "out") renderDiff();
  }

  function paintDiff(d) {
    const box = $("#rwDiff");
    const stats = $("#rwDiffStats");
    if (stats && d.stats) {
      stats.textContent = `相似度 ${Math.round(d.stats.ratio * 100)}% · 删 ${d.stats.removed} 字 / 增 ${d.stats.added} 字`;
    }
    const rows = [];
    for (const op of d.ops || []) {
      if (op.tag === "equal") {
        if (op.a) rows.push(`<p class="d-eq">${escapeHtml(op.a)}</p>`);
      } else if (op.tag === "delete") {
        rows.push(`<p class="d-del"><span class="mark">−</span>${escapeHtml(op.a)}</p>`);
      } else if (op.tag === "insert") {
        rows.push(`<p class="d-ins"><span class="mark">+</span>${escapeHtml(op.b)}</p>`);
      } else if (op.inline && op.inline.length) {
        const parts = op.inline.map((seg) => {
          if (seg.tag === "equal") return escapeHtml(seg.a);
          if (seg.tag === "delete") return `<del>${escapeHtml(seg.a)}</del>`;
          if (seg.tag === "insert") return `<ins>${escapeHtml(seg.b)}</ins>`;
          return `<del>${escapeHtml(seg.a)}</del><ins>${escapeHtml(seg.b)}</ins>`;
        }).join("");
        rows.push(`<p class="d-rep">${parts}</p>`);
      } else {
        rows.push(`<p class="d-del"><span class="mark">−</span>${escapeHtml(op.a)}</p>`);
        rows.push(`<p class="d-ins"><span class="mark">+</span>${escapeHtml(op.b)}</p>`);
      }
    }
    box.innerHTML = rows.length ? rows.join("") : '<p class="d-none">两边完全一样。</p>';
  }

  function paintSbs(d) {
    const box = $("#rwSbs");
    if (!box) return;
    const blocked = (html) => `<p class="d-none">${html}</p>`;
    const cells = [];
    for (const op of d.ops || []) {
      let left = "";
      let right = "";
      let leftCls = "left";
      let rightCls = "right";
      if (op.tag === "equal") {
        left = escapeHtml(op.a);
        right = escapeHtml(op.a);
      } else if (op.tag === "delete") {
        left = escapeHtml(op.a);
        leftCls += " del";
      } else if (op.tag === "insert") {
        right = escapeHtml(op.b);
        rightCls += " ins";
      } else if (op.inline && op.inline.length) {
        const leftParts = [];
        const rightParts = [];
        for (const seg of op.inline) {
          if (seg.tag === "equal") {
            leftParts.push(escapeHtml(seg.a));
            rightParts.push(escapeHtml(seg.a));
          } else if (seg.tag === "delete") {
            leftParts.push(`<del>${escapeHtml(seg.a)}</del>`);
          } else if (seg.tag === "insert") {
            rightParts.push(`<ins>${escapeHtml(seg.b)}</ins>`);
          } else {
            leftParts.push(`<del>${escapeHtml(seg.a)}</del>`);
            rightParts.push(`<ins>${escapeHtml(seg.b)}</ins>`);
          }
        }
        left = leftParts.join("");
        right = rightParts.join("");
        leftCls += " del-soft";
        rightCls += " ins-soft";
      } else {
        left = escapeHtml(op.a);
        right = escapeHtml(op.b);
        leftCls += " del";
        rightCls += " ins";
      }
      cells.push(`<div class="sbs-cell ${leftCls}">${left}</div>`);
      cells.push(`<div class="sbs-cell ${rightCls}">${right}</div>`);
    }
    if (!cells.length) {
      box.innerHTML = blocked("两边完全一样。");
      return;
    }
    box.innerHTML =
      '<div class="sbs-head"><span>原文</span><span>改写后</span></div>' +
      `<div class="sbs-grid">${cells.join("")}</div>`;
  }

  async function renderDiff() {
    const box = $("#rwDiff");
    if (!state.lastRewrite) {
      box.innerHTML = '<p class="d-none">先点一次「一键改写」，再来对比。</p>';
      return;
    }
    if (state.diff) {
      paintDiff(state.diff);
      paintSbs(state.diff);
      return;
    }
    box.innerHTML = '<p class="d-none">对比中…</p>';
    try {
      const d = await api("/api/diff", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ a: state.lastInput || "", b: state.lastRewrite }),
      });
      if (d.error) throw new Error(d.error);
      state.diff = d;
      paintDiff(d);
      paintSbs(d);
    } catch (err) {
      box.innerHTML = `<p class="d-none">对比失败：${escapeHtml(err.message)}</p>`;
    }
  }

  const STYLE_LABEL = { raw: "原样", light: "轻度整理", script: "口播稿" };

  function mountResultCard(host) {
    const card = $("#resultCard");
    if (!card || !host) return;
    if (!state.cardHome) {
      state.cardHome = { parent: card.parentElement, next: card.nextElementSibling };
    }
    if (card.parentElement !== host) host.appendChild(card);
  }

  function unmountResultCard() {
    const card = $("#resultCard");
    if (!card || !state.cardHome) return;
    const { parent, next } = state.cardHome;
    if (!parent || card.parentElement === parent) return;
    const anchor = next && next.parentElement === parent ? next : null;
    parent.insertBefore(card, anchor);
  }

  const PAGE_SIZES = [5, 10, 20, 50];

  async function renderHistoryPage() {
    const box = $("#histList");
    if (!box) return;
    if (!state.runs) {
      box.innerHTML = '<li class="hist-empty">读取中…</li>';
      await loadRuns();
    }
    const runs = state.runs || [];
    const q = ($("#histSearch").value || "").trim().toLowerCase();
    const videoOnly = $("#histVideoOnly").checked;
    const shown = runs.filter((r) => {
      if (videoOnly && !r.has_video) return false;
      if (!q) return true;
      return `${r.title || ""} ${r.uploader || ""}`.toLowerCase().includes(q);
    });
    const size = state.histSize || 10;
    const pages = Math.max(1, Math.ceil(shown.length / size));
    state.histPage = Math.min(state.histPage || 1, pages);
    const start = (state.histPage - 1) * size;
    const pageRows = shown.slice(start, start + size);

    $("#histCount").textContent = runs.length
      ? `共 ${runs.length} 条${shown.length !== runs.length ? `，命中 ${shown.length} 条` : ""}`
      : "还没有任务";
    if (!shown.length) {
      box.innerHTML = `<li class="hist-empty">${runs.length ? "没有匹配的任务" : "跑完第一个任务后，这里会按时间倒序列出全部记录。"}</li>`;
      renderPager(0, 1, size);
      return;
    }
    box.innerHTML = pageRows.map((r) => {
      const bits = [];
      if (r.duration) bits.push(r.duration);
      if (r.source) bits.push(r.source === "subtitle" ? "字幕" : r.source === "video" ? "视频转写" : "音频转写");
      bits.push(`${(r.outputs || []).length} 份产物`);
      if (r.elapsed) bits.push(fmtElapsed(r.elapsed));
      if (r.tokens) bits.push(`${r.tokens} tokens`);
      return `
        <li class="hist-row${r.dir === (state.run || {}).dir ? " active" : ""}" data-dir="${escapeHtml(r.dir)}">
          <div class="hist-main">
            <strong>${escapeHtml(r.title || r.dir)}</strong>
            <small>${escapeHtml(bits.join(" · "))}${r.created ? " · " + escapeHtml(r.created) : ""}</small>
          </div>
          <div class="hist-badges">
            ${r.has_video ? '<span class="badge video">VIDEO</span>' : ""}
            ${(r.skipped || []).length ? '<span class="badge warn">部分跳过</span>' : ""}
            <button class="hist-del" data-dir="${escapeHtml(r.dir)}" type="button" title="删除这条任务">删除</button>
            <span class="hist-go">查看 ›</span>
          </div>
        </li>`;
    }).join("");
    box.querySelectorAll(".hist-row").forEach((row) => {
      row.addEventListener("click", () => openHistoryRun(row.dataset.dir));
    });
    box.querySelectorAll(".hist-del").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteRun(btn.dataset.dir);
      });
    });
    renderPager(shown.length, pages, size);
  }

  function renderPager(total, pages, size) {
    const pager = $("#histPager");
    if (!pager) return;
    if (total <= size) {
      pager.innerHTML = "";
      return;
    }
    const cur = state.histPage || 1;
    const nums = [];
    const from = Math.max(1, cur - 2);
    const to = Math.min(pages, cur + 2);
    for (let n = from; n <= to; n += 1) nums.push(n);
    pager.innerHTML = `
      <button class="ghost" data-page="${cur - 1}" ${cur <= 1 ? "disabled" : ""} type="button">上一页</button>
      ${from > 1 ? '<span class="pager-dots">…</span>' : ""}
      ${nums.map((n) => `<button class="pager-num${n === cur ? " active" : ""}" data-page="${n}" type="button">${n}</button>`).join("")}
      ${to < pages ? '<span class="pager-dots">…</span>' : ""}
      <button class="ghost" data-page="${cur + 1}" ${cur >= pages ? "disabled" : ""} type="button">下一页</button>
      <span class="pager-info">第 ${cur} / ${pages} 页 · 每页 ${size} 条</span>`;
    pager.querySelectorAll("button[data-page]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = Math.min(Math.max(1, Number(btn.dataset.page)), pages);
        state.histPage = target;
        renderHistoryPage();
      });
    });
  }

  async function deleteRun(dir) {
    const name = (dir || "").split(/[\\/]/).pop();
    if (!confirm(`删除任务「${name}」？\n\n文件会被移到项目下的 .trash 目录，不会立刻消失。`)) return;
    try {
      const r = await api("/api/run/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dir }),
      });
      if (r.error) throw new Error(r.error);
      if ((state.run || {}).dir === dir) {
        state.run = null;
        const card = $("#resultCard");
        if (card) card.hidden = true;
      }
      await loadRuns();
      await renderHistoryPage();
    } catch (err) {
      alert("删除失败：" + err.message);
    }
  }

  async function openHistoryRun(dir) {
    document.querySelectorAll("#histList .hist-row").forEach((r) => {
      r.classList.toggle("active", r.dataset.dir === dir);
    });
    await openRun(dir);
    const card = $("#resultCard");
    if (card && !card.hidden) card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function loadRewriteHistory() {
    const box = $("#rwHistory");
    if (!box) return;
    try {
      const data = await api("/api/rewrite/history");
      const items = data.items || [];
      $("#rwHistoryCount").textContent = items.length ? `最近 ${items.length} 条` : "还没有记录";
      if (!items.length) {
        box.innerHTML = '<li class="empty" style="color:var(--muted);font-size:13px">改写过一次之后，这里会留下记录，方便回看和复用。</li>';
        return;
      }
      box.innerHTML = items.map((it) => `
        <li class="file-row rw-hist" data-id="${escapeHtml(it.id)}">
          <span class="fname">${escapeHtml(it.at || "")} · ${escapeHtml(STYLE_LABEL[it.style] || it.style || "")} · ${it.in_chars}→${it.out_chars} 字</span>
          <span class="fmeta rw-preview">${escapeHtml(it.preview || "")}</span>
          <span class="factions">
            <button class="ghost" data-act="view" type="button">查看</button>
            <button class="ghost" data-act="edit" type="button">再编辑</button>
            <button class="ghost" data-act="del" type="button">删除</button>
          </span>
        </li>`).join("");
      box.querySelectorAll(".rw-hist").forEach((row) => {
        const id = row.dataset.id;
        row.querySelector('[data-act="view"]').addEventListener("click", (e) => { e.stopPropagation(); viewHistory(id); });
        row.querySelector('[data-act="edit"]').addEventListener("click", (e) => { e.stopPropagation(); editHistory(id); });
        row.querySelector('[data-act="del"]').addEventListener("click", (e) => { e.stopPropagation(); deleteHistory(id); });
      });
    } catch (err) {
      box.innerHTML = `<li class="empty" style="color:var(--bad);font-size:13px">读取失败：${escapeHtml(err.message)}</li>`;
    }
  }

  async function fetchHistoryItem(id) {
    const item = await api(`/api/rewrite/history?id=${encodeURIComponent(id)}`);
    if (item.error) throw new Error(item.error);
    return item;
  }

  async function viewHistory(id) {
    const hint = $("#rwHint");
    try {
      const item = await fetchHistoryItem(id);
      $("#rwOutCard").hidden = false;
      $("#rwOut").innerHTML = String(item.output || "")
        .split(/\n{2,}/)
        .map((p) => `<p>${escapeHtml(p)}</p>`)
        .join("");
      $("#rwMeta").textContent = `历史 · ${item.at} · ${item.in_chars} 字 → ${item.out_chars} 字 · ${item.model}`;
      state.lastInput = item.input || "";
      state.lastRewrite = item.output || "";
      state.diff = null;
      $("#rwDiffStats").textContent = "";
      setRewriteView("out");
      $("#rwOutCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
      hint.textContent = "";
    } catch (err) {
      hint.className = "hint err";
      hint.textContent = "读取失败：" + err.message;
    }
  }

  async function editHistory(id) {
    try {
      const item = await fetchHistoryItem(id);
      $("#rwInput").value = item.input || "";
      updateCharCount();
      $("#rwHint").textContent = "已载入原始输入，可直接再改一次";
      $("#rwInput").scrollIntoView({ behavior: "smooth", block: "center" });
    } catch (err) {
      $("#rwHint").className = "hint err";
      $("#rwHint").textContent = "读取失败：" + err.message;
    }
  }

  async function deleteHistory(id) {
    if (!confirm("删除这条改写记录？")) return;
    try {
      await api("/api/rewrite/history/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      loadRewriteHistory();
    } catch (err) {
      $("#rwHint").className = "hint err";
      $("#rwHint").textContent = "删除失败：" + err.message;
    }
  }

  async function doRewrite() {
    const text = $("#rwInput").value.trim();
    const hint = $("#rwHint");
    if (text.length < 10) {
      hint.className = "hint warn";
      hint.textContent = "至少 10 个字";
      return;
    }
    $("#rwBtn").disabled = true;
    $("#rwBtn").textContent = "改写中…";
    hint.className = "hint";
    hint.textContent = "调用模型…";
    try {
      const r = await api("/api/rewrite", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, style: $("#rwStyle").value }),
      });
      if (r.error) throw new Error(r.error);
      const card = $("#rwOutCard");
      card.hidden = false;
      $("#rwOut").innerHTML = String(r.text || "")
        .split(/\n{2,}/)
        .map((p) => `<p>${escapeHtml(p)}</p>`)
        .join("");
      $("#rwMeta").textContent = `${r.in_chars} 字 → ${r.chars} 字 · ${r.tokens} tokens · ${r.model}`;
      state.lastRewrite = r.text;
      state.lastInput = text;
      state.diff = null;
      $("#rwDiffStats").textContent = "";
      setRewriteView("out");
      hint.textContent = "完成";
      loadRewriteHistory();
      card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) {
      hint.className = "hint err";
      hint.textContent = err.message;
    } finally {
      $("#rwBtn").disabled = false;
      $("#rwBtn").textContent = "一键改写";
    }
  }

  function setSource(src) {
    state.src = src;
    document.querySelectorAll("#srcSwitch .seg").forEach((el) => {
      el.classList.toggle("active", el.dataset.src === src);
    });
    $("#urlRow").hidden = src !== "url";
    $("#fileRow").hidden = src !== "file";
    if (src === "url") $("#url").focus();
  }

  function selectFile(file) {
    state.file = file || null;
    const label = $("#fileName");
    if (!file) {
      label.textContent = "也可把音频 / 视频直接拖进来（mp3 / m4a / wav / mp4 / mkv，最大 4GB）";
      label.classList.remove("picked");
      return;
    }
    label.textContent = `${file.name} · ${(file.size / 1048576).toFixed(1)} MB`;
    label.classList.add("picked");
  }

  // ---------------------------------------------------------------- 启动

  $("#runForm").addEventListener("submit", startJob);
  const refreshRuns = $("#refreshRuns");
  if (refreshRuns) refreshRuns.addEventListener("click", loadRuns);
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") $("#runForm").requestSubmit();
  });
  document.querySelectorAll(".modes input").forEach((el) => {
    el.addEventListener("change", () => {
      const any = document.querySelectorAll('input[name="mode"]:checked').length;
      if (!any) el.checked = true;
    });
  });

  document.querySelectorAll("#srcSwitch .seg").forEach((el) => {
    el.addEventListener("click", () => setSource(el.dataset.src));
  });
  $("#filePick").addEventListener("click", (e) => {
    if (e.target.id !== "fileInput") $("#fileInput").click();
  });
  $("#fileInput").addEventListener("change", (e) => selectFile(e.target.files[0]));
  document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => switchPage(b.dataset.page)));
  $("#setSave").addEventListener("click", saveSettings);
  $("#setTest").addEventListener("click", testConnection);
  $("#setClear").addEventListener("click", clearKey);
  $("#rwInput").addEventListener("input", updateCharCount);
  $("#rwBtn").addEventListener("click", doRewrite);
  $("#rwClear").addEventListener("click", () => { $("#rwInput").value = ""; updateCharCount(); $("#rwHint").textContent = ""; });
  $("#rwImport").addEventListener("change", (e) => importTranscript(e.target.value));
  document.querySelectorAll("#rwView .seg").forEach((b) => b.addEventListener("click", () => setRewriteView(b.dataset.view)));
  $("#rwCopy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(state.lastRewrite || ""); $("#rwHint").textContent = "已复制"; }
    catch (e) { $("#rwHint").textContent = "复制失败"; }
  });
  ["histSearch", "histVideoOnly"].forEach((id) => {
    const el = $("#" + id);
    if (el) el.addEventListener("input", () => { state.histPage = 1; renderHistoryPage(); });
  });
  const sizeSel = $("#histSize");
  if (sizeSel) sizeSel.addEventListener("change", () => {
    state.histSize = Number(sizeSel.value) || 10;
    state.histPage = 1;
    renderHistoryPage();
  });
  $("#histRefresh").addEventListener("click", async () => { await loadRuns(); renderHistoryPage(); });
  $("#howtoBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleHowto(); });
  document.addEventListener("click", (e) => {
    const pop = $("#howtoPop");
    if (!pop || pop.hidden) return;
    if (!e.target.closest(".tips-wrap")) toggleHowto(false);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggleHowto(false); });
  $("#fetchVideoBtn").addEventListener("click", () => {
    const dir = $("#fetchVideoBtn").dataset.dir;
    if (dir) fetchVideoFor(dir);
  });
  $("#probeBtn").addEventListener("click", probeUrl);
  $("#cookiePick").addEventListener("click", () => $("#cookieFile").click());
  $("#cookieFile").addEventListener("change", (e) => { if (e.target.files[0]) uploadCookies(e.target.files[0]); e.target.value = ""; });
  $("#cookieClear").addEventListener("click", () => { if (confirm("清除已上传的 cookies？")) clearCookies(); });
  $("#platform").addEventListener("change", renderPlatformNote);
  $("#url").addEventListener("input", () => {
    if ($("#platform").value !== "auto") return;
    const found = detectPlatform($("#url").value.trim());
    if (found) {
      const out = $("#probeOut");
      out.className = "probe-out";
      out.textContent = `识别为 ${found.label}：${platformNote(found)}`;
    }
  });

  ["dragenter", "dragover"].forEach((name) => {
    document.addEventListener(name, (e) => {
      if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes("Files")) return;
      e.preventDefault();
      document.body.classList.add("dragging");
      setSource("file");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    document.addEventListener(name, (e) => {
      if (name === "dragleave" && e.relatedTarget) return;
      document.body.classList.remove("dragging");
      if (name !== "drop") return;
      const files = e.dataTransfer && e.dataTransfer.files;
      if (!files || !files.length) return;
      e.preventDefault();
      selectFile(files[0]);
    });
  });

  loadStatus().then(() => {
    // 刷新页面时若还有任务在跑，自动接上进度
    const active = state.status && state.status.active_job;
    if (active && !state.running) {
      setRunning(true);
      $("#jobCard").hidden = false;
      state.jobId = active;
      pollJob(active);
    }
  });
  loadRuns();
  loadPlatforms();
  loadCookies();

  // #page=rewrite 直达文案改写
  if ((location.hash || "").startsWith("#page=rewrite")) switchPage("rewrite");
  if ((location.hash || "").startsWith("#page=settings")) switchPage("settings");

  // #run=<目录名> 直达某次结果
  const m = /^#run=(.+)$/.exec(location.hash || "");
  if (m) openRun(decodeURIComponent(m[1]));
})();
