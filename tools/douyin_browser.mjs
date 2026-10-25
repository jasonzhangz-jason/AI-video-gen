// 用本机 Chrome（无头 + CDP）打开抖音视频页，从页面自己发出的
// /aweme/v1/web/aweme/detail/ 响应里取出真实播放地址（a_bogus 由页面自己算）。
// 用法：node tools/douyin_browser.mjs <video-url> [timeoutMs]
// 输出 JSON：{ ok, desc, playUrls: [...], media: [...] }
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const url = process.argv[2];
const timeoutMs = Number(process.argv[3] || 30000);
const port = 9333 + Math.floor(Math.random() * 80);

if (!url) {
  console.log(JSON.stringify({ ok: false, error: "缺少 url" }));
  process.exit(2);
}

const profile = mkdtempSync(join(tmpdir(), "v2i-cdp-"));
const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--mute-audio", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--window-size=1280,800", "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getJSON(path) {
  for (let i = 0; i < 60; i += 1) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}${path}`);
      if (res.ok) return await res.json();
    } catch { /* chrome 还没起来 */ }
    await sleep(250);
  }
  throw new Error("CDP 端口没起来");
}

const API_RE = /aweme\/v1\/web\/aweme\/detail/i;
const MEDIA_RE = /douyinvod\.com|aweme\/v1\/play|\.mp4(\?|$)/i;

const bodies = [];
const media = new Set();
let ws;

try {
  const targets = await getJSON("/json/list");
  const pageTarget = targets.find((t) => t.type === "page");
  if (!pageTarget) throw new Error("没有可用的 page target");
  ws = new WebSocket(pageTarget.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });

  let id = 0;
  const pending = new Map();
  const send = (method, params = {}) => new Promise((resolve) => {
    const mid = ++id;
    pending.set(mid, resolve);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg.result);
      pending.delete(msg.id);
      return;
    }
    if (msg.method === "Network.requestWillBeSent") {
      const u = msg.params?.request?.url || "";
      if (MEDIA_RE.test(u)) media.add(u);
    }
    if (msg.method === "Network.responseReceived") {
      const u = msg.params?.response?.url || "";
      if (API_RE.test(u)) {
        const requestId = msg.params.requestId;
        send("Network.getResponseBody", { requestId }).then((res) => {
          if (res?.body) bodies.push(res.body);
        }).catch(() => {});
      }
    }
  };

  await send("Network.enable", { maxTotalBufferSize: 100 * 1024 * 1024 });
  await send("Page.enable");
  await send("Page.navigate", { url });

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline && bodies.length === 0) await sleep(500);

  if (bodies.length === 0) {
    await send("Runtime.evaluate", { expression: "document.querySelector('video')?.play().catch(()=>{})" });
    const extra = Date.now() + 10000;
    while (Date.now() < extra && bodies.length === 0) await sleep(500);
  }

  let desc = "";
  let playUrls = [];
  for (const raw of bodies) {
    try {
      const data = JSON.parse(raw);
      const detail = data?.aweme_detail;
      if (!detail) continue;
      desc = detail.desc || "";
      const cands = [];
      const push = (addr) => {
        for (const u of (addr?.url_list || [])) if (u) cands.push(u);
      };
      push(detail.video?.play_addr);
      push(detail.video?.download_addr);
      for (const br of (detail.video?.bit_rate || [])) push(br.play_addr);
      playUrls = [...new Set(cands)];
      if (playUrls.length) break;
    } catch { /* 不是 JSON，忽略 */ }
  }

  console.log(JSON.stringify({
    ok: playUrls.length > 0,
    desc: desc.slice(0, 40),
    playUrls: playUrls.slice(0, 4),
    media: [...media].slice(0, 3),
  }));
} catch (err) {
  console.log(JSON.stringify({ ok: false, error: String((err && err.message) || err) }));
  process.exitCode = 1;
} finally {
  try { ws?.close(); } catch { /* ignore */ }
  try { chrome.kill(); } catch { /* ignore */ }
  // 清理临时 profile，避免 %TEMP% 里堆 v2i-cdp-*
  await sleep(300);
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
}
