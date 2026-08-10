---
name: docker-ops
description: このプロジェクト（Windows上のU:ドライブ経由でWSL2内のリポジトリを触る構成）でDockerコンテナの起動・停止・ログ確認・migrate・シェルアクセスなどの日常操作を行う。「コンテナ起動して」「ログ見せて」「マイグレーションして」のように頼まれたときに使う。
---

このリポジトリの実体は WSL2 内（`/home/sumika/work/develop/my_django_project`）にあり、
Windows からは `U:\`（`\\wsl.localhost\Ubuntu\`）経由で見えている。
**Docker は Windows 側のターミナルから直接叩けるので、`wsl -e bash -c` で包む必要は無い。**
`docker compose build / up / exec / logs / ps` はUNCパス上でもそのまま動作する。

## 日常操作

| 目的 | コマンド |
| --- | --- |
| 起動（フォアグラウンド） | `docker compose up` |
| 起動（バックグラウンド） | `docker compose up -d` |
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

起動時（`docker compose up`）に未適用のマイグレーションは自動で適用される。ソースはマウントされているためホットリロードが効き、コンテナ再起動は基本不要（`myapp/views.py changed, reloading.` のようなログが出る）。

`requirements.txt` を変更した場合のみイメージの再ビルドが必要:

```bash
docker compose build --no-cache
docker compose up -d
```

読み取り専用の確認コマンド（`ps` / `logs` / `check` / `showmigrations` など）は確認なしで実行してよい。`docker compose down` のような状態を壊す操作や、ユーザーデータに影響する操作は実行前に確認する（[[permission-prompts-only-for-writes]]）。

## このプロジェクトで踏んだシェルの罠（[[wsl-windows-shell-gotchas]] 参照）

**Git Bash の MSYS パス変換**
先頭がスラッシュの引数は Windows パスに変換される（`/accounts/login/` → `C:/Program Files/Git/accounts/login/`）。URL パスや `curl -w` の書式を渡すときは `MSYS_NO_PATHCONV=1` を前置する。`/tmp/...` への書き出しも同様に化けるので、このプロジェクトのスクラッチファイルは指定されたスクラッチディレクトリを使う。

**`grep -P` は使えない**
`grep: -P supports only unibyte and UTF-8 locales` で失敗する。`sed -n 's/.*value="\([^"]*\)".*/\1/p'` などPCRE以外の方法で代替する。

**`pkill -f` は自分自身にマッチしうる**
`pkill -f 'manage.py runserver'` は、それを含む `bash -c` のコマンドライン自体にマッチして自滅する（exit 15）。kill は対象パターンの文字列と同居しない独立した呼び出しに分ける。

**`wsl -e bash -c "... &"` のバックグラウンドは即死する**
`wsl` コマンドが返ると子プロセスも終了する。常駐させる必要がある場合は `setsid nohup ... > log 2>&1 < /dev/null & disown` を使う（このプロジェクトの通常運用ではDockerを直接叩くので基本的に不要）。

**Git の dubious ownership**
`//wsl.localhost/...` は所有者チェックに引っかかる場合がある。`git config --global --add safe.directory '%(prefix)///wsl.localhost/Ubuntu/home/sumika/work/develop/my_django_project'` が登録済みのはずなので、`fatal: detected dubious ownership` が出た場合はこの設定が外れていないか確認する。

## 単発スクリプトで `django.test.Client` を使う場合（[[django-adhoc-test-client-host]]）

`manage.py test` の外（`python -c` などの単発確認スクリプト）で `django.test.Client()` を使うと、既定ホスト `testserver` が `ALLOWED_HOSTS` に無く `DisallowedHost` で 400 になる。`Client(SERVER_NAME='localhost')` を指定する。`DEBUG=False` 時の画面を確認する場合は `override_settings(DEBUG=False, ALLOWED_HOSTS=['localhost'])` も併用する。このやり方で作った検証用データ（ユーザー・選手など）は必ず後始末する。
