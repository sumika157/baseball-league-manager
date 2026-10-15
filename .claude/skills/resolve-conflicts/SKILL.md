---
name: resolve-conflicts
description: 開いている PR が base（main・epic）とコンフリクトしたときに、最新の base を merge で取り込んで解消し、lint とフルテストを通して push する。「PR #N のコンフリクトを直して」と頼まれたとき、自分の PR のマージを待つ間にコンフリクトに気づいたとき（open-pr の手順5）、または1時間ごとの見回り（scripts/resolve_conflicts.ps1）から起動されたときに使う。
---

コンフリクトした PR を直す手順（#267）。**rebase と force push はしない**。PR のブランチは別のセッションの worktree で
開かれていることが多いので、その worktree には触らず、一時的な作業場所で最新の base を merge で取り込み、追加だけの push にする
（相手が後から push すると non-fast-forward で弾かれるので、相手の作業は消えない。スカッシュマージなので main には1コミットで入る）。

以下、PowerShell の例は毎回この行から始める:

```powershell
$gh = "C:\Users\sumik\AppData\Local\Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\bin\gh.exe"
```

git は main の作業ツリー（`U:\home\sumika\work\develop\my_django_project`）から `git -C .claude/worktrees/conflict-<PR番号> ...` の形で叩く
（手元のフックはセッションの作業ディレクトリでブランチを判定するので、`cd` してからのコミットは main へのコミットと誤って止まる）。

## 1. 直す対象か確かめる

```powershell
& $gh pr view <PR番号> --json state,isDraft,mergeable,headRefName,baseRefName,headRefOid,title
```

- `state` が `OPEN`、`isDraft` が false、`mergeable` が `CONFLICTING` のときだけ続ける。`UNKNOWN` は GitHub が計算中なので、少し置いて見直す。
- フォークからの PR（`isCrossRepository`）は対象外。
- **持ち主のセッションが作業中なら手を出さない**（自分の PR を自分で直すときは除く。#270）。`git worktree list` でそのブランチを開いている
  worktree があり、push していないコミットがある（`git -C <その worktree> rev-list --count origin/<head>..HEAD` が 1 以上）か、
  HEAD やファイルが直近30分のうちに動いているなら、持ち主が載せ直しの途中かもしれない。割り込むと持ち主の push が弾かれて二度手間になるので、
  何もせずに終わる（まだコンフリクトしていれば、次の見回りが見直す）。

## 2. 一時的な作業場所で取り込む

前の実行が途中で落ちて `.claude/worktrees/conflict-<PR番号>` が残っていたら、先に手順6で消してから始める。

```bash
cd "U:/home/sumika/work/develop/my_django_project"
git fetch origin <base> <head>
git worktree add --detach .claude/worktrees/conflict-<PR番号> origin/<head>
git config --global --add safe.directory '%(prefix)///wsl.localhost/Ubuntu/home/sumika/work/develop/my_django_project/.claude/worktrees/conflict-<PR番号>'
git -C .claude/worktrees/conflict-<PR番号> merge --no-ff --no-commit origin/<base>
git -C .claude/worktrees/conflict-<PR番号> diff --name-only --diff-filter=U   # コンフリクトしたファイル
```

## 3. 解消する（判断が要るものは直さない）

コンフリクトした箇所ごとに、PR 側の意図（`& $gh pr view <PR番号>`・`& $gh pr diff <PR番号>`・Issue）と、base 側で入った変更
（`git log origin/<head>..origin/<base> -- <ファイル>` とその PR）を読んでから直す。

- **直してよい**: 両方の変更が独立していて、両方を残せば済む（同じ import 文・一覧・表への追記、隣り合う別の関数、Wiki の別の行など）。
  CLAUDE.md の規則（出典の一元化・層の向き）に反する形にしない。
- **直さずに止める**: 同じ処理・同じ規則を両方が別の形に変えていて、どちらを採るかに判断が要る / マイグレーションの番号がぶつかった
  （番号の振り直しは持ち主が行う）/ テストの期待値がぶつかった / 解消の結果を自分で説明できない。
  止めるときは `git -C ... merge --abort` し、PR に「どのファイルのどこが、どの PR の変更とぶつかったか」をコメントして、手順6の片付けへ進む。

解消したら:

```bash
git -C .claude/worktrees/conflict-<PR番号> add -A
git -C .claude/worktrees/conflict-<PR番号> commit -F <メッセージのファイル>
```

コミットメッセージは「<base> を取り込み、コンフリクトを解消した（#<PR番号>）」の1行と、ぶつかったファイルと解消のしかたを1行ずつ。
末尾に、その会話で指示された Co-Authored-By の行。

## 4. 確かめる

`worktree` スキルの手順2のとおり、`-w /app/.claude/worktrees/conflict-<PR番号>` と CACHE_ENV の `-e` を付けて流す:
lint（`ruff check .`・`ruff format --check .`・`mypy .`）と、フルテスト（domain + integration を `--parallel 4`、frontend-build の後に e2e）。
マイグレーションのファイルがぶつかったときは止める（手順3）。`makemigrations --check --dry-run` も流し、変更が出たら止める。

落ちたら、取り込みのせいか（base 側だけでも落ちるか）を確かめ、取り込みのせいなら直す。直せないときは手順3と同じく止める。

## 5. push してコメントする

```bash
git -C .claude/worktrees/conflict-<PR番号> push origin HEAD:<head>
```

- fast-forward だけ。**`--force` も `--force-with-lease` も付けない**。弾かれたら、その間に持ち主が push している。手順6で片付けて終わる
  （まだコンフリクトしていれば、次の見回りが新しい先端で試し直す）。
- PR に、ぶつかったファイルと解消のしかた、確かめたこと（lint・テストの件数）をコメントする。
- 自動マージが予約されていれば、CI が通ったところでマージされる。予約されていない PR は、そのまま持ち主かユーザーのマージを待つ。

## 6. 片付ける（止めたときも必ず）

```powershell
docker compose exec web sh -c "cd /app/.claude/worktrees/conflict-<PR番号> && rm -rf db.sqlite3 frontend/node_modules myapp/static/myapp/dist .mypy_cache .ruff_cache && find . -name __pycache__ -type d -prune -exec rm -rf {} +"
git worktree remove .claude/worktrees/conflict-<PR番号>
git worktree prune
```

ブランチを持たない worktree は1時間ごとの片付けが消さないので、ここで消さないと残り続ける。

## 持ち主のセッションが続きをやるとき

自分の worktree のブランチは、push された取り込みの分だけ遅れている。続けてコミットする前に
`git -C .claude/worktrees/<名前> merge --ff-only origin/<head>` で追いつく（追いつかずに push すると non-fast-forward で弾かれる）。
