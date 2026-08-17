#!/usr/bin/env bash
#
# 跑 lifecycle e2e（30 个 spec）。负责服务的启停，playwright 只负责跑测试。
#
# 为什么不让 playwright 自己管 webServer：它在 Windows 上 kill 不掉 dev.sh 的进程树。
# 实测 playwright 退出后，上一次的后端仍在监听：
#
#   0.0.0.0:13002  PID=13056  python   ← playwright 已退出，它却还活着
#
# 残留进程会占住端口，让下一次启动被迫换端口，甚至连环失败。所以这里改成显式管理：
# 自己起、自己等、自己收，收尾按 PID 树强杀并逐端口兜底。
#
# 2026-08-10 重写前，这个脚本在 Windows 上从未成功跑完过。四道障碍逐个修掉后才通：
#   1. cli.sh 写死 .venv/bin/uvicorn（Unix 布局），Windows 上后端起不来
#   2. 端口探测用连接语义，对「bind 了但没 listen」的占用（Docker）误判为空闲
#   3. playwright 的 webServer.command 是 "./dev.sh"，Windows 由 cmd.exe 执行，不认
#   4. dev.sh status 用默认端口查询，端口一旦自动切换就永远报 down，等满 240 秒超时
#
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WEB_ROOT="${PROJECT_ROOT}/apps/web"
DEV_PORTS_FILE="${PROJECT_ROOT}/.tmp/dev-ports.env"
DEV_LOG_FILE="$(mktemp -t aiasys-e2e-dev-XXXXXX.log)"

# 数据目录隔离的目标路径（export 动作在 is_windows 定义之后，见 setup_runtime_isolation）。
# 与 playwright.lifecycle.config.ts 里的 webServer.env 保持一致：不隔离时 e2e 后端直接
# 读写真实 ~/AIASys，测试工作区污染真实数据、断言误吸真实内容（asset-tree /
# tabbed-split / auto-task 三条 2026-08-12 的失败根因）。
E2E_RUNTIME_DIR="${TMPDIR:-/tmp}/aiasys-e2e-runtime"

# 端口扫描范围。dev.sh 的自动切换从 13000/13001 起步，正常最多挪几个；给到 13020
# 足够覆盖，又不会扫到无关服务上去。
PORT_SCAN_START=13000
PORT_SCAN_END=13020

MAX_START_ATTEMPTS=3
READY_TIMEOUT_SECONDS=150

DEV_STACK_PID=""
PRE_EXISTING_PIDS=""
STARTED_BY_US=0

is_windows() {
  case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*) return 0 ;;
    *) return 1 ;;
  esac
}

# 列出扫描范围内处于 LISTEN 的进程 PID，每行一个
listening_pids() {
  if is_windows; then
    powershell -NoProfile -Command "
      Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { \$_.LocalPort -ge ${PORT_SCAN_START} -and \$_.LocalPort -le ${PORT_SCAN_END} } |
        Select-Object -ExpandProperty OwningProcess -Unique
    " 2>/dev/null | tr -d '\r' | grep -E '^[0-9]+$' || true
  else
    lsof -ti:"${PORT_SCAN_START}-${PORT_SCAN_END}" -sTCP:LISTEN 2>/dev/null || true
  fi
}

kill_tree() {
  local pid="$1"
  [[ -z "$pid" ]] && return 0
  if is_windows; then
    # /T 连子进程一起杀。dev.sh 下面挂着 uv → python 与 npm → node 两条链，
    # 只杀父进程会留下孤儿继续占端口。
    taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
  else
    pkill -TERM -P "$pid" >/dev/null 2>&1 || true
    kill -TERM "$pid" >/dev/null 2>&1 || true
    sleep 1
    pkill -KILL -P "$pid" >/dev/null 2>&1 || true
    kill -KILL "$pid" >/dev/null 2>&1 || true
  fi
}

# 只杀「我们启动之后才出现」的进程。
#
# 不能无条件清空端口范围：开发者可能自己开着一套服务在调试，把它杀掉属于误伤。
# 用启动前的 PID 快照做差集，是这里唯一安全的判据。
stop_dev_stack() {
  [[ "${STARTED_BY_US}" -eq 0 ]] && return 0

  if [[ -n "${DEV_STACK_PID}" ]]; then
    kill_tree "${DEV_STACK_PID}"
  fi

  local pid
  for pid in $(listening_pids); do
    if ! printf '%s\n' "${PRE_EXISTING_PIDS}" | grep -qx "${pid}"; then
      echo "  收尾：清理残留进程 PID=${pid}" >&2
      kill_tree "${pid}"
    fi
  done

  rm -f "${DEV_PORTS_FILE}"
}

cleanup() {
  local exit_code=$?
  stop_dev_stack
  rm -f "${DEV_LOG_FILE}"
  exit "${exit_code}"
}

# 返回值区分三种情况，因为处置方式不同：
#   0 就绪  1 超时  2 进程已退出  3 端口在探测与绑定之间被抢占（唯一值得重试的）
wait_for_stack_ready() {
  local deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
  local backend_url="" frontend_url=""

  while ((SECONDS < deadline)); do
    if [[ -n "${DEV_STACK_PID}" ]] && ! kill -0 "${DEV_STACK_PID}" >/dev/null 2>&1; then
      # 进程没了也要先看是不是 bind 冲突导致的，否则会把可重试的情况误判成硬失败
      if grep -qE "attempting to bind|Address already in use|10048" "${DEV_LOG_FILE}" 2>/dev/null; then
        return 3
      fi
      return 2
    fi

    # bind 冲突：probe 判定端口可用，到 uvicorn 真正 bind 之间有几秒窗口，动态端口
    # 范围（Windows 实测 10000-65000）内的端口可能在这期间被别的程序抢走。这是
    # check-then-use 的固有竞态，靠重试解决，不必也无法靠更准的探测消除。
    if grep -qE "attempting to bind|Address already in use|10048" "${DEV_LOG_FILE}" 2>/dev/null; then
      return 3
    fi

    if [[ -f "${DEV_PORTS_FILE}" ]]; then
      # shellcheck disable=SC1090
      source "${DEV_PORTS_FILE}"
      backend_url="${BACKEND_URL:-}"
      frontend_url="${FRONTEND_URL:-}"
      if [[ -n "${backend_url}" && -n "${frontend_url}" ]] \
        && curl -fsS -m 3 -o /dev/null "${frontend_url}/" 2>/dev/null \
        && curl -fsS -m 3 -o /dev/null "${backend_url}/health" 2>/dev/null; then
        return 0
      fi
    fi

    sleep 1
  done

  return 1
}

# 数据目录隔离：后端认 AIASYS_RUNTIME_DATA_DIR / LOGS_DIR / WORKSPACES_DIR 三个 env
# （apps/backend/app/core/config.py），优先级高于数据目录里持久化的存储路径覆盖；
# local 认证默认用户在空目录下自建。固定路径 + 启动时清空，跑完不留垃圾。
# 已知边界：「复用已在运行的 dev server」分支不适用隔离（那套栈的数据目录由它自己的
# 启动环境决定），复用时会在 stderr 打出提示。
setup_runtime_isolation() {
  if is_windows; then
    # Git Bash 下 /tmp 映射到 %TEMP% 子目录，但后端是原生 Windows 进程，认的是
    # Windows 路径。通过 cygpath 转成 C:\... 形式再传给 env。
    E2E_RUNTIME_DIR="$(cygpath -w "${E2E_RUNTIME_DIR}" 2>/dev/null || echo "${E2E_RUNTIME_DIR}")"
  fi
  rm -rf "${E2E_RUNTIME_DIR}"
  export AIASYS_RUNTIME_DATA_DIR="${E2E_RUNTIME_DIR}/data"
  export AIASYS_RUNTIME_LOGS_DIR="${E2E_RUNTIME_DIR}/logs"
  export AIASYS_RUNTIME_WORKSPACES_DIR="${E2E_RUNTIME_DIR}/workspaces"
}

start_dev_stack() {
  STARTED_BY_US=1
  rm -f "${DEV_PORTS_FILE}"
  : >"${DEV_LOG_FILE}"
  (
    cd "${PROJECT_ROOT}"
    exec ./dev.sh
  ) >"${DEV_LOG_FILE}" 2>&1 &
  DEV_STACK_PID=$!
}

trap cleanup EXIT INT TERM

PRE_EXISTING_PIDS="$(listening_pids)"
if [[ -n "${PRE_EXISTING_PIDS}" ]]; then
  echo "启动前 ${PORT_SCAN_START}-${PORT_SCAN_END} 已有进程（收尾时不会动它们）：" >&2
  printf '  PID %s\n' ${PRE_EXISTING_PIDS} >&2
fi

if "${PROJECT_ROOT}/dev.sh" status >/dev/null 2>&1; then
  echo "检测到开发服务已在运行，直接复用。" >&2
  echo "注意：复用模式下数据目录隔离不生效，测试会读写该服务自身的 ~/AIASys。" >&2
  if [[ -f "${DEV_PORTS_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${DEV_PORTS_FILE}"
  fi
else
  setup_runtime_isolation
  ready_rc=1
  for attempt in $(seq 1 "${MAX_START_ATTEMPTS}"); do
    echo "启动开发服务（第 ${attempt}/${MAX_START_ATTEMPTS} 次尝试）…" >&2
    start_dev_stack
    set +e
    wait_for_stack_ready
    ready_rc=$?
    set -e

    case "${ready_rc}" in
      0)
        echo "开发服务已就绪：frontend=${FRONTEND_URL} backend=${BACKEND_URL}" >&2
        break
        ;;
      3)
        echo "端口在探测与绑定之间被抢占，清理后重试。" >&2
        grep -E "attempting to bind|10048" "${DEV_LOG_FILE}" 2>/dev/null | head -2 >&2 || true
        stop_dev_stack
        STARTED_BY_US=1  # stop_dev_stack 不重置它，这里显式保持，供下次收尾
        sleep 2
        ;;
      2)
        echo "开发服务提前退出，日志如下：" >&2
        cat "${DEV_LOG_FILE}" >&2
        exit 1
        ;;
      *)
        echo "等待开发服务就绪超时（${READY_TIMEOUT_SECONDS}s），日志如下：" >&2
        tail -40 "${DEV_LOG_FILE}" >&2
        exit 1
        ;;
    esac
  done

  if [[ "${ready_rc}" -ne 0 ]]; then
    echo "连续 ${MAX_START_ATTEMPTS} 次启动均因端口被抢占失败。" >&2
    echo "根因是默认端口 13000/13001 落在 Windows 动态端口范围内（10000-65000），" >&2
    echo "可用 AIASYS_BACKEND_PORT / AIASYS_FRONTEND_PORT 指定该范围外的端口规避。" >&2
    exit 1
  fi
fi

# 把实际端口交给 playwright。配置里 baseURL 取 PLAYWRIGHT_BASE_URL，不设的话它会用
# 硬编码的 13000——前端端口一旦自动切换，测试就会全部打到一个空端口上。
export PLAYWRIGHT_BASE_URL="${FRONTEND_URL:-http://localhost:13000}"
echo "PLAYWRIGHT_BASE_URL=${PLAYWRIGHT_BASE_URL}" >&2

# 后端同理，而且更隐蔽：readiness.setup.ts 的健康检查默认值写死了 13002
# （那是某次调试时 13001 被占、后端自动移位后的端口），后端在 13001 上正常服务时
# 它就等满 180 秒超时——2026-08-13 干净端口环境下实测复现。这里必须用 dev-ports.env
# 里的实际端口显式覆盖。
export PLAYWRIGHT_BACKEND_HEALTH_URL="${BACKEND_URL:-http://127.0.0.1:13001}/health"
echo "PLAYWRIGHT_BACKEND_HEALTH_URL=${PLAYWRIGHT_BACKEND_HEALTH_URL}" >&2

cd "${WEB_ROOT}"
set +e
# 配置可通过环境变量覆盖，默认跑 lifecycle 回归套件。
#
# e2e/manual/ 下那批以截图为输出的人工审查脚本用的是 playwright.manual.config.ts，
# 它们同样需要一个起好的全栈环境，没道理再复制一遍这份服务启停逻辑（端口扫描、
# 就绪等待、按 PID 树收尾在 Windows 上都是踩过坑才写对的）。所以这里只把配置文件
# 参数化，跑法变成：
#   PLAYWRIGHT_CONFIG=playwright.manual.config.ts bash scripts/dev/run_lifecycle_playwright.sh e2e/manual/xxx.spec.ts
PLAYWRIGHT_CONFIG="${PLAYWRIGHT_CONFIG:-playwright.lifecycle.config.ts}"
if [[ ! -f "${PLAYWRIGHT_CONFIG}" ]]; then
  # 早失败并说清原因。配置名拼错时 playwright 自己的报错是「no tests found」，
  # 方向完全指错，会让人去查测试文件而不是查配置名。
  echo "找不到 playwright 配置: ${WEB_ROOT}/${PLAYWRIGHT_CONFIG}" >&2
  exit 2
fi
echo "PLAYWRIGHT_CONFIG=${PLAYWRIGHT_CONFIG}" >&2
npx playwright test -c "${PLAYWRIGHT_CONFIG}" "$@"
PLAYWRIGHT_EXIT=$?
set -e

# 显式退出并带上 playwright 的退出码。trap 里的 cleanup 会用 $? 继承它，
# 不这样写的话最后一条清理命令的退出码会把测试结果覆盖成 0。
exit "${PLAYWRIGHT_EXIT}"
