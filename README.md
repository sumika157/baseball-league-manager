# Baseball League Manager

野球のリーグ・チーム・選手と、試合ごとの打撃／投球／守備成績を管理する Django アプリケーションです。
試合のスコアブックを1打席ずつ付けると、得点・勝敗・通算成績・順位・打率や防御率などの指標が自動で集計されます。

**このプロジェクトは Docker 上で動作します。** ローカルに Python や仮想環境を用意する必要はありません。

---

## 動作環境

| 項目 | バージョン / 場所 |
| --- | --- |
| OS | Windows 11 + WSL2 (Ubuntu) |
| Docker | 25.0.2 |
| Docker Compose | v2.24.3 |
| Python | 3.10（コンテナ内） |
| Django | 5.2.10 |
| Node.js | 22（`frontend` コンテナ内。ホストには不要） |
| React / Vite | 19 / 6（`frontend/package.json` で完全固定） |
| データベース | SQLite (`db.sqlite3`) |
| WSL 上のパス | `/home/sumika/work/develop/my_django_project` |
| Windows 上のパス | `U:\home\sumika\work\develop\my_django_project` |

> `U:` ドライブは `\\wsl.localhost\Ubuntu\` に割り当てられています。
> エディタからは Windows パスで開けますが、**コマンドの実行は WSL のターミナルから行います**。

---

## 事前準備（初回のみ）

1. **Docker Desktop を起動する**
   タスクバーのクジラのアイコンが「Running」になるまで待ちます。
2. **WSL 統合を有効にする**
   Docker Desktop の `Settings` → `Resources` → `WSL Integration` を開き、
   `Ubuntu` のトグルを ON にして `Apply & restart` を押します。

準備できたか確認します。WSL のターミナルで次を実行し、バージョン番号が表示されれば完了です。

```bash
docker info --format '{{.ServerVersion}}'
```

3. **`.env` を作成する**
   `SECRET_KEY` などの設定は環境変数で渡すため、`.env` が必要です。

```bash
cd ~/work/develop/my_django_project
cp .env.example .env

# SECRET_KEY を生成して .env の該当行を書き換える
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

`.env` は Git 管理外なので、リポジトリには公開されません。

---

## 環境変数

設定値は `.env` から読み込まれます（`docker-compose.yml` の `env_file`）。

| 変数名 | 必須 | 既定値 | 説明 |
| --- | --- | --- | --- |
| `DJANGO_SECRET_KEY` | ✅ | なし | 暗号署名に使う秘密鍵。未設定だと起動時にエラーになります |
| `DJANGO_DEBUG` | | `False` | 開発時は `True`。本番では必ず `False` |
| `DJANGO_ALLOWED_HOSTS` | | `localhost,127.0.0.1` | アクセスを許可するホスト名（カンマ区切り） |

`DJANGO_SECRET_KEY` を設定せずに起動すると、次のエラーで停止します。

```
django.core.exceptions.ImproperlyConfigured: 環境変数 DJANGO_SECRET_KEY が設定されていません。
```

> **`.env` の値に `$` を含めないでください。**
> Docker Compose が `.env` 内の `$xxx` を変数として展開してしまい、
> `SECRET_KEY` が壊れた状態で Django に渡ります。エラーにならないため気づきにくい問題です。
> `python3 -c "import secrets; print(secrets.token_urlsafe(64))"` で生成すれば `$` は含まれません。

---

## 起動と停止

WSL のターミナルを開いて実行します。

```bash
cd ~/work/develop/my_django_project

# 起動（初回はイメージのビルドが走ります）
docker compose up

# バックグラウンドで起動する場合
docker compose up -d

# 停止
docker compose down
```

起動時には**未適用のマイグレーションが自動で適用されてから**サーバーが立ち上がります。
そのため `docker compose up` だけで常に最新の DB 状態になります。

```
baseball-web  | Operations to perform:
baseball-web  |   Apply all migrations: admin, auth, contenttypes, myapp, sessions
baseball-web  | Running migrations:
baseball-web  |   No migrations to apply.
baseball-web  | Starting development server at http://0.0.0.0:8000/
```

フォアグラウンド（`-d` なし）で起動した場合は `Ctrl + C` で停止できます。

---

## ブラウザからのアクセス

コンテナは WSL 内で動いていますが、**Windows のブラウザから `localhost` でそのままアクセスできます**。

| 画面 | URL |
| --- | --- |
| ダッシュボード（ホーム） | http://localhost:8000/ |
| チーム一覧 | http://localhost:8000/teams/ |
| 選手一覧 | http://localhost:8000/team/&lt;チームID&gt;/ |
| 選手の基本情報の編集 | http://localhost:8000/team/&lt;チームID&gt;/player/&lt;選手ID&gt;/edit/ |
| ログイン | http://localhost:8000/accounts/login/ |
| 新規登録 | http://localhost:8000/accounts/signup/ |
| 管理画面 | http://localhost:8000/admin/ |

管理画面には既存の管理ユーザー **`admin`** でログインできます。

---

## ドキュメント

画面の使い方・成績の仕様・アーキテクチャ・開発の手順は **[Wiki](https://github.com/sumika157/baseball-league-manager/wiki)** にまとめています。

| 読み手 | 入口 |
| --- | --- |
| アプリを使う人 | [画面の歩き方](https://github.com/sumika157/baseball-league-manager/wiki/画面の歩き方)・[試合の記録](https://github.com/sumika157/baseball-league-manager/wiki/試合の記録)・[成績と順位の見方](https://github.com/sumika157/baseball-league-manager/wiki/成績と順位の見方) |
| 開発する人 | [開発環境](https://github.com/sumika157/baseball-league-manager/wiki/開発環境)・[アーキテクチャ](https://github.com/sumika157/baseball-league-manager/wiki/アーキテクチャ)・[テストと品質](https://github.com/sumika157/baseball-league-manager/wiki/テストと品質) |

Wiki の原稿は [docs/wiki/](docs/wiki/) にあり、main に入ると GitHub Actions が Wiki へ反映します。
**Wiki の画面では編集せず、`docs/wiki/` を直して PR を出してください**（画面で直した内容は次の反映で上書きされます）。

開発の規則は [CLAUDE.md](CLAUDE.md)、実装の進め方は [docs/ROADMAP.md](docs/ROADMAP.md) を参照してください。
