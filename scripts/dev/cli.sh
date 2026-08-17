#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRONTEND_PORT="${AIASYS_FRONTEND_PORT:-13000}"
BACKEND_PORT="${AIASYS_BACKEND_PORT:-13001}"
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
FRONTEND_URL="http://127.0.0.1:${FRONTEND_PORT}"

# 实际生效端口的落盘位置。
#
# 为什么需要它：start 分支会在端口被占用时自动切换（13001 → 13002），但那个新端口
# 只活在 start 那次进程的内存里。`dev.sh status` 是全新进程，第 7 行把 BACKEND_PORT
# 重新算回默认的 13001，于是只要端口切换过，status 就永远报 backend down。
#
# 后果是把前面所有端口修复都抵消掉：run_lifecycle_playwright.sh 用 status 判就绪，
# 服务明明起来了它也认不出，一路等满 240 秒再报「等待开发服务就绪超时」——错误信息
# 还会把人误导向「启动太慢」，而真实原因是「查错了端口」。
#
# .tmp/ 已在 .gitignore 内，不用新增忽略规则。
DEV_PORTS_FILE="${PROJECT_ROOT}/.tmp/dev-ports.env"

write_dev_ports() {
  mkdir -p "$(dirname "${DEV_PORTS_FILE}")"
  cat >"${DEV_PORTS_FILE}" <<EOF
# 由 scripts/dev/cli.sh 在启动时写入，退出时删除。手改无效。
BACKEND_PORT=${BACKEND_PORT}
FRONTEND_PORT=${FRONTEND_PORT}
BACKEND_URL=${BACKEND_URL}
FRONTEND_URL=${FRONTEND_URL}
EOF
}

# 读取实际端口。文件可能是上次异常退出留下的陈旧记录，所以调用方拿到端口后仍要自己
# 探活——本函数只负责「换个更可能对的端口去问」，不承诺服务活着。
load_dev_ports() {
  if [[ -f "${DEV_PORTS_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${DEV_PORTS_FILE}"
    return 0
  fi
  return 1
}

command_name="${1:-start}"
if [[ "$#" -gt 0 ]]; then
  shift
fi

print_usage() {
  cat <<EOF
Usage:
  ./dev.sh              启动前后端开发服务
  ./dev.sh start        启动前后端开发服务
  ./dev.sh status       查看前后端端口与健康状态
  ./dev.sh design-lint  校验根目录 DESIGN.md
  ./dev.sh design-export-css [output]
                        从 DESIGN.md 生成 Tailwind 4 CSS 变量草案
  ./dev.sh design-export-runtime
                        生成当前运行时变量候选主题和映射说明
  ./dev.sh setup-hooks  启用仓库内置 Git hooks
EOF
}

check_url_ready() {
  local url="$1"
  curl -fsS "$url" >/dev/null 2>&1
}

# /dev/tcp 能力自检结果缓存：unknown / yes / no
_DEV_TCP_CAPABLE="unknown"
# 「无法探测」的警告只打一次：find_available_port 会循环调用 200 次
_PORT_PROBE_WARNED=0

# 检测 bash 是否支持 /dev/tcp 网络重定向。
#
# 判据是错误串形态而不是退出码：「连接被拒」与「不支持该伪设备」的退出码都是 1，
# 光看退出码无法区分，而这两种情况的正确处置完全相反（前者说明端口空闲，后者说明
# 探测手段不可用）。端口 1 在三端都几乎必然无人监听，支持时会立刻拿到
# Connection refused。已实测两种形态：
#   支持   → "/usr/bin/bash: line 2: /dev/tcp/127.0.0.1/1: Connection refused"
#   不支持 → "...: No such file or directory"（用 /dev/nosuchthing 伪设备对照确认）
_check_dev_tcp_capable() {
  local err
  # `|| true` 是必需的：本文件开头有 set -e，命令替换里的非零退出会直接终止脚本
  err=$( (exec 3<>/dev/tcp/127.0.0.1/1) 2>&1 ) || true
  case "$err" in
    *"No such file or directory"*) _DEV_TCP_CAPABLE="no" ;;
    *) _DEV_TCP_CAPABLE="yes" ;;
  esac
}

# 端口探测：返回 0 表示空闲，1 表示被占用
#
# 2026-08-09 修跨平台缺陷。原实现只有 nc 与 python3 两条探测路径，而 Windows
# Git Bash 下两者都不存在（本机实测 nc / python3 / python 三个都没有），于是每次
# 都走到函数末尾的 `return 1`。而 return 1 的语义是「端口被占用」——探测手段缺失
# 被当成了探测结果，方向还是最坏的那个：find_available_port 遍历 200 个端口全部
# 判定占用后返回空值，脚本在 set -euo pipefail 下静默退出，一行错误都不打印
# （实测 dev.log 完全为空，bash -x 才追到 NEW_BACKEND_PORT= 后立即 exit 1）。
#
# 后果不止于 dev.sh 不可用：playwright.lifecycle.config.ts 的 webServer 就是
# ./dev.sh，所以 30 个 lifecycle e2e 在 Windows 上根本无法运行，只能靠人工点界面。
#
# 主路径改用 bash 内置的 /dev/tcp：零外部依赖、三端一致，属于「代码层消灭平台
# 分支」。已实测 Git Bash（bash 5.2.37）双向正确——对已知 LISTEN 端口连得上、
# 对空闲端口连不上。nc 与 python3 保留为回退，不删除既有能力。
#
# 兜底语义从「视为占用」改为「视为空闲 + 警告一次」。理由是失败要响：判为空闲时
# 后续 bind 会自己抛 Address already in use，错误明确且指向真实原因；判为占用则
# 表现为静默死亡，把「探测工具缺失」伪装成「端口都被占了」。
#
# 2026-08-10 二次修正：主路径从「连接探测」改为「bind 探测」。
#
# 上面那版用 /dev/tcp 连得上与否判断占用，问的是「有人在提供服务吗」，而这里真正
# 要问的是「我能不能占用它」。两者不等价，实测被 Docker 打了个正面：
#
#   netstat -ano | grep :13001              零命中
#   curl http://127.0.0.1:13001/            连不上
#   Get-NetTCPConnection -LocalPort 13001   0.0.0.0:13001 状态=Bound
#                                           PID=21240 com.docker.backend
#
# com.docker.backend 把 socket bind 在 13001 后没有 listen。Bound 而非 Listen 的
# socket 不出现在 netstat 里、也不接受连接，于是连接式探测一律判「空闲」，而服务
# 真去 bind 时必然 10048。表现就是 dev.sh 选中 13001 → uvicorn bind 失败 →
# set -euo pipefail 下整链退出 → 前端被 cleanup 连带杀掉，全栈起不来。
#
# 且这不是偶发：Windows 动态端口范围实测 10000-65000（netsh int ipv4 show
# dynamicport tcp），项目默认端口 13000/13001 正落在里面，任何发起出站连接的程序
# 都可能被系统随机分配到它们，每次开机结果还不同——典型 flaky 故障源。
#
# 探测逻辑移到 scripts/dev/port_probe.py（纯 stdlib），解释器优先用 backend 的
# venv：它必然存在（项目本来就要 uv sync 才能跑），且是普通 python 调用、无
# `uv run` 的环境检查开销。连接探测降级为无 python 时的回退，并在语义上标明它
# 漏判 Bound 状态。
_PORT_PROBE_PY=""
_PORT_PROBE_PY_RESOLVED=0

_resolve_port_probe_py() {
  _PORT_PROBE_PY_RESOLVED=1
  local c
  # venv 优先：与后端实际运行的解释器一致，也避免 PATH 里挑到不带 stdlib 的怪东西
  for c in \
    "${PROJECT_ROOT}/apps/backend/.venv/Scripts/python.exe" \
    "${PROJECT_ROOT}/apps/backend/.venv/bin/python"; do
    if [[ -x "$c" ]]; then
      _PORT_PROBE_PY="$c"
      return 0
    fi
  done
  for c in python3 python py; do
    if command -v "$c" >/dev/null 2>&1; then
      _PORT_PROBE_PY="$c"
      return 0
    fi
  done
  return 1
}

probe_port() {
  local host="$1" port="$2"

  if [[ "$_PORT_PROBE_PY_RESOLVED" -eq 0 ]]; then
    _resolve_port_probe_py || true
  fi

  if [[ -n "$_PORT_PROBE_PY" ]]; then
    if "$_PORT_PROBE_PY" "${PROJECT_ROOT}/scripts/dev/port_probe.py" check "$host" "$port"; then
      return 0  # 可绑定 = 空闲
    fi
    return 1  # 不可绑定 = 被占用（含 Bound 未 listen 这类连接探测看不见的占用）
  fi

  # ── 以下均为无 python 时的回退：连接探测 ──
  #
  # 语义弱于 bind：只能发现「有人在 listen」，发现不了 bind 未 listen 的占用
  # （见上面 Docker 的实例）。保留是为了不丢失既有能力，但不再是首选。
  #
  # 连接目标统一改回环：调用方传的是服务的绑定地址 0.0.0.0，而「连接 0.0.0.0」
  # 的行为跨平台不一致（部分平台直接失败），会让整条回退路径退化成「一律判空闲」。
  local probe_host="$host"
  if [[ "$probe_host" == "0.0.0.0" || "$probe_host" == "::" ]]; then
    probe_host="127.0.0.1"
  fi

  if [[ "$_DEV_TCP_CAPABLE" == "unknown" ]]; then
    _check_dev_tcp_capable
  fi

  if [[ "$_DEV_TCP_CAPABLE" == "yes" ]]; then
    # fd 3 在子 shell 内打开，随子 shell 退出自动回收，无需手动关闭
    if (exec 3<>"/dev/tcp/${probe_host}/${port}") 2>/dev/null; then
      return 1  # 连得上 = 被占用
    fi
    return 0  # 连不上 = 空闲（注意：可能漏判 Bound 未 listen 的占用）
  fi

  if command -v nc >/dev/null 2>&1; then
    if nc -z "$probe_host" "$port" 2>/dev/null; then
      return 1  # 端口可达 = 被占用
    else
      return 0  # 端口不可达 = 空闲
    fi
  else
    if [[ "$_PORT_PROBE_WARNED" -eq 0 ]]; then
      _PORT_PROBE_WARNED=1
      echo "警告：无可用的端口探测手段（没有 python、bash 不支持 /dev/tcp、也没有 nc）。" >&2
      echo "      端口一律视为空闲，若实际被占用会在服务启动时报 Address already in use。" >&2
    fi
    return 0
  fi
}

# 查找可用端口
find_available_port() {
  local host="$1" start="$2" max="${3:-200}"

  if [[ "$_PORT_PROBE_PY_RESOLVED" -eq 0 ]]; then
    _resolve_port_probe_py || true
  fi

  # 有 python 时把整段扫描交给它：一次解释器启动扫完 max 个端口，而不是逐端口
  # 起一次进程。max 默认 200，逐个起进程在 Windows 上光是进程创建就要好几秒。
  if [[ -n "$_PORT_PROBE_PY" ]]; then
    "$_PORT_PROBE_PY" "${PROJECT_ROOT}/scripts/dev/port_probe.py" find "$host" "$start" "$max"
    return $?
  fi

  local p
  for ((p = start; p < start + max; p++)); do
    if probe_port "$host" "$p"; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

status_command() {
  local frontend_status="down"
  local backend_status="down"
  # 先记下默认端口算出的 URL，load_dev_ports 会覆盖同名变量
  local default_backend_url="${BACKEND_URL}"
  local default_frontend_url="${FRONTEND_URL}"

  # 优先按实际生效端口查询（理由见文件头 DEV_PORTS_FILE 的注释）
  load_dev_ports || true

  if check_url_ready "${FRONTEND_URL}/"; then
    frontend_status="up"
  fi

  if check_url_ready "${BACKEND_URL}/health"; then
    backend_status="up"
  fi

  # 端口文件可能是上次异常退出留下的陈旧记录，也可能服务是用 AIASYS_*_PORT 固定端口
  # 另行起的。两种情况下回落到默认端口再问一次，避免「服务活着却报 down」。
  if [[ "${backend_status}" == "down" && "${BACKEND_URL}" != "${default_backend_url}" ]]; then
    if check_url_ready "${default_backend_url}/health"; then
      backend_status="up"
      BACKEND_URL="${default_backend_url}"
    fi
  fi
  if [[ "${frontend_status}" == "down" && "${FRONTEND_URL}" != "${default_frontend_url}" ]]; then
    if check_url_ready "${default_frontend_url}/"; then
      frontend_status="up"
      FRONTEND_URL="${default_frontend_url}"
    fi
  fi

  echo "frontend ${FRONTEND_URL}: ${frontend_status}"
  echo "backend  ${BACKEND_URL}: ${backend_status}"

  if [[ "${frontend_status}" == "up" && "${backend_status}" == "up" ]]; then
    return 0
  fi

  return 1
}

start_backend() {
  (
    cd "${PROJECT_ROOT}/apps/backend"
    # 主路径用 uv run，不写死 .venv/bin/uvicorn。
    #
    # 原写法 `exec .venv/bin/uvicorn` 是 Unix 专属的 venv 布局：Windows 上
    # 可执行文件在 .venv/Scripts/ 且带 .exe 后缀，所以在 Windows 必然
    # "No such file or directory"。后果不只是后端起不来——
    # playwright.lifecycle.config.ts 的 webServer 就是 ./dev.sh，于是 30 个
    # lifecycle e2e 测试在 Windows 上根本无法运行，只能靠人工点界面确认。
    #
    # uv run 三端行为一致（项目 CI 的 setup-uv 与本地 `uv run pytest` 全程用它），
    # 属于「代码层消灭平台分支」而不是加一层 if os.name 判断。
    # 后面两个分支是无 uv 环境的回退，按 Windows / Unix 各自的 venv 布局探测；
    # 全都找不到时显式报错退出，而不是像原来那样静默失败——静默失败会让调用方
    # （run_lifecycle_playwright.sh）一直等到 240 秒超时才报「等待开发服务就绪超时」，
    # 把「路径不对」误导成「启动慢」。
    if command -v uv >/dev/null 2>&1; then
      exec uv run uvicorn app.main:app --host 0.0.0.0 --port "${BACKEND_PORT}"
    elif [[ -x ".venv/Scripts/uvicorn.exe" ]]; then
      exec .venv/Scripts/uvicorn.exe app.main:app --host 0.0.0.0 --port "${BACKEND_PORT}"
    elif [[ -x ".venv/bin/uvicorn" ]]; then
      exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${BACKEND_PORT}"
    else
      echo "启动后端失败：找不到 uvicorn。" >&2
      echo "  既没有 uv 可执行文件，也没有 .venv/Scripts/uvicorn.exe（Windows）" >&2
      echo "  或 .venv/bin/uvicorn（macOS / Linux）。" >&2
      echo "  请先在 apps/backend 下执行 uv sync。" >&2
      exit 1
    fi
  ) &
  BACKEND_PID=$!
}

start_frontend() {
  (
    cd "${PROJECT_ROOT}/apps/web"
    export VITE_API_TARGET="${BACKEND_URL}"
    exec npm run dev -- --host 0.0.0.0 --port "${FRONTEND_PORT}"
  ) &
  FRONTEND_PID=$!
}

cleanup_children() {
  local exit_code=$?

  # 先删端口文件：避免进程都没了还留着一份「服务在 13002」的陈旧记录，
  # 让下一次 status 去问一个空端口
  rm -f "${DEV_PORTS_FILE}"

  if [[ -n "${FRONTEND_PID:-}" ]]; then
    kill "${FRONTEND_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${BACKEND_PID:-}" ]]; then
    kill "${BACKEND_PID}" >/dev/null 2>&1 || true
  fi

  wait "${FRONTEND_PID:-}" >/dev/null 2>&1 || true
  wait "${BACKEND_PID:-}" >/dev/null 2>&1 || true

  exit "${exit_code}"
}

case "${command_name}" in
  start)
    trap cleanup_children EXIT INT TERM

    # 检查后端端口
    BACKEND_LOCKED=false
    if [[ -n "${AIASYS_BACKEND_PORT:-}" ]]; then
      BACKEND_LOCKED=true
    fi
    if ! probe_port "0.0.0.0" "${BACKEND_PORT}"; then
      if ${BACKEND_LOCKED}; then
        echo "❌ 后端端口 ${BACKEND_PORT} 已被占用，且 AIASYS_BACKEND_PORT 已锁定" >&2
        exit 1
      fi
      NEW_BACKEND_PORT=$(find_available_port "0.0.0.0" "$((BACKEND_PORT + 1))")
      if [[ -z "${NEW_BACKEND_PORT}" ]]; then
        echo "❌ 无法为后端找到可用端口（起始: ${BACKEND_PORT}）" >&2
        exit 1
      fi
      echo "⚠ 后端端口 ${BACKEND_PORT} 被占用，自动切换到 ${NEW_BACKEND_PORT}"
      BACKEND_PORT="${NEW_BACKEND_PORT}"
      BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
    fi

    # 检查前端端口
    FRONTEND_LOCKED=false
    if [[ -n "${AIASYS_FRONTEND_PORT:-}" ]]; then
      FRONTEND_LOCKED=true
    fi
    if ! probe_port "0.0.0.0" "${FRONTEND_PORT}"; then
      if ${FRONTEND_LOCKED}; then
        echo "❌ 前端端口 ${FRONTEND_PORT} 已被占用，且 AIASYS_FRONTEND_PORT 已锁定" >&2
        exit 1
      fi
      NEW_FRONTEND_PORT=$(find_available_port "0.0.0.0" "$((FRONTEND_PORT + 1))")
      if [[ -z "${NEW_FRONTEND_PORT}" ]]; then
        echo "❌ 无法为前端找到可用端口（起始: ${FRONTEND_PORT}）" >&2
        exit 1
      fi
      echo "⚠ 前端端口 ${FRONTEND_PORT} 被占用，自动切换到 ${NEW_FRONTEND_PORT}"
      FRONTEND_PORT="${NEW_FRONTEND_PORT}"
      FRONTEND_URL="http://127.0.0.1:${FRONTEND_PORT}"
    fi

    # 端口至此已最终确定（含自动切换的结果），落盘供 status 与 e2e 脚本读取
    write_dev_ports

    start_backend
    start_frontend
    # Wait for any background job to finish (bash 3.2 compatible alternative to wait -n)
    while true; do
      for pid in $(jobs -p); do
        if ! kill -0 "$pid" 2>/dev/null; then
          break 2
        fi
      done
      sleep 0.5
    done
    ;;
  status)
    status_command
    ;;
  design-lint)
    exec "${PROJECT_ROOT}/scripts/design/validate-design-md.sh" "$@"
    ;;
  design-export-css)
    exec node "${PROJECT_ROOT}/scripts/design/export-tailwind4-css.mjs" "$@"
    ;;
  design-export-runtime)
    exec node "${PROJECT_ROOT}/scripts/design/export-runtime-theme-candidate.mjs" "$@"
    ;;
  setup-hooks)
    exec "${PROJECT_ROOT}/scripts/dev/setup-hooks.sh"
    ;;
  help|-h|--help)
    print_usage
    ;;
  *)
    echo "Unknown command: ${command_name}" >&2
    print_usage >&2
    exit 1
    ;;
esac
