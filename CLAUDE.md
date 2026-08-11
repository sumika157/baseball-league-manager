# プロジェクト規則

野球リーグ管理の Django アプリ。全体像・画面構成・セットアップは [README.md](README.md) を参照。
ここには**作業時に必ず守る規則**だけを書く。

## 実行環境

- **すべてのコマンドは Docker コンテナ経由**で実行する（`docker compose exec web python manage.py ...`）。ホストに Python 環境は無い。
- リポジトリの実体は WSL2 内。Windows からは `U:\` 経由で見えるが、**実行中コンテナへの操作（`exec` / `logs` / `ps`）は Windows 側から直接叩ける**。`wsl -e bash -c` で包まない。
- ただし**コンテナを新規作成する操作（`docker compose up` / `run`）だけは WSL 側から実行する**。Windows 側から作るとバインドマウントが実体と切り離され、空マウントや容量 127MB の幽霊ディスク（書き込みが実リポジトリに届かない・`ENOSPC` で死ぬ）になる。
- テスト: `docker compose exec web python manage.py test`（フル）。domain 層のみなら
  `docker compose exec -e DJANGO_SETTINGS_MODULE= web python -m unittest discover -s myapp/tests/domain -t .`（DB 不要・最速）。
- `.env` の値に `$` を含めない（Docker Compose が変数展開して壊す。エラーにならず気づけない）。
- `requirements.txt`・`requirements-dev.txt` は**間接依存まで `==` でバージョン固定**する（現状の方針を維持）。どちらかを変更したら `docker compose build --no-cache` でイメージの再ビルドが必要。

## アーキテクチャ規則（DDD）

依存の向きは常に内側（domain）へ: `presentation → application → domain ← infrastructure`

- **`myapp/domain/` は Django を一切 import しない。** domain 層のテストは Django 設定なしで通ること。
- application は domain のインターフェース（`domain/repositories.py`）越しに永続化を使う。`infrastructure/orm_models.py` を直接 import しない。
- presentation（views）は application 経由で操作する。ORM モデルやリポジトリ実装を直接触らない。
- **更新と参照を分ける**: 更新はリポジトリ経由で集約単位（`Team` / `Game`）に読み書きする。一覧表示などの参照は `infrastructure/queries.py` から直接 DTO を作る（集約を組み立てない）。参照クエリのインターフェースは `application/queries.py`（戻り値が DTO のため domain には置けない）。
- **依存の組み立ては `presentation/views.py` の `build_service()` だけ。** 呼ぶ側ごとに一部の依存だけを渡さない。渡し忘れが「開く画面によって落ちるサービス」になる（管理画面のテンプレートタグで実際に起きた）。テストも `tests/helpers.py` 経由でここを呼ぶ。
- **層をまたぐ受け渡しに素の `dict` を使わない。** application が presentation に返す形は `application/dto.py` の dataclass にする。文字列キーの dict は綴りを間違えても静的検査が黙る。`get_game_edit_data` と `_player_index` は dict のまま残っているが、**新しく増やさない**。触ったついでに DTO へ寄せる。
- **`TeamApplicationService` は既に約50メソッド・1,500行**あり、チーム・選手・試合・リーグ・管理画面の概況を1クラスで抱えている。ここへ足す前に、対象ごとの別サービスに置けないか考える。分ける判断は選択肢としてユーザーに提示する。

## 同じ事実の出典を2つ作らない

- **チームの勝敗・選手の通算成績・順位はテーブルに保持しない。** すべて `Game` から集計する。集計結果をフィールドやキャッシュとして保存する変更は入れない。
- **年齢は保持しない。** 生年月日から算出する。
- 投球回の変換（`5.2` = 5回2/3 = 17アウト）は `InningsPitched` 値オブジェクトが唯一の出典。他の場所に再実装しない。率（打率・防御率など）は試合ごとの率を平均せず、合算した実数から計算し直す。
- 選択肢の一覧（球場の屋根種別など）はドメインの値オブジェクトが唯一の出典。画面やモデルに複製しない。
- **成績のカウント項目は値オブジェクト（`BattingLine` / `PitchingLine`）のフィールドが出典。** 永続化（`_BATTING_FIELDS`）・入力フォーム（`STAT_FIELDS`）・React（`frontend/src/game_edit/types.ts`）の列挙はそれに従う。TypeScript から Python を読めないためこの重複だけは消せないので、`tests/integration/test_stat_fields.py` が突き合わせる。**項目を増やすときはこの4か所を同じコミットで直す**（ずれても例外にならず、その項目だけ保存されない・入力欄が出ないという静かな不具合になる）。

## 不変条件は集約が守る

背番号の一意性（期間が重なる同番号の禁止）、在籍期間の重複禁止などは `Team` 集約が自身で検査する。
ORM に直接 `bulk_create` 等で書き込むコード（データ投入コマンドなど）は、集約の検査を素通りするため自分で同じ検査を行うこと。

## 型と静的検査

- **`domain` / `application` / `infrastructure` の関数は引数・戻り値に型注釈を付ける**（`disallow_untyped_defs` が効いている）。注釈の無い関数は **mypy の検査対象から外れ、中身がどれだけ間違っていても黙って通る**。`services.py` の `__init__` に注釈を付けただけで、同ファイルから29件の指摘が出た前例がある。`presentation` は `request` など注釈しにくい引数が多いため対象外。
- 注釈が付けにくいときに `Any` や `# type: ignore` で通すのは、**その関数が本当に型を選ばない場合だけ**（例: 何が来ても int に直す `_require_non_negative`）。理由をコメントに書く。`# type: ignore` は `[misc]` のようにコードまで書く。
- **同じスコープで、型の違う値に同じ変数名を使わない。** mypy は最初の代入で型を固定するため、2つ目以降が検査されない・誤った指摘になる。前例: ボックススコアで打撃と投球のループ変数をどちらも `entry` / `line` にしていた（`outing` / `pitched` に改名して解消）。
- 保存済みの集約から取り出す id（型は `int | None`）は `application/services.py` の `_saved_id()` を通す。内包表記の中でも同じ書き方で済む。

## テスト

- **テストは層ごとのディレクトリに置く**: 業務ルールは `tests/domain/`（DB 不要・Django 非依存で、Django 設定を読み込まずに通ること）、画面の動作・リポジトリの往復・フォーム検証・テンプレート検査は `tests/integration/`。
- **ディレクトリの中は対象ごとのファイルに分ける。** 既存の大きいファイルに足し続けない（`test_integration.py` が 3,975行・47クラスまで膨らんで分割した前例がある）。目安として1ファイル600行を超えたら分ける。結合テストの共通の土台は `tests/integration/base.py` の `BaseCase`。
- **実ブラウザでの確認だけを E2E**（`tests/e2e/`、Playwright + `StaticLiveServerTestCase`）に置く。対象は主要導線のスモークと、JS・CSS が絡んで integration テストでは検証できないもの。業務ルールや画面のロジックは domain / integration 側で検証し、E2E に寄せない（遅く壊れやすいため）。
- **バグを修正したら、同じコミットに再発防止テストを添える**（前例: テンプレートのコメント漏れを検査する `tests/integration/test_templates.py`）。どの層のバグかに応じて上記の置き場所に従う。
- コミット前に `ruff check .`・`ruff format .`・`mypy .`（いずれもコンテナ内）を通す。設定は `pyproject.toml` が唯一の出典。**`# noqa` で黙らせる前に指摘のとおり直す**（それでも黙らせるなら理由をコメントに残す）。

## マイグレーションとデータ

- **適用済み（コミット済み）のマイグレーションは編集しない。** 起動時に自動 migrate される運用のため、履歴が壊れるとどの環境でも起動しなくなる。直したい場合は新しいマイグレーションを追加する。
- **データ移行（`RunPython`）はスキーマ変更と別ファイルに分ける**（前例: `0022` でフィールド追加 → `0024` で backfill）。
- 既存データの一括更新はマイグレーションで行う。`seed_virtual_players.py` などの管理コマンドは**追加専用**とし、既存レコードの更新と責務を混ぜない。
- 一括削除やマイグレーションのロールバックなど**破壊的なデータ操作の前は、コンテナを停止して `db.sqlite3` をコピーしてバックアップ**を取る。

## UI・設計方針

- **両立しない操作を同じ画面に並べない。** 片方を無効化して案内文で繕う前に、片方を消せないか考える。消す判断は選択肢としてユーザーに提示する。
- 書き込みの導線（登録フォーム・編集ボタン）は、ログインしていない人には表示しない。押しても弾かれるだけの導線を見せない。
- 読み書きが同じ URL に同居する画面は、GET は誰でも通し POST だけログインを求める（画面ごと `login_required` にしない）。
- 並べ替えのキーと既定の向きはドメイン層が持つ。不正なソートキーはエラーにせず既定の並びに落とす。

## 既知の罠（踏み直さない）

- **SQLite + `prefetch_related` の多段リレーション**: 関連行が1000件を超えると OR 連結クエリになり `Expression tree is too large` で落ちる。`Prefetch(..., queryset=...select_related(...))` で JOIN にまとめる（回避例: `infrastructure/repositories.py` の `DjangoTeamRepository`）。
- **テンプレートのコメント**: `{# ... #}` は単一行専用。複数行にまたがると中身がそのまま画面に出る（エラーにならない）。複数行は `{% comment %}` を使い、必ず `{% extends %}` より後に置く。検査は `tests/integration/test_templates.py` にある。
- **単発スクリプトの `django.test.Client`**: `Client(SERVER_NAME='localhost')` を指定する（既定の `testserver` は `ALLOWED_HOSTS` に無く 400 になる）。検証用に作ったデータは必ず後始末する。
- **テンプレート検索順**: `INSTALLED_APPS` の `myapp` は `django.contrib.admin` より前に置いたまま動かさない（`registration/` テンプレートの優先順位が壊れる）。

## 文言・命名

- **ユーザーの目に触れる文言はすべて日本語**で書く: ドメイン例外のメッセージ、`verbose_name`・`help_text`、画面・テンプレートの文言、フォームのエラーメッセージ。
- docstring・コード内コメントも日本語で書く。
- コードの識別子（クラス名・関数名・変数名・URL 名）は英語。

## コミット

- **機能ごとにコミットする。** 複数の機能や無関係な修正を1つのコミットに混ぜない。逆に、1つの機能（実装＋テスト＋README更新）は1コミットにまとめる。
- コミットメッセージは既存の履歴にならい日本語で書く。
- ブランチは切らず **main に直接コミット**する（単独開発のため）。

## ドキュメント

- 機能を追加・変更したら README の該当箇所（画面構成・アーキテクチャの説明）を更新する。README が仕様の一次ドキュメント。
