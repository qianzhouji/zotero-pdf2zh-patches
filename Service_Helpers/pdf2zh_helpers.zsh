# >>> PDF2ZH HELPERS V2 >>>

pdf2zh-start() {
    local domain="gui/$(id -u)"
    local label="com.yuke.pdf2zh"
    local plist="$HOME/Library/LaunchAgents/com.yuke.pdf2zh.plist"
    local health="http://127.0.0.1:8890/health"
    local errlog="$HOME/zotero-pdf2zh/server/pdf2zh-launchd-error.log"

    # 已经正常运行就不重复启动
    if /usr/bin/curl -fsS --max-time 1 "$health" >/dev/null 2>&1; then
        local pid
        pid=$(lsof -tiTCP:8890 -sTCP:LISTEN 2>/dev/null)
        echo "PDF2zh Server 已在运行：http://127.0.0.1:8890  PID=${pid}"
        return 0
    fi

    echo "正在启动 PDF2zh Server..."

    # 如果 LaunchAgent 尚未载入则载入；已载入时忽略报错
    launchctl bootstrap "$domain" "$plist" >/dev/null 2>&1 || true

    # 启动 / 重启服务
    launchctl kickstart -k "$domain/$label" >/dev/null 2>&1 || true

    # 最多等待 15 秒，不再固定只等 2 秒
    local i
    for i in {1..60}; do
        if /usr/bin/curl -fsS --max-time 1 "$health" >/dev/null 2>&1; then
            local pid
            pid=$(lsof -tiTCP:8890 -sTCP:LISTEN 2>/dev/null)
            echo "PDF2zh Server 启动成功（${i}s）"
            echo "地址：http://127.0.0.1:8890  PID=${pid}"
            return 0
        fi
        sleep 1
    done

    echo "PDF2zh Server 启动失败或健康检查超时。"
    echo "LaunchAgent 状态："
    launchctl print "$domain/$label" 2>/dev/null | \
        grep -E 'state =|pid =|last exit code' || true

    echo
    echo "最近的错误日志："
    tail -n 20 "$errlog" 2>/dev/null || true

    return 1
}


pdf2zh-stop() {
    local domain="gui/$(id -u)"
    local label="com.yuke.pdf2zh"

    launchctl kill SIGTERM "$domain/$label" >/dev/null 2>&1 || true
    launchctl bootout "$domain/$label" >/dev/null 2>&1 || true

    local i
    for i in {1..5}; do
        if ! lsof -tiTCP:8890 -sTCP:LISTEN >/dev/null 2>&1; then
            echo "PDF2zh Server 已停止"
            return 0
        fi
        sleep 1
    done

    local pid
    pid=$(lsof -tiTCP:8890 -sTCP:LISTEN 2>/dev/null)

    echo "PDF2zh Server 未完全停止，8890 端口仍被占用。"
    echo "PID=${pid}"
    return 1
}


pdf2zh-status() {
    local health="http://127.0.0.1:8890/health"
    local pid

    pid=$(lsof -tiTCP:8890 -sTCP:LISTEN 2>/dev/null)

    if /usr/bin/curl -fsS --max-time 1 "$health" >/dev/null 2>&1; then
        echo "PDF2zh Server 正在运行"
        echo "地址：http://127.0.0.1:8890"
        echo "PID=${pid}"
        /usr/bin/curl -fsS "$health"
        echo
        return 0
    fi

    if [[ -n "$pid" ]]; then
        echo "8890 端口存在进程，但 PDF2zh 健康检查未通过。"
        echo "PID=${pid}"
        return 1
    fi

    echo "PDF2zh Server 当前未运行"
    return 1
}


pdf2zh-log() {
    local logfile="$HOME/zotero-pdf2zh/server/pdf2zh-launchd.log"

    touch "$logfile"

    echo "正在持续追踪 PDF2zh 日志"
    echo "日志：$logfile"
    echo "按 Ctrl+C 退出日志查看（不会停止 Server）"
    echo

    tail -n +1 -F "$logfile"
}

# <<< PDF2ZH HELPERS V2 <<<
