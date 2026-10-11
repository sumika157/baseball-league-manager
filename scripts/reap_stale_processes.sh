#!/bin/sh
# コンテナの中で、docker exec で始めたまま置き去りになったテスト・検査・e2e のプロセスを止める。
# scripts/cleanup.ps1 が web・frontend のコンテナで実行する（コンテナに ps が無いので /proc を読む）。
#
# 使い方: sh reap_stale_processes.sh <寿命（秒）> [apply]
#   apply を付けないときは対象を表示するだけ。
#   出力は1行に1件「<pid> <経過秒> <コマンド>」。
#
# 対象: PID 1 の子孫ではない（＝ compose が起動したサービス本体ではない）プロセスのうち、
#   コマンドがテスト・検査・e2e のもので、寿命を超えたもの。その子孫もまとめて止める。
#   VS Code Server（devcontainer）や手で開いたシェルはコマンドが一致しないので対象外。
set -u

max_age=$1
mode=${2:-}

hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
up=$(cut -d' ' -f1 /proc/uptime)
up=${up%%.*}

# 全プロセスを「pid ppid 経過秒 コマンド」の形で集める
table=$(
    for d in /proc/[0-9]*; do
        stat=$(cat "$d/stat" 2>/dev/null) || continue
        # NUL 区切りで、改行を含むこともある（compose の command の折り返し）ので、どちらも空白にする
        cmd=$(tr '\000\n' '  ' < "$d/cmdline" 2>/dev/null)
        [ -n "$cmd" ] || continue
        # 2列目のコマンド名は空白や括弧を含みうるので、最後の ") " より後ろだけを区切る
        # （そこから数えて 2 番目が ppid、20 番目が起動時刻）
        # shellcheck disable=SC2086
        set -- ${stat##*) }
        echo "${d#/proc/} $2 $((up - ${20} / hz)) $cmd"
    done
)

targets=$(printf '%s\n' "$table" | awk -v max="$max_age" -v self="$$" '
    $1 ~ /^[0-9]+$/ {
        pid = $1; ppid[pid] = $2; age[pid] = $3
        c = $0; sub(/^[0-9]+ [0-9]+ -?[0-9]+ /, "", c); cmd[pid] = c
        pids[++n] = pid
    }
    # PID 1（compose が起動したサービス本体）の子孫か
    function under_init(p,    i) {
        for (i = 0; i < 64 && p != "" && p != 0; i++) {
            if (p == 1) return 1
            p = ppid[p]
        }
        return 0
    }
    END {
        for (i = 1; i <= n; i++) {
            p = pids[i]
            if (p == self || under_init(p) || age[p] <= max) continue
            if (cmd[p] ~ /vscode-server|\.vscode/) continue
            if (cmd[p] ~ /manage\.py|unittest|playwright|chrom|mypy|ruff|vite|npm|npx|tsc/) mark[p] = 1
        }
        # 対象の子孫もまとめて止める
        do {
            changed = 0
            for (i = 1; i <= n; i++) {
                p = pids[i]
                if (!(p in mark) && p != self && (ppid[p] in mark)) { mark[p] = 1; changed = 1 }
            }
        } while (changed)
        for (i = 1; i <= n; i++) if (pids[i] in mark) print pids[i], age[pids[i]], cmd[pids[i]]
    }
')

[ -n "$targets" ] || exit 0
printf '%s\n' "$targets"
[ "$mode" = apply ] || exit 0

pids=$(printf '%s\n' "$targets" | cut -d' ' -f1)
# shellcheck disable=SC2086
kill -TERM $pids 2>/dev/null
sleep 5
# shellcheck disable=SC2086
kill -KILL $pids 2>/dev/null
exit 0
