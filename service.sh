#!/usr/bin/env bash
# 文件位置: contract-review/service.sh
# 功能说明: 合同审查系统（FastAPI + uvicorn，端口 18090）的一键启停脚本。
#
#   用法：
#     bash service.sh start    启动（已在运行则提示，不重复拉起）
#     bash service.sh stop     停止（按端口找进程，避免误杀别的 python）
#     bash service.sh status   查看运行状态
#     bash service.sh restart  重启
#
#   为什么要有这个脚本：直接 python -m uvicorn ... 跑在后台时，终端一关进程就没了；
#   本脚本用 nohup + disown 把服务从当前终端会话剥离，日志落到 logs/server.log，
#   关掉终端服务仍在。前提是在你自己的交互式 Git Bash 里执行——
#   若在短生命周期的自动化 shell 里跑，子进程仍会随该 shell 结束被回收。
set -e
cd "$(dirname "$0")"

PORT=18090
# Python 解释器：可用环境变量 PY 覆盖，默认用本机 3.12
PY="${PY:-C:/Program Files/Python312/python.exe}"
# 共享鉴权底座（硬依赖，缺了启动会 ModuleNotFoundError）
SAKURA="${SAKURA:-D:/WorkBuddy/2026-10-14-11-06-38/sakura_platform/packages/sakura_base[web]}"

is_up() { curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/"; }

# 按端口找监听进程的 PID（Windows netstat 最后一列即 PID）
pids_on_port() {
  netstat -ano 2>/dev/null | grep ":$PORT " | grep LISTENING | awk '{print $NF}' | sort -u
}

# 依赖自检：sakura_base 缺失时给出安装动作，别等 uvicorn 抛一堆栈
ensure_deps() {
  if ! "$PY" -c "import sakura_base" 2>/dev/null; then
    echo "[!] 缺少共享鉴权底座 sakura_base，正在安装：$SAKURA"
    "$PY" -m pip install -e "$SAKURA"
  fi
}

case "${1:-start}" in
  start)
    if is_up; then echo "[=] 服务已在 http://127.0.0.1:$PORT/ 运行"; exit 0; fi
    ensure_deps
    mkdir -p logs
    nohup "$PY" -m uvicorn main:app --host 0.0.0.0 --port "$PORT" > logs/server.log 2>&1 &
    disown
    sleep 3
    if is_up; then
      echo "[+] 已启动：http://127.0.0.1:$PORT/"
      echo "    演示账号：zhoumingyuan@gmail.com / zhoumingyuan （法务合规经理）"
      echo "               chenjingzhi@gmail.com  / chenjingzhi  （平台管理员）"
      echo "    日志：logs/server.log"
    else
      echo "[x] 启动失败，请查看 logs/server.log"
      tail -n 20 logs/server.log 2>/dev/null || true
      exit 1
    fi
    ;;
  stop)
    pids="$(pids_on_port)"
    if [ -z "$pids" ]; then echo "[=] 端口 $PORT 无监听进程，无需停止"; exit 0; fi
    for pid in $pids; do
      taskkill //PID "$pid" //F >/dev/null 2>&1 && echo "[-] 已停止进程 $pid" || echo "[!] 停止进程 $pid 失败（可能需要管理员权限）"
    done
    ;;
  status)
    if is_up; then
      echo "[+] 运行中：http://127.0.0.1:$PORT/（PID: $(pids_on_port | tr '\n' ' ')）"
    else
      echo "[-] 未运行"
    fi
    ;;
  restart)
    "$0" stop || true
    sleep 1
    "$0" start
    ;;
  *)
    echo "用法：bash service.sh {start|stop|status|restart}"
    exit 1
    ;;
esac
