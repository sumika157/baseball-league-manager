# Baseball League Manager

野球のリーグ・チーム・選手と打撃／投球成績を管理する Django アプリケーションです。
選手を登録すると、打率・OPS・防御率・WHIP などの指標が自動で計算されて一覧に表示されます。

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

### ホットリロード

ソースコードはコンテナにマウントされているため、**ファイルを編集して保存すると自動でリロードされます**。
コンテナを再起動する必要はありません。

```
baseball-web  | /app/myapp/views.py changed, reloading.
```

---

## ブラウザからのアクセス

コンテナは WSL 内で動いていますが、**Windows のブラウザから `localhost` でそのままアクセスできます**。

| 画面 | URL |
| --- | --- |
| ダッシュボード（ホーム） | http://localhost:8000/ |
| チーム一覧 | http://localhost:8000/teams/ |
| 選手一覧 | http://localhost:8000/team/&lt;チームID&gt;/ |
| 選手の成績編集 | http://localhost:8000/team/&lt;チームID&gt;/player/&lt;選手ID&gt;/edit/ |
| ログイン | http://localhost:8000/accounts/login/ |
| 新規登録 | http://localhost:8000/accounts/signup/ |
| 管理画面 | http://localhost:8000/admin/ |

管理画面には既存の管理ユーザー **`admin`** でログインできます。

---

## よく使うコマンド

`manage.py` のコマンドは、起動中のコンテナに対して `docker compose exec` 経由で実行します。

| 目的 | コマンド |
| --- | --- |
| マイグレーションファイルの作成 | `docker compose exec web python manage.py makemigrations` |
| マイグレーションの適用 | `docker compose exec web python manage.py migrate` |
| マイグレーションの適用状況を確認 | `docker compose exec web python manage.py showmigrations` |
| 管理ユーザーの作成 | `docker compose exec web python manage.py createsuperuser` |
| 設定ミスの検査 | `docker compose exec web python manage.py check` |
| テストの実行 | `docker compose exec web python manage.py test` |
| Django シェル | `docker compose exec web python manage.py shell` |
| コンテナ内のシェルに入る | `docker compose exec web bash` |
| ログを見る | `docker compose logs -f web` |
| 状態を確認する | `docker compose ps` |

### イメージを作り直す

`requirements.txt` を変更したときは、イメージのビルドし直しが必要です。

```bash
docker compose build --no-cache
docker compose up -d
```

---

## VS Code で開発する（Dev Container）

`.devcontainer/` を用意しているため、VS Code からコンテナ内に直接接続して開発できます。
**コンテナ内の Python を参照するので、補完や型チェックがそのまま効きます。**

1. 拡張機能 **Dev Containers** をインストールする
2. コマンドパレット（`F1`）から **「Dev Containers: Reopen in Container」** を選ぶ

初回は自動で Python / Pylance / Django の拡張機能が入ります。

> VS Code を閉じてもコンテナは動き続ける設定（`"shutdownAction": "none"`）です。
> 一緒に停止させたい場合は `.devcontainer/devcontainer.json` を `"stopCompose"` に変更してください。

---

## データベースについて

`db.sqlite3` は**イメージには含めず、ホスト側のファイルをマウントして使っています**。
そのため次のようになります。

- コンテナを作り直しても **データは消えません**
- ホスト側の `db.sqlite3` を直接バックアップできます
- Git 管理外です（`.gitignore` で除外）

バックアップを取る場合は、コンテナを停止してからファイルをコピーします。

```bash
docker compose down
cp db.sqlite3 db.sqlite3.bak
```

---

## トラブルシューティング

### `docker: command not found`（WSL 上で）

Docker Desktop の WSL 統合が無効です。「事前準備」の手順2を実施してください。

### `Cannot connect to the Docker daemon`

Docker Desktop が起動していません。タスクバーのアイコンが「Running」になるまで待ってから再実行します。

### ブラウザで「接続できません」と表示される

コンテナが起動しているか確認します。

```bash
docker compose ps
```

`Up` と表示されない場合はログを確認します。

```bash
docker compose logs web
```

### ポートが既に使われている（`port is already allocated`）

8000 番を別のプロセスが使っています。停止するか、`docker-compose.yml` の `ports` を
`"8001:8000"` のように変更してください。

### ソースを編集してもリロードされない

`docker compose ps` でコンテナが `Up` か確認してください。
それでも反映されない場合はコンテナを再起動します。

```bash
docker compose restart web
```

---

## プロジェクト構成

```
my_django_project/
├── .devcontainer/
│   └── devcontainer.json   # VS Code Dev Container 設定
├── config/                 # プロジェクト設定
│   ├── settings.py         # Django の設定
│   └── urls.py             # ルート URL 定義
├── myapp/                  # アプリ本体
│   ├── domain/             # 業務ルール（Django 非依存）
│   │   ├── value_objects.py    Position / JerseyNumber /
│   │   │                       InningsPitched / BattingLine / PitchingLine
│   │   ├── entities.py         Team / Game（集約ルート）/ Player / League
│   │   ├── repositories.py     永続化のインターフェース
│   │   └── exceptions.py       DomainError
│   ├── application/        # ユースケース
│   │   ├── services.py         TeamApplicationService
│   │   └── dto.py              画面へ渡す読み取り専用データ
│   ├── infrastructure/     # Django ORM への接続
│   │   ├── orm_models.py       Django の Model 定義
│   │   ├── repositories.py     リポジトリ実装とマッピング
│   │   └── queries.py          一覧表示用の参照クエリ
│   ├── presentation/       # HTTP
│   │   ├── views.py            ビュー
│   │   └── forms.py            入力検証
│   ├── models.py           # infrastructure/orm_models.py の再輸出
│   ├── urls.py             # アプリの URL 定義
│   ├── migrations/         # マイグレーション
│   ├── tests/              # 層ごとのテスト
│   ├── static/myapp/css/
│   │   └── theme.css       # Bootstrap に重ねるテーマ層
│   └── templates/          # HTML テンプレート
│       ├── myapp/              画面本体・404・500
│       ├── registration/       ログイン・新規登録・パスワード関連
│       └── admin/              管理画面用の上書き
├── Dockerfile              # イメージ定義
├── docker-compose.yml      # サービス定義（ポート公開・マウント・自動 migrate）
├── .dockerignore           # イメージに含めないファイル
├── .env                    # 環境変数の実値（Git 管理外）
├── .env.example            # .env のテンプレート
├── db.sqlite3              # データベース（Git 管理外）
├── manage.py               # Django のコマンド入口
└── requirements.txt        # 依存パッケージ
```

---

## アーキテクチャ

ドメイン駆動設計（DDD）にもとづき、4つの層に分けています。
**依存の向きは常に内側（ドメイン）へ**で、ドメイン層は Django を一切知りません。

```
presentation  →  application  →  domain  ←  infrastructure
（HTTP・画面）    （ユースケース）  （業務ルール）  （Django ORM）
```

| 層 | ディレクトリ | 責務 |
| --- | --- | --- |
| ドメイン | [myapp/domain/](myapp/domain/) | 野球の語彙とルール。Django 非依存 |
| アプリケーション | [myapp/application/](myapp/application/) | ユースケースの手順と画面用 DTO |
| インフラ | [myapp/infrastructure/](myapp/infrastructure/) | Django ORM、リポジトリ実装、参照用クエリ |
| プレゼンテーション | [myapp/presentation/](myapp/presentation/) | HTTP の解釈とフォーム検証 |

### ドメイン層の中身

| ファイル | 内容 |
| --- | --- |
| `value_objects.py` | `Position` `JerseyNumber` `InningsPitched` `BattingLine` `PitchingLine` `Season` `TeamRecord` |
| `entities.py` | `Team`・`Game`（いずれも集約ルート）・`Player`・`League` |
| `services.py` | ランキングと順位表（誰を対象とし、何で順位づけるか） |
| `repositories.py` | 永続化のインターフェース（実装は infrastructure） |
| `exceptions.py` | `DomainError` とその派生 |

### 画面構成

```
/                        ダッシュボード（ホーム）
├── /games/              試合一覧（年・チームで絞り込み）
│   └── /games/<id>/     試合詳細（出場選手の成績）
├── /standings/          順位表（年で切替）
├── /teams/              チーム一覧
│   └── /team/<id>/      選手一覧（野手／投手を切替）
│       └── .../player/<id>/        選手の個人ページ（通算＋試合ごと）
│           └── .../edit/           選手の基本情報の編集
└── /accounts/...        ログイン・新規登録・パスワード関連
```

実装の進め方は [docs/ROADMAP.md](docs/ROADMAP.md) を参照。

### 並べ替え

選手一覧・チーム一覧・順位表は、見出しを押すと並べ替わります。
並び順は `?sort=home_runs&dir=desc` のように **URL に残る**ので、共有やブックマークができます。

- 「何を基準に並べ替えられるか」と「その列の既定の向き」はドメイン層が持ちます。
  打率や本塁打は多い順、防御率や WHIP は少ない順、といった違いを画面側で覚えずに済ませるためです
- 不正なキーが URL に入っていてもエラーにせず、既定の並びに落とします
- 同値のときは背番号の小さい順で安定させます
- 未登板の投手は、率で並べるとき（防御率・WHIP・K/9）は常に末尾に置きます

### チームの表示順

チームの並びは、管理画面のリーグ編集画面で**行をドラッグして**手動で決められます。
その順序がサイトのチーム一覧とダッシュボードにも反映されます。
ドラッグすると「表示順」の数値が振り直されるので、通常の「保存」で確定します。

### 選手のプロフィール

管理画面の選手編集画面から入力します。すべて任意で、分かっているものだけ埋めます。

| 区分 | 項目 |
| --- | --- |
| プロフィール | 生年月日・投打・身長体重・出身地・入団年 |
| プロ入り前の経歴 | 出身高校・出身大学・出身社会人チーム |

- **年齢は保持せず生年月日から求めます**（保持すると翌年ずれるため）
- プロ入り前の経歴は**入力された区分だけ**を通った順に並べます。高校から
  そのままプロ、大学を経ずに社会人へ、といった順路にも対応します

### 選手の経歴（在籍）を登録する

所属チームと背番号は**在籍**が持ちます。過去の経歴は管理画面から登録します。

1. 管理画面 → **選手** → 対象の選手を開く
2. 下部の **「在籍（経歴）」** に行を追加する
3. チーム・背番号・加入年・退団年を入れて保存する

退団年を空欄にすると「現在も在籍中」になります。まとめて登録したい場合は
管理画面の **「在籍」** から直接追加することもできます。

登録時には次を検査します。

- **期間が重なる同じ背番号は登録できません**（同じチームに同じ番号の選手が
  同時に2人いる状態を防ぐため）。期間が重ならなければ同じ番号を再利用できます
- 退団年が加入年より前にはできません
- 同じチームに同じ年から二重に加入することはできません

成績は選手に紐づくため、移籍しても失われません。

### 試合がすべての出典

**チームの勝敗も選手の通算成績も、テーブルに持ちません。** 試合（`Game`）を登録すると、
そこから集計して求めます。同じ事実の出典を2つ作らないためです。

```
試合を登録  →  スコア        →  チームの勝敗  →  順位
            →  選手の成績    →  通算成績      →  打率・OPS・防御率
```

- 試合は管理画面の「試合」から登録します。1試合ぶんの打撃成績・投球成績も
  そこでインラインで入力します
- 選手編集画面では成績を変更できません。表示は集計結果で、直せるようにすると
  試合の明細と食い違うためです
- 投球回の合計は「アウト数」で足します。`5.2 + 5.2` は `10.4` ではなく `11.1` です
- 率（打率・OPS・防御率）は試合ごとの率を平均せず、合算した実数から計算し直します

### 順位

**順位は保持せず、勝率の高い順で自動的に決まります。** 手入力できるようにすると
勝敗と矛盾しても検知できないためです。

- 勝率は日本プロ野球の規則にならい **勝 ÷ (勝 + 敗)**。引分は分母に含めません
- 勝率が同じチームは同順位として扱います
- ゲーム差は首位との差を `((首位の勝 - 勝) + (敗 - 首位の敗)) ÷ 2` で算出します
- その年の成績が未登録のチームは順位表に載せません
  （0勝0敗として並べると、未登録なのか全敗なのか区別できなくなるため）

ダッシュボードはリーグ全体の概況と、OPS・本塁打・防御率・奪三振のランキングを表示します。
順位づけの規則（未出場の選手を除く、規定打数など）はドメインサービスにあり、
画面を持たなくても単体テストできます。

集約ルートは **`Team` と `Game`** の2つです。試合は2チームにまたがるため、
`Team` の内部には置けず独立した集約になります。

`Team` については「同一チーム内で背番号は重複しない」という不変条件は
チーム全体を見ないと判定できないため、`Team` がロスターを保持して自ら保証します。

### 投球回（InningsPitched）

野球では `5.2` が **5回と2/3**（＝17アウト）を意味し、10進数の 5.2 ではありません。
この変換ルールは `InningsPitched` 値オブジェクトが唯一の出典で、内部では常に
アウト数の整数で保持します。`5.3` のような存在しない表記は `6.0` に正規化されます。

### 更新と参照の分離

- **更新** はリポジトリ経由で集約単位に読み書きします（不変条件を守るため）
- **参照** は一覧表示のように不変条件を扱わないため、集約を組み立てず
  [infrastructure/queries.py](myapp/infrastructure/queries.py) から直接 DTO を作ります

---

## デザイン

Bootstrap 5 の上に薄いテーマ層（[theme.css](myapp/static/myapp/css/theme.css)）を重ねています。
グリッドとユーティリティは Bootstrap のまま使い、配色・余白・タイポグラフィ・角丸だけを上書きします。

- 明るいグレー地に白の面、細い罫線。アクセントは1色に絞る
- 成績表は `tabular-nums` で桁を揃える（データが主役の画面では効果が大きい）
- 表は枠線を持たせず行区切りのみで構成

管理画面にも同じ考え方のテーマ層（[admin-theme.css](myapp/static/myapp/css/admin-theme.css)）を当てています。
Django 5.x の admin は配色を CSS 変数で持っているため、変数を差し替えるだけで大半が変わります。

> admin のライト／ダーク切替に追随させるには、Django 本体と同じ3つの文脈
> （`html[data-theme="light"], :root` / `@media (prefers-color-scheme: dark)` / `html[data-theme="dark"]`）
> すべてで変数を定義する必要があります。どれか1つ欠けると、その状態のときだけ Django 既定色に戻ります。

### テンプレートの優先順位に注意

`INSTALLED_APPS` では **`myapp` を `django.contrib.admin` より前**に置いています。
テンプレートはこの順に検索されるため、後ろにあると
`django.contrib.admin` が持つ `registration/password_*.html` が優先され、
サイト側のパスワード関連画面が管理画面の見た目になってしまいます。

その副作用として管理画面内のパスワード変更までサイト側の見た目になるため、
管理画面用のテンプレートを `templates/admin/` に別名で用意し、
[myapp/admin.py](myapp/admin.py) の `admin.site.password_change_template` で指定しています。

---

## テスト

```bash
docker compose exec web python manage.py test
```

| ファイル | 内容 | DB |
| --- | --- | --- |
| `tests/test_domain_value_objects.py` | 指標計算・投球回の変換・入力値の検証 | 不要 |
| `tests/test_domain_entities.py` | 背番号の一意性・並び順・ポジション変更 | 不要 |
| `tests/test_integration.py` | リポジトリの往復・画面の動作 | 必要 |

ドメイン層のテストは Django の設定すら読み込まずに実行できます。

```bash
docker compose exec -e DJANGO_SETTINGS_MODULE= web \
  python -m unittest myapp.tests.test_domain_value_objects myapp.tests.test_domain_entities
```
