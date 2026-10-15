"""worktree の変更を、マージ前にブラウザで確かめるためのプレビューサーバー（#220）。

web コンテナの中で、worktree を作業ディレクトリにして実行する（Makefile の preview・preview-stop・preview-list）:

    docker compose exec -w /app/.claude/worktrees/<名前> web python /app/scripts/preview.py start
    docker compose exec -w /app/.claude/worktrees/<名前> web python /app/scripts/preview.py stop
    docker compose exec web python /app/scripts/preview.py list

- ポートは PORTS から空いているものを選ぶ（docker-compose.yml がホストへ公開している範囲と同じ）。
- DB は worktree の db.sqlite3。無ければ main の db.sqlite3 を写してから migrate する（main のデータは変えない）。
- localhost のクッキーはポートで分かれないので、セッションのクッキー名をポートごとに変える
  （同じ名前だと、プレビューでログインしたときに localhost:8000 のログインが切れる）。
- コンテナに ps が無いので、動いているプレビューは /proc から探す。
"""

import contextlib
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PORTS = range(8001, 8010)
WORKTREES = Path("/app/.claude/worktrees")
MAIN_DB = Path("/app/db.sqlite3")


def say(message: str) -> None:
    """利用者への表示（このスクリプトは結果を画面に出すのが役目なので、print ではなくここに寄せる）。"""
    sys.stdout.write(f"{message}\n")


def running() -> dict[int, tuple[int, Path]]:
    """動いているプレビューを {pid: (ポート, 作業ディレクトリ)} で返す（自動再読み込みの親子の両方が入る）。"""
    found: dict[int, tuple[int, Path]] = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / "cmdline").read_bytes().decode(errors="replace").split("\0")
            cwd = Path(os.readlink(proc / "cwd"))
        except OSError:
            continue
        if "runserver" not in args:
            continue
        for arg in args:
            host, _, port = arg.rpartition(":")
            if host == "0.0.0.0" and port.isdigit() and int(port) in PORTS:
                found[int(proc.name)] = (int(port), cwd)
    return found


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def wait_until_listening(port: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def worktree_dir() -> Path:
    cwd = Path.cwd()
    if cwd.parent != WORKTREES:
        sys.exit(f"worktree の中で実行してください（-w {WORKTREES}/<名前>）。今の作業ディレクトリ: {cwd}")
    return cwd


def start() -> None:
    cwd = worktree_dir()
    for port, where in running().values():
        if where == cwd:
            say(f"既に動いています: http://localhost:{port}/")
            return

    db = cwd / "db.sqlite3"
    if not db.exists():
        # main の runserver が書き込み中でも壊れた写しにならないよう、ファイルの複写ではなくバックアップで写す
        with contextlib.closing(sqlite3.connect(MAIN_DB)) as source, contextlib.closing(sqlite3.connect(db)) as copy:
            source.backup(copy)
        say(f"main の DB を写しました: {db}")
    env = {**os.environ, "PYTHONPYCACHEPREFIX": "/tmp/pycache"}
    subprocess.run([sys.executable, "manage.py", "migrate", "--noinput"], cwd=cwd, env=env, check=True)
    if not (cwd / "myapp/static/myapp/dist").exists():
        say("注意: React の画面の成果物がありません。先に make frontend-build WT=<名前> を実行してください。")

    free = [port for port in PORTS if port_is_free(port)]
    if not free:
        last = PORTS.stop - 1
        sys.exit(f"空いているポートがありません（{PORTS.start}〜{last}）。make preview-list で確かめてください。")
    port = free[0]
    log = Path(f"/tmp/preview-{cwd.name}.log")
    with log.open("w") as out:
        subprocess.Popen(
            [sys.executable, "manage.py", "runserver", f"0.0.0.0:{port}"],
            cwd=cwd,
            env={**env, "DJANGO_SESSION_COOKIE_NAME": f"sessionid_{port}"},
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            # docker exec が終わっても残るよう、別のセッションにする
            start_new_session=True,
        )
    if not wait_until_listening(port, 30):
        sys.exit(f"起動しませんでした。ログ: {log}")
    say(f"プレビュー: http://localhost:{port}/  （ログ: {log}）")


def stop() -> None:
    cwd = worktree_dir()
    pids = [pid for pid, (_, where) in running().items() if where == cwd]
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    say(f"止めました（{len(pids)} プロセス）" if pids else "動いているプレビューはありません")


def show() -> None:
    previews = sorted(set(running().values()))
    if not previews:
        say("動いているプレビューはありません")
    for port, where in previews:
        say(f"http://localhost:{port}/  {where.name}")


def main() -> None:
    commands = {"start": start, "stop": stop, "list": show}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit("使い方: python /app/scripts/preview.py start|stop|list")
    commands[sys.argv[1]]()


if __name__ == "__main__":
    main()
