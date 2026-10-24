/* v2i Web GUI —— 无框架、无外部依赖 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const state = { status: null, jobId: null, running: false, run: null, file: null, src: "url" };

  const FILE_LABELS = {
    "out/summary.md": "摘要卡",
    "out/points.md": "观点清单",
    "out/article.md": "长文",
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

  const escapeHtml = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
      $("#queueHint").textContent = `提示：填好 ${llm.env_hint || "API key"} 后重启服务，提炼环节才能跑`;
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
      $("#runs").innerHTML = `<p class="empty">读取失败：${escapeHtml(err.message)}</p>`;
    }
  }

  function renderRuns(runs) {
    const box = $("#runs");
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
      const empty = $("#emptyState"); if (empty) empty.hidden = true;
      $("#resultTitle").textContent = s.title || dir;
      const bits = [];
      if (s.uploader) bits.push(s.uploader);
      if (s.duration) bits.push(s.duration);
      if (s.source) bits.push(s.source === "subtitle" ? "字幕直取" : "本地转写");
      if (s.elapsed) bits.push(`用时 ${fmtElapsed(s.elapsed)}`);
      if (s.tokens) bits.push(`${s.tokens} tokens`);
      $("#resultMeta").textContent = bits.join(" · ");

      const files = detail.files || [];
      const tabs = $("#tabs");
      tabs.innerHTML = files.map((f, idx) =>
        `<button class="tab${idx === 0 ? " active" : ""}" data-file="${escapeHtml(f)}" type="button">${escapeHtml(FILE_LABELS[f] || f)}</button>`
      ).join("");
      tabs.querySelectorAll(".tab").forEach((el) => {
        el.addEventListener("click", () => {
          tabs.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
          el.classList.add("active");
          showFile(dir, el.dataset.file);
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
    if (job.url) meta.push(`<span>${escapeHtml(job.url)}</span>`);
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
      prefer_subs: $("#preferSubs").checked,
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
        params.set("prefer_subs", options.prefer_subs ? "1" : "0");
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
  $("#refreshRuns").addEventListener("click", loadRuns);
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

  // #run=<目录名> 直达某次结果
  const m = /^#run=(.+)$/.exec(location.hash || "");
  if (m) openRun(decodeURIComponent(m[1]));
})();
