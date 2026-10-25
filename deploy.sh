#!/usr/bin/env bash
# =============================================================================
#  v2i 一键部署（Linux / macOS / WSL）
#
#  做的事：
#    1. 检查 Python >= 3.10 与 pip
#    2. 把依赖装进 vendor/（不污染系统 Python）
#    3. 生成默认配置 .env（从 .env.example 复制）
#    4. 起 Web 服务（默认 0.0.0.0:8765），带健康检查
#    5. 可选：装成 systemd 服务（开机自启 / 崩溃重启）
#
#  常用：
#    ./deploy.sh                      # 首次部署并启动
#    ./deploy.sh --port 9000          # 换端口
#    ./deploy.sh --service            # 装成 systemd 服务（需要 root）
#    ./deploy.sh --skip-deps          # 依赖已装好，只重启
#    ./deploy.sh --no-start           # 只准备环境，不启动
#    ./deploy.sh --stop               # 停掉 nohup 起的服务
#    ./deploy.sh --dry-run            # 只打印将执行的命令
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT=8765
HOST=0.0.0.0
PY="${PYTHON:-python3}"
MIRROR="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
SKIP_DEPS=0
FORCE_DEPS=0
INSTALL_SERVICE=0
NO_START=0
DRY_RUN=0
DO_STOP=0
SERVICE_NAME="${SERVICE_NAME:-v2i}"

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    --mirror) MIRROR="$2"; shift 2 ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --force-deps) FORCE_DEPS=1; shift ;;
    --service) INSTALL_SERVICE=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --stop) DO_STOP=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage; exit 1 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '    [dry-run] %s\n' "$*"
  else
    eval "$@"
  fi
}
die() { printf '\n[x] %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 0. 停止
if [[ $DO_STOP -eq 1 ]]; then
  PIDFILE="$ROOT/.runtime/web.pid"
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    PID="$(cat "$PIDFILE")"
    kill "$PID" && rm -f "$PIDFILE"
    info "已停止（pid $PID）"
  else
    info "没有正在运行的服务（$PIDFILE 不存在或进程已退出）"
  fi
  exit 0
fi

# ---------------------------------------------------------------- 1. Python
step "检查 Python（需要 >= 3.10）"
if ! command -v "$PY" >/dev/null 2>&1; then
  die "找不到 $PY。Ubuntu/Debian: sudo apt install -y python3 python3-pip python3-venv；CentOS: sudo dnf install -y python3 python3-pip"
fi
"$PY" - <<'PYEOF'
import sys
if sys.version_info[:2] < (3, 10):
    sys.exit(f"Python 版本过低：{sys.version.split()[0]}，需要 3.10+")
print(f"    Python {sys.version.split()[0]} @ {sys.executable}")
PYEOF

# ---------------------------------------------------------------- 2. 依赖
step "安装依赖到 vendor/（不污染系统 Python）"
if [[ $SKIP_DEPS -eq 1 ]]; then
  info "跳过（--skip-deps）"
elif [[ $FORCE_DEPS -eq 0 && -d "$ROOT/vendor/faster_whisper" && -d "$ROOT/vendor/yt_dlp" ]]; then
  info "vendor/ 已有依赖，跳过（要强制更新加 --force-deps）"
else
  if ! "$PY" -m pip --version >/dev/null 2>&1; then
    info "pip 不可用，尝试 ensurepip …"
    run "\"$PY\" -m ensurepip --upgrade" || die "pip 不可用：请先安装 python3-pip / python3-venv"
  fi
  run "\"$PY\" -m pip install --target \"$ROOT/vendor\" --upgrade -r \"$ROOT/requirements.txt\" -i \"$MIRROR\""
  info "提示：国内网络慢可以先设 PIP_INDEX_URL，或加 --mirror <url>"
fi

# ---------------------------------------------------------------- 3. 配置
step "准备配置与目录"
if [[ -f "$ROOT/.env" ]]; then
  info ".env 已存在，保持不动"
else
  run "cp \"$ROOT/.env.example\" \"$ROOT/.env\""
  info "已生成 .env —— 记得填 DEEPSEEK_API_KEY（也可以在 Web 的「系统配置」里填）"
fi
run "mkdir -p \"$ROOT/.runtime\" \"$ROOT/runs\" \"$ROOT/.cookies\""
run "chmod +x \"$ROOT/v2i.sh\" \"$ROOT/deploy.sh\""

# ---------------------------------------------------------------- 4. 启动
start_nohup() {
  LOG="$ROOT/.runtime/web.log"
  PIDFILE="$ROOT/.runtime/web.pid"
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    info "已有进程在跑（pid $(cat "$PIDFILE")），先停掉再起"
    run "kill \"\$(cat \"$PIDFILE\")\" || true"
    run "rm -f \"$PIDFILE\""
    sleep 1
  fi
  info "启动：$HOST:$PORT → 日志 $LOG"
  run "PYTHONUNBUFFERED=1 nohup \"$ROOT/v2i.sh\" web --host \"$HOST\" --port \"$PORT\" >> \"$LOG\" 2>&1 & echo \$! > \"$PIDFILE\""
}

health_check() {
  if [[ $DRY_RUN -eq 1 ]]; then
    info "[dry-run] curl -fsS http://127.0.0.1:$PORT/api/status"
    return 0
  fi
  local i
  for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
      info "健康检查通过（第 ${i} 次尝试）"
      return 0
    fi
    sleep 1
  done
  die "服务没起来，看看日志：tail -n 50 $ROOT/.runtime/web.log"
}

if [[ $INSTALL_SERVICE -eq 1 ]]; then
  step "安装 systemd 服务（$SERVICE_NAME）"
  [[ "$(id -u)" -eq 0 ]] || die "装 systemd 服务需要 root：sudo ./deploy.sh --service"
  UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
  cat >"$UNIT" <<EOF
[Unit]
Description=v2i 视频转文字服务
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$ROOT/v2i.sh web --host $HOST --port $PORT
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
# 需要代理时取消注释：
# Environment=HTTPS_PROXY=http://127.0.0.1:7890
# Environment=HTTP_PROXY=http://127.0.0.1:7890

[Install]
WantedBy=multi-user.target
EOF
  info "已写入 $UNIT"
  run "systemctl daemon-reload"
  run "systemctl enable --now \"$SERVICE_NAME\""
  health_check || true
  info "常用命令：systemctl status $SERVICE_NAME / journalctl -u $SERVICE_NAME -f"
elif [[ $NO_START -eq 1 ]]; then
  step "跳过启动（--no-start）"
  info "手动启动：$ROOT/v2i.sh web --host $HOST --port $PORT"
else
  step "启动服务（nohup 后台）"
  start_nohup
  health_check
fi

# ---------------------------------------------------------------- 5. 汇总
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
step "完成"
info "本机访问：http://127.0.0.1:$PORT/"
[[ -n "${LAN_IP:-}" ]] && info "局域网：  http://$LAN_IP:$PORT/"
info "日志：$ROOT/.runtime/web.log"
info "停止：$ROOT/deploy.sh --stop    重启：$ROOT/deploy.sh --skip-deps"
info "首次使用：打开页面 → 系统配置 → 填 DEEPSEEK_API_KEY → 保存并生效"
info "服务器记得放行端口：ufw allow $PORT/tcp  或  firewall-cmd --add-port=$PORT/tcp --permanent"
