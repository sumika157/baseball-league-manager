---
name: docker-ops
description: このプロジェクト（Windows上のU:ドライブ経由でWSL2内のリポジトリを触る構成）でDockerコンテナの起動・停止・ログ確認・migrate・シェルアクセスなどの日常操作を行う。「コンテナ起動して」「ログ見せて」「マイグレーションして」のように頼まれたときに使う。
---

コンテナ操作の手順。**Windows と WSL の使い分け・再ビルドが必要な条件は `CLAUDE.md`（実行環境）が出典**で、
ここには手順だけを書く（同じ事実を2か所に置かない）。

## 起動（コンテナを作る操作だけは WSL 側から）

`docker compose up` / `run` は WSL 側で実行する。Windows 側（`U:\`）から作るとバインドマウントが
実体と切り離される（症状と理由は CLAUDE.md）。

```bash
wsl -e bash -c "cd /home/sumika/work/develop/my_django_project && docker compose up -d"
```

`ERROR: CreateProcessParseCommon: Failed to translate U:\...` が出るが、`cd` で Linux パスへ
移っているので実害は無く、マウントは正しく張られる。マウントを疑うときは
`docker compose exec web df -h /app` がプロジェクト実体（1TB のディスク）と一致するかを見る。

## 日常操作（Windows 側からそのまま叩ける）

| 目的 | コマンド |
| --- | --- |
| 停止 | `docker compose down` |
| 状態確認 | `docker compose ps` |
| ログ確認 | `docker compose logs -f web` |
| マイグレーション作成 | `docker compose exec web python manage.py makemigrations` |
| マイグレーション適用 | `docker compose exec web python manage.py migrate` |
| マイグレーション状況確認 | `docker compose exec web python manage.py showmigrations` |
| 設定ミス検査 | `docker compose exec web python manage.py check` |
| 管理ユーザー作成 | `docker compose exec web python manage.py createsuperuser` |
| Djangoシェル | `docker compose exec web python manage.py shell` |
| コンテナ内シェル | `docker compose exec web bash` |

起動時に未適用のマイグレーションは自動で適用される。ソースはマウントされているためホットリロードが
効き、コンテナ再起動は基本不要（`myapp/views.py changed, reloading.` のようなログが出る）。

イメージの再ビルドが必要になる条件は CLAUDE.md（実行環境）にある。手順は build を Windows 側で行い、
**起動だけ WSL 側に戻す**:

```bash
docker compose build --no-cache
wsl -e bash -c "cd /home/sumika/work/develop/my_django_project && docker compose up -d"
```

読み取り専用の確認コマンド（`ps` / `logs` / `check` / `showmigrations` など）は確認なしで実行してよい。
`docker compose down` のような状態を壊す操作や、ユーザーデータに影響する操作は実行前に確認する
（[[permission-prompts-only-for-writes]]）。

## このプロジェクトで踏んだシェルの罠（[[wsl-windows-shell-gotchas]] 参照）

**Git Bash の MSYS パス変換**
先頭がスラッシュの引数は Windows パスに変換される（`/accounts/login/` → `C:/Program Files/Git/accounts/login/`）。URL パスや `curl -w` の書式を渡すときは `MSYS_NO_PATHCONV=1` を前置する。`/tmp/...` への書き出しも同様に化けるので、このプロジェクトのスクラッチファイルは指定されたスクラッチディレクトリを使う。

**Bash ツールで PowerShell の here-string を使わない**
`git commit -m @'...'@` は Git Bash では here-string にならず、`@` が本文に混ざったコミットができる（エラーにならない）。複数行のメッセージはファイルに書いて `git commit -F <file>` で渡す。

**`grep -P` は使えない**
`grep: -P supports only unibyte and UTF-8 locales` で失敗する。`sed -n 's/.*value="\([^"]*\)".*/\1/p'` などPCRE以外の方法で代替する。

**`pkill -f` は自分自身にマッチしうる**
`pkill -f 'manage.py runserver'` は、それを含む `bash -c` のコマンドライン自体にマッチして自滅する（exit 15）。kill は対象パターンの文字列と同居しない独立した呼び出しに分ける。

**`wsl -e bash -c "... &"` のバックグラウンドは即死する**
`wsl` コマンドが返ると子プロセスも終了する。常駐させる必要がある場合は `setsid nohup ... > log 2>&1 < /dev/null & disown` を使う（通常運用ではDockerを直接叩くので基本的に不要）。

**Git の dubious ownership**
`//wsl.localhost/...` は所有者チェックに引っかかる場合がある。`git config --global --add safe.directory '%(prefix)///wsl.localhost/Ubuntu/home/sumika/work/develop/my_django_project'` が登録済みのはずなので、`fatal: detected dubious ownership` が出た場合はこの設定が外れていないか確認する。

## 単発スクリプトで `django.test.Client` を使う場合

`Client(SERVER_NAME='localhost')` が必要（理由は CLAUDE.md の既知の罠）。`DEBUG=False` 時の画面を
確認する場合は `override_settings(DEBUG=False, ALLOWED_HOSTS=['localhost'])` も併用する。
[[django-adhoc-test-client-host]]
