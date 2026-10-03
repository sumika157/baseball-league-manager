---
name: worktree
description: 並行作業のために git worktree を作り、その中のコードを検査・テストし、終わったら片付ける。ブランチを切るときに他の worktree がある・ユーザーが並行作業だと言った・未コミットの無関係な変更があるとき、または「worktree で作業して」「worktree を消して」と頼まれたときに使う。
---

worktree での作業手順。**worktree を使う条件と、罠の理由は `CLAUDE.md`「並行して作業するときは worktree を使う」が出典**で、
ここには Claude Code から実際に叩くコマンドだけを書く。以下、`<名前>` は `.claude/worktrees/` の下のディレクトリ名、
`<ブランチ>` はブランチ名。

## 1. 作る

ブランチは `issue-plan` スキルの手順4（`gh issue develop`）でリモートに作ってある前提。main の作業ツリーで（Bash から）:

```bash
cd "U:/home/sumika/work/develop/my_django_project"
git log --oneline origin/main..main     # 何か出たらユーザーに push を促す（先行分が抜けたブランチになる）
git fetch origin <ブランチ>
git worktree add -b <ブランチ> .claude/worktrees/<名前> origin/<ブランチ>
git config --global --add safe.directory '%(prefix)///wsl.localhost/Ubuntu/home/sumika/work/develop/my_django_project/.claude/worktrees/<名前>'
git -C .claude/worktrees/<名前> status -sb   # dubious ownership が出なければよい
```

- `EnterWorktree` ツールは使えない。cwd は main の作業ツリーに置いたまま、Read / Edit / Write には
  `U:\home\sumika\work\develop\my_django_project\.claude\worktrees\<名前>\...` の絶対パスを渡す。
- **main の作業ツリーのファイルを編集しない。** パスの打ち間違いで main 側を直すと、別セッションの作業に混ざる。
  編集したら `git -C .claude/worktrees/<名前> status --short` に出ることを確かめる。

## 2. 中で検査・テストする

コンテナは main の作業ツリーから叩き、**`-w` で worktree を指す**（省くと main のコードを検査して「通ったのに直っていない」になる）。
`-e` の3つは Makefile の `CACHE_ENV` と同じ（省くと root 所有のキャッシュが worktree に残り、片付けで消せなくなる）。

PowerShell から（パス変換が起きないのでこちらを使う）:

```powershell
$wt = "/app/.claude/worktrees/<名前>"; $ce = @("-e","PYTHONPYCACHEPREFIX=/tmp/pycache","-e","RUFF_CACHE_DIR=/tmp/ruff-cache","-e","MYPY_CACHE_DIR=/tmp/mypy-cache")
docker compose exec -w $wt @ce web sh -c "ruff check . && ruff format --check . && mypy ."            # lint（コミット前に必須）
docker compose exec -w $wt @ce web python manage.py test                                             # フルスイート
docker compose exec -w $wt @ce -e DJANGO_SETTINGS_MODULE= web python -m unittest discover -s myapp/tests/domain -t .   # domain だけ
```

- Bash ツールから叩くときは先頭に `MSYS_NO_PATHCONV=1` を付ける（無いと `-w /app/...` が Windows パスに化けて
  `Cwd must be an absolute path` で失敗する）。
- 個別のテストの指定や結果の読み方は `run-tests` スキル。
- 対象を取り違えていないかは、テストの出力からは分からない。迷ったら `docker compose exec -w $wt web pwd` で
  `/app/.claude/worktrees/<名前>` が出ることを確かめる（コンテナに git は入っていない）。

### e2e・TypeScript の型検査の前に

gitignore された `node_modules`・`dist` は worktree に無い。ビルドはコンテナを新しく作る操作なので **WSL 側から**:

```powershell
wsl -e bash -c "cd /home/sumika/work/develop/my_django_project && make frontend-build WT=<名前>"
wsl -e bash -c "cd /home/sumika/work/develop/my_django_project && make frontend-check WT=<名前>"   # tsc
```

`db.sqlite3` はテストが作るので integration までは不要。

### ブラウザで見たいとき

`runserver`（localhost:8000）が配信するのは main の作業ツリー。worktree の変更はブラウザで見られないので、
確認は自動テスト（必要なら e2e）で行う。

## 3. git 操作

worktree の中の git は Windows 側（Claude Code）から行う（worktree の `.git` に Windows のパスが入るため、WSL のターミナルでは動かない）。

```bash
git -C .claude/worktrees/<名前> add -A
git -C .claude/worktrees/<名前> commit -F <メッセージのファイル>
```

コミットメッセージは複数行になるのでファイルに書いて `-F` で渡す（Bash で PowerShell の here-string を使わない）。
push と PR は `open-pr` スキル。

## 4. 片付ける

PR がマージされたら（`gh pr view <番号> --json state` が `MERGED`）片付ける。マージ前に消さない。

```bash
git -C .claude/worktrees/<名前> status --short   # 何も出ないこと。出たら消す前にユーザーに確かめる
git worktree remove .claude/worktrees/<名前>
git branch -D <ブランチ>                          # スカッシュマージなので -d では消えない
```

- **`Directory not empty` で失敗したら**、コンテナ（root）が作った生成物が残っている。WSL の `sudo` はパスワードを
  要求するので、コンテナ経由で消してからやり直す:
  ```powershell
  docker compose exec web sh -c "cd /app/.claude/worktrees/<名前> && rm -rf .mypy_cache .ruff_cache && find . -name __pycache__ -type d -prune -exec rm -rf {} +"
  ```
- `git worktree remove --force` は中途半端に成功することがある（追跡ファイルだけ消えて登録が残る）。使ったら
  `git worktree list` と `ls .claude/worktrees/` で残骸が無いことを確かめ、登録だけ残っていたら `git worktree prune`。
- **他のセッションの worktree は消さない。** `git worktree list` には並行中の作業も並ぶ。自分が作ったものだけを片付ける。
