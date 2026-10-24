# プロジェクト規則

野球リーグ管理の Django アプリ。セットアップと起動は [README.md](README.md)、画面構成・仕様・アーキテクチャは Wiki（原稿は [docs/wiki/](docs/wiki/)。入口は [Home](docs/wiki/Home.md)）を参照。

ここには**どのファイルを触るときでも効く規則**だけを書く。特定の場所でだけ効く規則は `.claude/rules/`（`paths` で
スコープ済み。該当ファイルを読んだときに読み込まれる）、手順とコマンドは `.claude/skills/` に置く。
**何を作るかを決める段階で必要な規則（依存の向き・出典の一元化・不変条件）はこのファイルに置く。**

## 実行環境

- **すべてのコマンドは Docker コンテナ経由**で実行する（`docker compose exec web python manage.py ...`）。ホストに Python 環境は無い。
- リポジトリの実体は WSL2 内（Windows からは `U:\`）。実行中コンテナへの操作（`exec` / `logs` / `ps`）は Windows 側から直接叩ける。
  **コンテナを新規作成する操作（`docker compose up` / `run`）だけは WSL 側から実行する**（Windows 側から作るとバインドマウントが
  実体と切り離され、書き込みが実リポジトリに届かない）。手順は `docker-ops` スキル。
- テスト（フル）は `docker compose exec web python manage.py test myapp.tests.domain myapp.tests.integration --parallel 4` と
  `docker compose exec web python manage.py test myapp.tests.e2e` の2回（WSL からは `make test`）。domain 層のみなら
  `docker compose exec -e DJANGO_SETTINGS_MODULE= web python -m unittest discover -s myapp/tests/domain -t .`（DB 不要・最速）。
  **手元では並列数を `auto` にしない**（手元のコンテナは他のエージェントと共有で、20本にしたら Docker のエンジンが応答しなくなった。#184）。
  CI は専用のランナーなので `auto` でよい。どれを流すかの判断は `run-tests` スキル。
- `.env` の値に `$` を含めない（Docker Compose が変数展開して壊す。エラーにならず気づけない）。
- `requirements.txt`・`requirements-dev.txt` は**間接依存まで `==` でバージョン固定**する。変更したら `docker compose build --no-cache` でイメージを作り直す。

## アーキテクチャ規則（DDD）

依存の向きは常に内側（domain）へ: `presentation → application → domain ← infrastructure`

- **`myapp/domain/` は Django を一切 import しない。** domain 層のテストは Django 設定なしで通ること。
- application は domain のインターフェース（`domain/repositories.py`）越しに永続化を使う。`infrastructure/orm_models.py` を直接 import しない。
- presentation（views）は application 経由で操作する。ORM モデルやリポジトリ実装を直接触らない。
- **更新と参照を分ける**: 更新はリポジトリ経由で集約単位（`Team` / `Game`）に読み書きする。一覧表示などの参照は `infrastructure/queries.py` から直接 DTO を作る（集約を組み立てない）。参照クエリのインターフェースは `application/queries.py`（戻り値が DTO のため domain には置けない）。
- **ワークスペースどうし・実データとペナントの世界を混ぜない**: リポジトリと参照クエリは範囲 `WorldScope(workspace_id, world_id)` を
  必須の引数で受け取り（既定値なし）、実データはそのワークスペース、ペナントはその世界だけを読み書きする。
  世界の台帳（`WorldRepository`・`WorldSummaryQuery`）もワークスペースの id を必須で受け取る。「範囲が無ければ全部読む」形の API を作らない。
  **例外は管理画面専用の `admin_real_queryset` だけ**（全ワークスペースの実データ。`admin.py` 以外から使わないことを `test_wiring.py` が検査）。
  範囲を作るのは views の `_real_scope(request)`・`_world_scope(request, world_id)`、管理コマンドの `--workspace`・`--world`、テストの
  `real_scope()`・`world_scope()` だけ（Wiki「ペナントモードの仕組み」の「世界の分け方」・設計書 `docs/design/workspaces.md`）。
- **依存の組み立ては `presentation/views.py` の `build_*()` 関数だけ。** サービスを増やすときは組み立て口もここに足し、依存を全部渡す
  （`tests/integration/test_wiring.py` が検査する）。管理コマンドとテスト（`tests/helpers.py` 経由）もここから取る。呼ぶ側ごとに一部の依存だけを
  渡さない（渡し忘れが「開く画面によって落ちるサービス」になる。管理画面のテンプレートタグで実際に起きた）。
  **組み立て口はすべて範囲（`scope`）を必須で受け取る**（台帳だけを読む `build_pennant_world_view(workspace_id)` を除く）。
  ペナント専用・実データ専用の組み立て口に違う種類の範囲を渡すと `InvalidWorld`（どれがどれかは `test_wiring.py` の分類が出典）。
- **層をまたぐ受け渡しに素の `dict` を使わない。** application が presentation に返す形は `application/dto.py` の dataclass にする（文字列キーの dict は綴りを間違えても静的検査が黙る）。DTO に集約を包ませない。集約からの取り出しは application が担い、presentation は DTO を写すだけにする（前例: `application/game_edit.py` と `GameEditData`）。
- **`TeamApplicationService` に足す前に、対象ごとの別サービスに置けないか考える**（既に巨大で、分割は #219 で計画中）。分ける判断は選択肢としてユーザーに提示する。

## 同じ事実の出典を2つ作らない

- **チームの勝敗・選手の通算成績・順位はテーブルに保持しない。** すべて `Game` から集計する。集計結果をフィールドやキャッシュとして保存する変更は入れない。
- **年齢は保持しない。** 生年月日から算出する。
- 投球回の変換（`5.2` = 5回2/3 = 17アウト）は `InningsPitched` 値オブジェクトが唯一の出典。他の場所に再実装しない。率（打率・防御率など）は試合ごとの率を平均せず、合算した実数から計算し直す。
- 選択肢の一覧（球場の屋根種別など）はドメインの値オブジェクトが唯一の出典。画面やモデルに複製しない。
- **成績のカウント項目は値オブジェクト（`BattingLine` / `PitchingLine` / `FieldingLine`）のフィールドが出典。** 永続化（`infrastructure/repositories.py` の `_BATTING_FIELDS` など）とテーブルの列はそれに従い、`tests/integration/test_stat_fields.py` が突き合わせる。**項目を増やすときは値オブジェクト・永続化・参照クエリ（守備）・マイグレーションを同じコミットで直す**（ずれても例外にならず、その項目だけ保存されないという静かな不具合になる）。

### 例外: 1試合の明細は打席の導出値だが、保存もする

**試合の成績の出典は打席（`GamePlateAppearance`）。** `GameBattingLine` / `GamePitchingLine` / `GameFieldingLine` /
`GameInningScore` はそこから導ける値だが、**通算成績の集計のために保存もしている**（この規則の例外。もう1つは下の表彰）。
自責点と失点は走者ごとの逐次再生でしか出せず、SQL で集計できないため（経緯と実測は Wiki「試合がすべての出典」）。

同じ事実が2か所にあるので、**集約が照合する**:

- `domain/services/scoring.py` の `ensure_lines_match_plate_appearances()` が、保存しようとしている明細を打席から数え直した値と突き合わせる。
- `DjangoGameRepository.save()` が保存前に必ず通す。`bulk_create` で直接書くコード（`seed_virtual_games`）は素通りするので、自分で呼ぶ。
- 照合しないのは勝敗・セーブ・ホールド・先発登板だけ（打席からは決まらず、イニングスコアと継投から決まる別の関心事）。

**この例外を他の項目に広げない。** 「集計が遅いから保存する」を一般化すると、通算成績も順位も保存する形に戻ってしまう。

### 例外: ペナントの締めたシーズンのタイトル受賞者（表彰）は保存する

ペナントの世界でシーズンを締めるとき、その年のタイトル受賞者（部門の1位。同率は全員）を**表彰**（`PennantAward`。
`domain/pennant/awards.py`）として保存する（#248。2026-10-15 ユーザー決定）。「集計の結果」ではなく
**締めた時点で確定した記録**として扱う。戦力外の実績の加点がタイトルの数を使い、締めるたびに過去の全シーズンを
導き直すと 8リーグの10シーズン目で約24秒かかるため。

- **範囲は締めたシーズンのタイトル受賞者だけ。** 通算成績・順位・勝敗・タイトルの上位（画面の3人）は保存しない。実データのタイトルも保存しない（実データの試合は後から入力・修正される）
- **書くのはシーズンを締める処理だけ**で、受賞者はタイトルの画面と同じ集計（`application/pennant_awards.py` の `derive_awards`。`get_league_titles`）で導いてそのまま写す。保存した受賞者と後から導いた受賞者がずれないのは、**締めたシーズンの試合を書き換えない**から（`tests/integration/test_pennant_awards.py` が固定する）。これを次で守る:
  - 世界の試合を書くのは日を進める処理だけ。スコアブック・管理画面・明細の再構築（`rebuild_fielding_lines`）は実データの範囲に固定してある（`test_wiring.py`・`test_world_isolation.py`・`test_seed_commands_scope.py` が固定）
  - `DjangoGameRepository` の書き込み（`save` / `add_all` / `prepare_add_all`）は、世界の締めた年（`is_season_closed`）の試合を `AlreadyClosed` で拒む。締める前の年（シーズン終了後のポストシーズンなど）は書ける
  - 締めた年の試合を書き換える経路を足すなら、その年の表彰を消して同じ関数で導き直す
- **過去の表彰は受賞時の規定で確定し、書き換えない。** 部門・規定・記録の書式を後から変えても、締めた年の表彰は導き直さない（記録の表記も受賞時のまま）
- **同じ年を両方から読まない**: 締めた年は表彰を読み、終わったが締めていない年（`season.unclosed_completed_year`）だけを導く

**この例外は締めたシーズンのタイトル受賞者に限る。** 締めた年の順位・優勝・成績・タイトルの上位など、同じ条件（締めた後に変わらない・導くのが遅い）を満たすものも、ユーザーの決定なしに保存しない。

## 不変条件は集約が守る

背番号の一意性（期間が重なる同番号の禁止）、在籍期間の重複禁止などは `Team` 集約が自身で検査する。
ORM に直接 `bulk_create` 等で書き込むコード（データ投入コマンドなど）は、集約の検査を素通りするため自分で同じ検査を行うこと。

## テストと品質のゲート

- **テストは層ごとのディレクトリに置く**: 業務ルールは `tests/domain/`（DB 不要・Django 非依存）、画面の動作・リポジトリの往復・フォーム検証は `tests/integration/`、実ブラウザでの確認だけ `tests/e2e/`。
- **バグを修正したら、同じコミットに再発防止テストを添える**（前例: `tests/integration/test_templates.py`）。
- コミット前に `ruff check .`・`ruff format --check .`・`mypy .` を通す（コンテナ内。WSL からは `make lint`）。ルールの設定は `pyproject.toml` が唯一の出典。**`# noqa` で黙らせる前に指摘のとおり直す**（それでも黙らせるなら理由をコメントに残す）。
- **CI（`.github/workflows/ci.yml`）が PR と main・`epic/` への push で lint・型検査・React のビルド・フルテストを流す。** 直しては確かめる繰り返しは手元で行い、
  push の前に1回だけ手元でフルテストを流す。マージは CI の `test` が通ってから（自動マージは CI が通るまで待つ）。マージの後は main・epic への push で走った CI を確かめる
  （並行した PR は単独で通っても組み合わせで壊れる。#32 と #37）。落ちていたら次の作業より先に直す。
  差分がテストに影響しないパスだけなら、手元のフルテストを省く（CI も `test` をスキップする）。どのパスが該当するかは `ci.yml` の
  `changes` ジョブの `UNAFFECTED_BY_TESTS` が唯一の出典。手順は `open-pr` スキル。

## UI・設計方針

- **両立しない操作を同じ画面に並べない。** 片方を無効化して案内文で繕う前に、片方を消せないか考える。消す判断は選択肢としてユーザーに提示する。
- 書き込みの導線（登録フォーム・編集ボタン）は、ログインしていない人には表示しない。押しても弾かれるだけの導線を見せない。
- 読み書きが同じ URL に同居する画面は、GET は誰でも通し POST だけログインを求める（画面ごと `login_required` にしない）。
- 並べ替えのキーと既定の向きはドメイン層が持つ。不正なソートキーはエラーにせず既定の並びに落とす。

## 既知の罠（踏み直さない）

- **性能**: SQLite の多段 `prefetch_related` は関連1000件超で落ちるか遅くなる。遅さは SQL ではなく Python 側（ORM のモデル生成と値オブジェクトの組み立て）に出る。データ投入後と遅いと言われたときは `manage.py measure_pages` で実測する（詳細は `.claude/rules/performance.md`）。
- **単発スクリプトの `django.test.Client`**: `Client(SERVER_NAME='localhost')` を指定する（既定の `testserver` は `ALLOWED_HOSTS` に無く 400 になる）。検証用に作ったデータは必ず後始末する。
- **JSON API の部分更新**: キーの欠落を「空リスト」と同じ扱いにすると既存データが全消去される。行は位置ではなく識別子で組み立てる。
- **`Team.add_player` などの年の既定は現実の今日**: `add_player(from_year=)`・`promote_player(year=)`・`retire_player(year=)`・`appoint_captain(year=)`・`remove_captain(year=)` は年を省くと `date.today().year` が入る。ペナントの世界の年で書くときは必ず年を渡す。

## 文言・命名

- **ユーザーの目に触れる文言はすべて日本語**で書く: ドメイン例外のメッセージ、`verbose_name`・`help_text`、画面・テンプレートの文言、フォームのエラーメッセージ。
- docstring・コード内コメントも日本語で書く。
- コードの識別子（クラス名・関数名・変数名・URL 名）は英語。

## ブランチとコミット

### 手順の重さは変更の大きさで変える

| 変更 | Issue | セルフレビュー |
| --- | --- | --- |
| 1行で説明しきれる変更（誤字・文言・依存の更新） | 省く | 1周 |
| ドキュメント・規則・設定だけ（`docs/`・`.claude/`・CLAUDE.md・CI 以外の設定） | 省いてよい | 1周 |
| 上記以外（コードの変更） | 立てる | 3周 |

ブランチ・PR・CI の確認は、どの変更でも省かない。Issue を省いた PR は本文に「なぜ」を書く。

### Issue と PR

- **実装に着手する前に、実装計画を GitHub Issue に書く**（`issue-plan` スキル）。会話の中にしか無い計画は、後から「何をやると決めて着手したか」を追えない。
  - 計画にユーザーへの選択肢が含まれるときは、Issue の URL を提示したところで止め、回答を待ってから着手する。
  - 実装中に計画が変わったら Issue 本文を最新に直し、変えた理由をコメントに残す。
- **PR は必ず Issue とリンクさせる。** ブランチは `gh issue develop` で Issue に紐づけて作り、PR を作ったらリンクしたかを確かめる（しなくてもエラーにならない）。
- **Issue の状態は GitHub Projects（[Baseball League Manager](https://github.com/users/sumika157/projects/2)）で追う。** 作業中とレビュー待ちは自動では動かないので手で動かす
  （手順は `issue-plan`）。**Project は状態だけを持つ。** 何を・なぜ・どの順では `docs/ROADMAP.md` と Issue に書く。
- **タスクごとにブランチを切る。main に直接コミットしない。** 命名は `feature/` `fix/` `refactor/` `docs/` ＋ 英語の kebab-case（例: `feature/player-nationality`）。
- **機能ごとにコミットする。** 複数の機能や無関係な修正を1つのコミットに混ぜない。1つの機能（実装＋テスト＋ドキュメント更新）は1コミットにまとめる。
- コミットメッセージ・PR のタイトルと本文は日本語で書く。
- 完了したらセルフレビュー（下記）を済ませて push し、PR を作り、**`gh pr merge --auto --squash` で自動マージを予約する**（手順は `open-pr` スキル）。
  CI が通ればマージされる。落ちたら直して push し直す。マージされたら main の CI を確かめ、worktree を片付け、URL と結果を報告する（#244）。
- **次の PR は自動マージを予約せず、URL を提示してユーザーのマージを待つ**: `infra/`（本番の構成）に触れる / epic の統合ブランチを main に入れる /
  データの全再生成や既存データを書き換えるマイグレーションを含む。画面を変える PR は、一般公開までは自動でよい（公開したらこの一覧に足す）。
  PR 本文にプレビューの URL を書き、マージ後でもユーザーが触って確かめられるようにする。CLAUDE.md を変える PR は自動でよいが、報告で規則の変更点を1行ずつ示す。
- **マージの判断を `gh pr merge`（`--auto` なし）で代わりに下さない。** 例外の PR をこちらでマージしない。
- **`gh pr create` の前に `gh pr list --head <ブランチ> --state all` を見る。** ユーザーが先に PR を作っていることがあり、
  2本目を作るとスカッシュマージで main に空コミットが残る（#1 と #2。エラーにならない）。
- **PR を出した後にブランチへ追記する前に、PR がまだ OPEN かを確かめる。** マージ済みのブランチへの push は main に届かず、エラーにもならない（#83）。
- **ブランチを切る前に `git log --oneline origin/main..main` を見る。** ローカル main が先行していたらユーザーに push を促す。
- `gh` は **Windows 側だけ**にある（`sumika157` で認証済み。WSL には無い）。PATH に無ければ
  `C:\Users\sumik\AppData\Local\Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\bin\gh.exe` を直接叩く。

### PR を出す前のセルフレビュー

push の前に、PR に載る差分の全体（`git diff origin/<base>...HEAD`）を**3つの観点すべて**で読み、見つけたものを直す。これを1周として、上の表の周数を回す。
次の周は直した後の差分を、また3観点すべてで読み直す（観点を周ごとに1つずつ割り振らない）。

- **正しさ**: 頼まれたことを過不足なく満たしているか。バグ・境界値・通っていない経路（未ログイン・空データ・JSON のキー欠落など）。
- **規則**: このファイルと、触ったファイルに効く `.claude/rules/`。domain・集約・リポジトリ・クエリに触れたら `ddd-boundary-reviewer` エージェントを通す。
- **読み手**: PR 本文・コミットメッセージ・README と Wiki が変更と合っているか。差分が Issue の計画と合っているか。再発防止テストの有無、デバッグ出力・一時ファイル・無関係な変更の混入、コミットの粒度。

- 直しは修正コミットを積まず、`git commit --amend` で機能のコミットに含める（push 前なので書き換えてよい）。
- **終わる条件は「重要な指摘が残っていない」こと。** 重要＝バグ・データ消失・このファイルの規則違反・頼まれたことの未達。
  最後の周で重要な指摘を直したときだけ、直した箇所をもう1周確かめる。
- PR 本文に「セルフレビュー」節を設け、周ごとに直したこと（無ければ「指摘なし」）と、直さずに残した軽微な指摘を書く。

### 並行作業は2本まで、分けるときは worktree

- **同時に動かす作業（セッション・バックグラウンドのエージェント）は2本までを目安にする。** 手元のコンテナは共有で、
  増やすほど負荷・テスト DB の衝突・コンテナの再起動待ちが増え、全体では遅くなる。並行中はコンテナを再起動しない。
- 次のいずれかに当てはまるなら worktree で作業する: 並行作業中 / `git worktree list` に他の worktree がある / 未コミットの変更が自分のタスクと無関係。
  `.git/index` は単一ファイルなので、同じ作業ツリーで別セッションが `git add` すると、相手の変更が自分のコミットで確定する（実際に起きた）。
- worktree の作り方・中での検査とテスト（**`-w` で worktree を指さないと main のコードを検査する**）・罠・片付けは `worktree` スキル。

### 段階単体では main に入れられない機能は `epic/` に積む

**段階単体では動かない**（永続化だけ・画面だけ）/ **データの全再生成を伴う** / 3コミット以上に分かれる見込み、のいずれかなら、
統合ブランチ `epic/<機能>` を1本立てて、その上にタスクブランチを乗せ、**main への PR は統合ブランチの1本だけ**にする。
当てはまらない単発の修正・小さい機能は従来どおり `feature/xxx` 1本で main へ PR する。

- epic は親 Issue ＋ 段階ごとの sub-issue で追う（前例: #26 と #50〜#54）。タスクの PR は base を統合ブランチにする。
- 統合ブランチは段階の区切りごとに main を取り込む（`git merge origin/main`）。
- 設計は `docs/design/<機能>.md` に置き、段階ごとに実測値と決定を追記する。統合ブランチを main にマージするときに Wiki へ吸収して削除する。
- ブランチ名の制約・Issue の紐づけ方・sub-issue の閉じ方は `issue-plan` スキルの「epic の運用」。

## ドキュメント

- **仕様の一次ドキュメントは Wiki。原稿はリポジトリの `docs/wiki/` が唯一の出典**で、main に入ると
  `.github/workflows/wiki-sync.yml` が GitHub Wiki へ丸ごと反映する。Wiki の画面では編集しない（次の反映で上書きされる）。
- 機能を追加・変更したら `docs/wiki/` の該当ページを**同じコミットで**更新する。README はセットアップと起動の手順だけを持ち、仕様は書かない。
- ページ間のリンクは `[表示名](ページ名)`（拡張子なし）、リポジトリ内のファイルへのリンクは
  `https://github.com/sumika157/baseball-league-manager/blob/main/...` の絶対 URL で書く（Wiki では相対パスがリポジトリを指さない）。
- ページを増やしたら `Home.md` と `_Sidebar.md` の目次にも足す。
