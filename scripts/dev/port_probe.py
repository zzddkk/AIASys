#!/usr/bin/env python3
"""端口可用性探测：回答「我能不能占用这个端口」，而非「有人在提供服务吗」。

这两个问题不等价，而 dev 启动脚本要的是前者。2026-08-10 的实测事故：

    $ netstat -ano | grep :13001          # 零命中
    $ curl http://127.0.0.1:13001/        # 连不上
    $ python -c "socket.bind(('0.0.0.0', 13001))"
    OSError: [WinError 10048] 通常每个套接字地址只允许使用一次

三者同时成立。占用方是 com.docker.backend，它把 socket bind 在 0.0.0.0:13001
之后没有 listen：

    PS> Get-NetTCPConnection -LocalPort 13001
    0.0.0.0:13001  状态=Bound  PID=21240  com.docker.backend

Bound 而非 Listen 的 socket 不出现在 netstat 里，也不接受连接。于是所有连接式
探测（nc -z、bash 的 /dev/tcp、socket.connect）都会把它判成「空闲」，而服务真去
bind 时必然 10048。cli.sh 原先就是这么误判的：判空闲 → 选中 13001 → uvicorn
bind 失败 → set -euo pipefail 下整链退出，连带前端被 cleanup 杀掉。

这不是偶发。Windows 的动态端口范围实测是 10000-65000：

    PS> netsh int ipv4 show dynamicport tcp
    Start Port : 10000    Number of Ports : 55000

项目默认端口 13000/13001 正落在里面，任何发起出站连接的程序都可能被系统随机
分配到它们。今天是 Docker，明天可能是别的进程，且每次开机结果不同——典型的
flaky 故障源。

所以探测必须用 bind 语义。bind 是唯一与「服务能否真的起来」同构的判据：它问的
就是操作系统会不会把这个端口给我。

用法：
    port_probe.py check <host> <port>          退出码 0=可占用，1=被占用
    port_probe.py find  <host> <start> <count> 打印首个可占用端口，找不到退出码 1

无第三方依赖，只用 stdlib，三端行为一致。
"""

from __future__ import annotations

import socket
import sys


def can_bind(host: str, port: int) -> bool:
    """能否独占绑定 host:port。

    刻意不设 SO_REUSEADDR：要复现的正是服务启动时的独占条件。加上它会让探测比
    实际服务更宽松，重新引入「探测通过但服务起不来」的偏差——那就是本函数要
    消灭的那类假信号。

    host 必须与服务实际使用的绑定地址一致。0.0.0.0 与任一具体地址的同端口互相
    冲突，而 bind 127.0.0.1 成功不保证 bind 0.0.0.0 成功，用错地址会漏判。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        return False
    finally:
        # 立即关闭。TCP 的 TIME_WAIT 只在连接建立后产生，未 listen 未 accept 的
        # socket 关闭后端口即刻可用，不会因为探测本身把端口占住。
        sock.close()
    return True


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    mode = argv[1]

    if mode == "check":
        if len(argv) != 4:
            print("用法: port_probe.py check <host> <port>", file=sys.stderr)
            return 2
        return 0 if can_bind(argv[2], int(argv[3])) else 1

    if mode == "find":
        if len(argv) != 5:
            print("用法: port_probe.py find <host> <start> <count>", file=sys.stderr)
            return 2
        host, start, count = argv[2], int(argv[3]), int(argv[4])
        for port in range(start, start + count):
            if can_bind(host, port):
                print(port)
                return 0
        print(
            f"在 {host}:{start}-{start + count - 1} 范围内未找到可用端口",
            file=sys.stderr,
        )
        return 1

    print(f"未知模式: {mode}（应为 check 或 find）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
