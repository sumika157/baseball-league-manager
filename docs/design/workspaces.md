# 設計: ワークスペース（マルチテナント化）と一般公開

> 状態: 設計確定（コードは未着手）。親 Issue [#74](https://github.com/sumika157/baseball-league-manager/issues/74)、子 Issue は #75（この設計書）・#76〜#82（段階 W0〜W6。W1 は #77 の W1a と #172 の W1b に分ける。9.）。
> 2026-10-07 に `epic/pennant-mode` の最新（P6 と #95〜#104 まで入っている）と main の現状に照合して直した（3.1）。ペナント epic の残りは #167 と main 入りだけ。
> 着手はペナントモード（#26）を P6 まで終えて main に入れた後。W0（本番構成）だけは epic の外で先に main へ入れる。
> 段階ごとの実測値と決定はこのファイルの「14. 段階ごとの記録」に追記し、`epic/workspaces` を main に入れるときに Wiki へ吸収して削除する。

## 0. 名前

- コード上の名前は **`Workspace`**（集約・ORM・サービス）、画面の表示は **「ワークスペース」**。
- 「世界」はペナント（`PennantWorld` / `WorldScope`）がすでに使い、画面にも「世界バー」として出ている。
  そのためテナントの名前に「世界」は使わない。
- 両者の関係は入れ子にする: **ワークスペース ⊃ { 実データ（1つ）, ペナントの世界（0個以上） }**（3.1）。

## 1. なぜ

今のアプリにはデータの置き場所が1つしかない。新規登録した人は何も書けない（書けるのは管理ユーザーとチーム担当者だけ）。
書けるようにすれば、全員が同じリーグを書き換えることになる。このままでは一般公開しても、来た人が自分のチーム・選手・試合で遊べない。

そこで全データの上に「ワークスペース」という区切りを足し、持ち主がその中で全権を持つ形にして、公開できるようにする。

**一番重い作業は、区切りを足すことではない。** 今は管理画面でしかできない登録（リーグ・チーム・球場・選手のプロフィール）を、
サイトの画面として作ることが一番重い。一般の利用者に管理画面は渡せないので、これが無いと「登録して遊べる」にならない。

## 2. やること / やらないこと

### やること

| 項目 | 内容 |
| --- | --- |
| ワークスペース | 作成・名前の変更・公開/非公開の切り替え・削除。1人が持てる数は **3** まで |
| 範囲で絞る | すべてのリポジトリと参照クエリが、範囲を必須の引数で受け取る（ペナントの `WorldScope` を一般化する）。世界の台帳（`DjangoWorldRepository`・`DjangoWorldSummaryQuery`）もワークスペースで絞る。例外は管理画面だけ（3.1） |
| URL | `/w/<key>/...` の形でワークスペースを URL に入れる（セッションで切り替える方式は採らない。6.） |
| 権限 | 持ち主＝全権。編集者＝全体を編集できる。チーム担当＝担当チームの範囲だけ編集できる（今の `Team.managers`）。公開なら未ログインでも閲覧できる。非公開なら部外者には 404。**サイト側の `is_staff` の特権は廃止する** |
| 招待 | 持ち主が招待リンク（トークン）を発行し、自分で相手に渡す（メールは送らない）。期限は **7日**。相手はログインして受諾する |
| サイトの登録画面 | リーグ（外国人枠と支配下選手の登録枠 `registered_player_limit` を含む）・チーム（所属リーグ・本拠地・表示順）・球場の作成・編集・削除（中が空のときだけ）。選手のプロフィール欄の編集 |
| 見本 | 作成時に小さい見本を生成する（既定はオン） |
| 既存データ | 今の全データを1つのワークスペースへ移し、運営者が持ち主の公開ワークスペースにする。トップから見本として案内する |
| 公開の土台（W0） | Lightsail ＋ docker compose（Cloudflare Tunnel ＋ gunicorn）、SQLite の WAL ＋ IMMEDIATE、バックアップ（ROADMAP フェーズ5 の残り） |

### やらないこと

| 項目 | 理由 |
| --- | --- |
| 古い URL（`/team/<id>/` など）からのリダイレクト | まだ公開していないので、外から張られたリンクが無い |
| 公開ワークスペースの一覧（ディレクトリ） | 荒らしや不適切な名前を全員の目に触れさせる経路になり、通報や非表示の運用が要る。公開ワークスペースは URL を知っている人だけが開ける |
| 持ち主の譲渡・アカウントの削除画面 | 要望が出てから。それまでは運営者が管理画面で対応する |
| 過去の在籍・主将の在任歴をサイトで編集 | 現在の在籍の追加・退団・移籍と主将の指名は、サイトにすでにある。過去の経歴を組み立て直す画面は量が大きい |
| 試合数の上限・ボット対策 | 後で足す。差し込み口だけ作っておく（3.5） |
| ほかのワークスペースのリーグを元にしたペナントの世界 | ワークスペースをまたいでデータを写すことになり、範囲の保証が崩れる |
| メール（招待・パスワード再設定の送信） | 当面は送らない |
| PostgreSQL への移行・スキーマ分割 | 決定済み（当面は SQLite、区切りは同じ DB の中で行う） |

## 3. ドメインへの影響

### 3.1 ペナントの「世界」との関係

#### 統合ブランチ `epic/pennant-mode` で確かめた事実（2026-10-07 時点。epic は 65a4715、main 取り込み中の worktree も参照。着手時に #167 と main 入りの変更を確かめ直す）

| 項目 | 現状 | 根拠 |
| --- | --- | --- |
| 世界の出典 | `League.world`（FK。null なら実データ） | `orm_models.py` の `League.world` |
| `League` の一意制約 | `world IS NULL` のときの `name`（`unique_real_league_name`）と `(world, name)`（`unique_world_league_name`）。作るのは `0042_pennant_world_and_league_world.py`（main 取り込み後の番号） | `orm_models.py:118` 付近 |
| `WorldScope` の形 | `WorldScope(world_id: int \| None)`、`real()`・`pennant(world_id)`、既定値なし。frozen dataclass で `is_real`・`is_pennant`・`__str__` を持つ。`World.scope` が `pennant(id)` を返す | `domain/pennant/world.py:47-77`、`:112-116` |
| `WorldScope` を作る箇所 | 本番コード25か所（views 16・`admin.py:98` の `_REAL`・`DjangoWorldRepository.delete`・`World.scope`・管理コマンド6）。テスト約130 | `git grep` |
| `scoping` の関数 | `leagues_in`・`teams_in`・`stints_in`・`games_in`・`fixtures_in`・`ratings_in`・`players_in`・`club_plans_in`（P5a）。台帳用に `world_condition(path, scope)`・`world_condition_in(path, world_ids)`・`leagues_in_worlds`・`games_in_worlds`・`fixtures_in_worlds`。範囲ではないが同じファイルに `period_covering(year)`（#95） | `scoping.py:22,34,43,47,51,97,102` |
| 選手は世界の列を持たない | 在籍をたどって決まる。ただし実データ側は**否定形**（ペナントの球団に在籍したことが無い選手）で、在籍の無い選手も実データに含まれる（管理画面から隠さないため）。開発 DB の在籍の無い選手は0人 | `scoping.py:82-94` |
| 球場 | 世界をまたいで共有し、ペナントは読むだけ（分類 `SHARED`）。ただし `DjangoTeamRepository.save` は `home_stadium_id` を範囲で検査せずに書く | `repositories.py:233-251` |
| 書き込みの判定 | `PennantWorld.owner`（`_is_owner`・`_requires_world_owner`）。owner は `SET_NULL`。世界の上限は1人5つ（`MAX_WORLDS_PER_OWNER=5`、`count_by_owner`）、シーズンは `MAX_SEASONS_PER_WORLD=10` | `views.py:401,406`、`world.py:28-30` |
| 世界の一覧 | 誰でも見られる。台帳は範囲を持たない設計で、`test_wiring.py:94` の `UNSCOPED = {DjangoWorldRepository, DjangoWorldSummaryQuery}` で検査から外してある。`find_all`・`list_all` は全世界を読む | `repositories.py:1359-`、`queries.py:758-` |
| 組み立て口 | 16個（下表）。`build_world_view_service(world_id)` は P4a で入った | `views.py:170-709` |
| `scoped_url` | 入っている。context の `world` が無ければ `reverse(name)`、あれば `reverse("pennant_" + name, (world_id, *args))`。テンプレートで `{% scoped_url %}` 54か所、素の `{% url %}` 97か所 | `templatetags/scoped.py:17-22`、`urls.py` の `_pennant()` |
| マイグレーション | main は `0041_jersey_number_as_text` まで。epic は main 取り込みで `0042〜0045` に振り直し中。**ワークスペースは 0046 から始まる見込み**（着手時に確かめ直す） | `myapp/migrations/` |

組み立て口（16個）の範囲の受け方:

| 範囲の受け方 | 組み立て口 |
| --- | --- |
| 実データに固定 | `build_service`・`build_recording_service`・`build_team_analysis_service`・`build_roster_service`・`build_permission_query`・`build_player_search_query`・`build_pennant_world_service`（分岐元が実データ） |
| `world_id` を受け取る | `build_world_view_service`・`build_pennant_ratings_service`・`build_pennant_season_service`・`build_pennant_offseason_service`・`build_offseason_view_service`・`build_pennant_home_service`・`build_club_service`・`build_club_details_service` |
| 範囲を持たない | `build_pennant_world_view` |

補助は `_repositories_for(scope)` と `_world_clock(world_id, ...)`。`_scope(world_id)`（`views.py:419`）が共有ビューの範囲を決める唯一の分岐点で、`test_wiring.py` は全組み立て口を検査している。

#### 比べた案

| 案 | 内容 | 判定 |
| --- | --- | --- |
| **A. 入れ子にし、リーグの親は1つだけ（採用）** | 実データのリーグは `League.workspace`、ペナントのリーグは `League.world` を持つ。どちらか片方だけを持つことを CheckConstraint で強制する。世界は `PennantWorld.workspace` でワークスペースに属する | 同じ事実の出典が重ならない。ペナントのリーグのワークスペースは、世界からたどれる |
| B. 全リーグに `workspace` を持たせ、ペナントのリーグは `world` も持つ | 実装は単純 | ペナントのリーグでは、ワークスペースが2か所（`League.workspace` と `League.world.workspace`）になる |
| C. 実データも世界の1行にする（`League.world` を null 不可にし、世界に種別を持たせる） | 最終形は一番きれい | 実データの行では `PennantWorld` の列（シード・開幕年・受け持つ球団）が意味を持たない。ペナントの模型と制約を作り直すことになる |
| D. 世界とテナントを別々の仕組みで二重に絞る | — | 絞り込みの層が2つになり、片方の絞り忘れがそのまま漏れになる |

**A なら一意制約はむしろ単純になる。**

- 実データの `League.name` は `UniqueConstraint(["workspace", "name"])` にする。ペナントのリーグは workspace が NULL なので、この制約とは衝突しない。
- ペナントのリーグは今の `(world, name)` のまま。
- epic の条件付き制約（`name WHERE world IS NULL`）は、この制約で置き換える。

#### `WorldScope` の一般化

- `WorldScope(workspace_id: int, world_id: int | None)` にする。作り方は `real(workspace_id)` と `pennant(workspace_id, world_id)` の2つで、どちらも引数は必須。
- 引数が増えるので、今ある呼び出しは mypy ですべて落ちる。**直し漏れが残らない**（epic が P2 で使ったのと同じ手）。
- `scoping` の条件は次のとおり。
  - 実データ: `league.workspace_id = X`
  - ペナント: `league.world_id = W`
  - ペナントでは、組み立てるときに `PennantWorld.workspace == X` を照合する。URL を書き換えて他人の世界を開けないようにするため。
  - `club_plans_in`（P5a）も同じ条件の書き換えの対象。
- `stadiums_in(scope)` を足す。実データでもペナントでも、そのワークスペースの球場を返す。**ペナントの範囲でも `scope.workspace_id` で引く**（ペナントの範囲にもワークスペースの id が要る理由）。
- `players_in(scope)` は、実データでは「そのワークスペースのリーグの球団に在籍がある選手」。
  - 今は否定形（ペナントの球団に在籍したことが無い選手）なので、これを肯定形（`Exists(在籍 → チーム → League.workspace_id = X)`）に書き換える。性能は `measure_pages` の選手検索で確かめる。
  - **選手に workspace の列は足さない。** 在籍から導けるので、持つと出典が2つになる。
  - その代わり「選手は少なくとも1つ在籍を持つ」を前提にする。サイトでの選手登録は必ずチームの下で行い、ペナントの分岐とドラフトも在籍を作る。
  - 在籍の無い選手を作れるのは管理画面（運営者）だけ。肯定形にするとこの選手はどの範囲からも消えるので、管理画面は下の専用関数で拾う。
- **世界の台帳もワークスペースで絞る。** `DjangoWorldRepository(workspace_id)`・`DjangoWorldSummaryQuery(workspace_id)` にし、`test_wiring.py` の `UNSCOPED` を空にする（決定 2026-10-07 Q3）。画面（`/pennant/`）は W6 まで変えない。
  - `world_condition_in` と `*_in_worlds` は、ワークスペースで絞った台帳が返す世界の id だけを受け取る。
- **管理画面だけは全ワークスペースの実データを読んでよい例外**（決定 2026-10-07 Q4）。`scoping` に管理画面専用の関数を1つ置き（全ワークスペースの実データ＋在籍の無い選手）、`admin.py` 以外が import していないことをテストで縛る。「全範囲を読む API を作らない」規則の唯一の例外にする。
- 名前は `WorldScope` のまま残し、docstring に「実データ＝そのワークスペースの実データの世界」と書く。

#### ペナント側で変わること（W6）

| 項目 | 変更 |
| --- | --- |
| 分岐元 | 「実データのリーグ」→「そのワークスペースの実データのリーグ」 |
| 世界の一覧 | `/pennant/` → `/w/<key>/pennant/`。「ほかの人の世界」に出るのは、同じワークスペースの中の世界だけ |
| 世界の公開範囲 | ワークスペースの公開・非公開に従う（世界ごとに公開の列は持たない） |
| 書き込みの判定 | 世界を作れるのは、ワークスペースの全体編集権を持つ人（持ち主・編集者）。進める・編成するのは世界の作成者（`PennantWorld.owner`）。ワークスペースの持ち主は削除もできる |
| 上限 | ペナントの上限（世界の数・シーズン数）は「ワークスペースごと」に読み替える。今は「1人5世界」（`MAX_WORLDS_PER_OWNER=5`、owner で数える）。値（5のままか）と数え方（owner ではなく workspace）は W6 で決める |
| `scoped_url`（P4a） | `workspace_key` を必ず運ぶように一般化する（6.） |
| 戦力分析のペナント版（#130、ロードマップ フェーズ4.6 の G） | W6 の前後で URL の作り方が変わる。9. の並び順の注記 |

### 3.2 新しいドメインの型（`myapp/domain/workspace.py`。Django は import しない）

| 型 | 種類 | 内容 |
| --- | --- | --- |
| `Visibility` | Enum（値オブジェクト） | `PUBLIC` / `PRIVATE`。選択肢の唯一の出典。フォームと ORM の choices はここから写す |
| `Workspace` | 集約ルート | `id`・`key`（URL 用）・`name`・`owner_id`・`visibility`・`editor_ids: frozenset[int]`・`team_managers: dict[team_id, frozenset[user_id]]` |
| `Invitation` | エンティティ | `token_digest`・`role`・`team_ids`・`expires_at`・`created_by`。受諾したら消す（残すと、メンバーである事実が2か所になる） |
| `InvitedRole` | Enum | `EDIT_ALL`（全体を編集）/ `EDIT_TEAMS`（特定のチームを編集） |
| `WorkspaceAccess` | 値オブジェクト（frozen） | 1回の要求で、その人がそのワークスペースで何をできるか。`role`（`OWNER` / `EDITOR` / `TEAM_MANAGER` / `VIEWER` / `NONE`）・`managed_team_ids`・`visibility` を持つ。メソッドは `can_view()`・`can_edit_all()`・`can_edit_teams(team_ids)`（どちらか1チームの担当なら可）・`can_manage_members()`・`can_delete()` |
| `WorkspaceQuota` | 値オブジェクト | `max_owned_workspaces=3`。後で足すための差し込み口として `max_games: int \| None`・`max_pennant_worlds: int \| None` も持つ。`ensure_can_create_workspace(owned_count)` で検査する |
| 例外 | `DomainError` の派生 | `WorkspaceNotFound`・`WorkspaceLimitReached`・`InvalidInvitation`（期限切れ・使用済み）・`InvalidWorkspace`。メッセージは日本語 |

### 3.3 不変条件と、それを守る場所

| 不変条件 | 置き場所 | 理由 |
| --- | --- | --- |
| 持ち主は編集者の集合に入らない。チーム担当のチームは、そのワークスペースのチームである | `Workspace` 集約（保存のとき、範囲のチーム id を渡して検査する） | メンバー全体を見ないと判定できない |
| 1人が持てる数の上限 | `WorkspaceQuota`（持ち主の所有数はアプリケーション層が数えて渡す） | 数えるのは参照、判定は純粋関数 |
| 招待は期限内に1回だけ使える。`EDIT_TEAMS` ならチームが1つ以上ある | `Invitation` | — |
| 実データのリーグ名・球場名は、ワークスペースの中で一意 | DB の UniqueConstraint。違反はリポジトリが日本語の例外に変える | 今は全体で一意なので、範囲を変えるだけ |
| チームの本拠地・所属リーグ・移籍先、試合の両チームは、同じワークスペースのもの | 範囲で絞ったリポジトリ（範囲外の id は NotFound）。球場は `stadiums_in(scope)` で引く。**本拠地は `DjangoTeamRepository.save` で検査する**（今は `home_stadium_id` を範囲で検査せずに書くので、W1b で足す。ペナントの球団は分岐元の `home_stadium_id` を指すので、不変条件としてもここで守る）。移籍先は Team 集約（範囲内のチームだけ）で守る | **漏れを止める本体**。フォームに隠しフィールドで id を載せている画面（`GameForm.home_team` など）があるため |
| 選手の在籍はすべて同じワークスペースのチームだけ | Team 集約（移籍先は範囲内）と管理画面の外部キーの選択肢（`PlayerStintAdmin`）。integration テストで検査する | 在籍経由で範囲が決まる3つのモデルの前提で、DB では強制できない。ペナントは分岐で選手を複製するので成り立つが、実データでは管理画面で他所のチームを選べると破れる |
| 背番号・在籍・主将・外国人枠 | 変更なし（`Team` / `Game`） | 範囲の外の行は集約に入ってこない |

**`Team.managers` の扱い:** 「チーム担当」というメンバー種別の行は作らない。

- ワークスペースのメンバーは、持ち主・`WorkspaceEditor` の行・そのワークスペースのチームの `managers` を合わせたものになる。
- 役割をフィールドで持つと、`Team.managers` と食い違いうる。
- 書き込みは `Workspace` 集約に1本化する。保存先は既存の M2M テーブルのまま。

### 3.4 期間を持つ概念か

持たない。メンバーと担当は「現在の状態」だけを持つ（ペナントの1軍登録と同じ判断）。
招待の期限は「いつまで有効か」の1点だけなので、在籍（`Stint`）のような重なりの検査は要らない。

### 3.5 上限・ボット対策の差し込み口

- 作成系のユースケース（ワークスペースの作成・試合の作成・世界の作成・進める）は、入口で `WorkspaceQuota` の検査を呼ぶ形にしておく。
- 上限の値は `build_*` が settings（環境変数）から読んで渡す。
- ボット対策は、後で新規登録のビュー（`SignUpView`）に差し込む（おとりの入力欄や、IP ごとの回数制限）。

### 3.6 見本データ

| 案 | 内容 | 判定 |
| --- | --- | --- |
| **生成（採用）** | 固定シードにワークスペースごとのずらしを加えて、選手と能力を引く。シミュレーションのエンジンで試合を回し、`assemble_game` と `GameRepository.add_all` で保存する | スキーマや成績の項目が増えても自動で追従する。本番に numpy は要らない |
| テンプレートの複製 | 見本用のワークスペースを1つ持ち、id を振り直しながら約10の表を写す | 項目を増やすたびに複製のコードも直す必要があり、直し漏れはその項目が静かに欠ける形で出る。PROTECT の外部キーの順序も面倒 |
| 今の seed コマンドを流用 | — | numpy が開発用の依存にしか無い。3,480試合を作ると約70秒かかる |

**規模:** 1リーグ6チーム・各25人（150人）・球場6、開幕から約2週間ぶん（**約36試合、1チーム12試合前後**）。

- 見積り: 1試合約27ms（ペナント P3b の実測）なので、生成と保存で約1〜1.5秒。行数は1試合約245行として約9千行（約0.7MB）。
- 順位表・ランキング・ボックススコアが空にならない、最小限の量にした。
- 試合は「実データ」の試合として入るので、スコアブックで編集できる。
- 名前とプロフィールの生成は、ペナント P6 が新人の生成のために切り出すものを使う。

## 4. 層ごとの変更

### domain

- `domain/workspace.py`（新規。3.2）
- `domain/pennant/world.py`: `WorldScope(workspace_id, world_id)`、`World.workspace_id`
- `domain/repositories.py`: `WorkspaceRepository`（`find_by_key` / `find_by_id` / `save` / `delete`）・`InvitationRepository`・範囲つきの `StadiumRepository` を Protocol で足す
- `domain/sample/`（新規）: 見本の組み立て。能力は `domain/simulation/samples.py`、試合は `domain/simulation/engine.py` と `assemble_game` を使う
- 既存の `Team` / `Game` / 値オブジェクト / 集計は変更しない

### application

| サービス | 責務 | 依存 |
| --- | --- | --- |
| `WorkspaceService`（新規、`application/workspaces.py`） | 作成（見本の有無を選べる）・一覧（自分が持つもの、メンバーとして入っているもの）・名前と公開の変更・削除・招待の発行と取消・受諾・メンバーの一覧と役割の変更と除名 | `WorkspaceRepository`・`InvitationRepository`・`WorkspaceQuota`・`SampleBuilder`・範囲つきリポジトリの生成器 |
| `LeagueSetupService`（新規、`application/league_setup.py`） | リーグ・チーム・球場の作成・編集・削除（中が空のときだけ）。選手のプロフィールの更新 | 範囲つきの `LeagueRepository`・`TeamRepository`・`StadiumRepository` |
| `TeamApplicationService` | **メソッドは足さない。** 範囲つきで組み立てられるだけ | — |
| `GameRecordingService` | 変更なし（範囲つきで組み立てる） | — |
| `TeamAnalysisService`・`RosterService` | 変更なし。範囲つきで組み立てるだけ（戦力分析は `DjangoTeamAnalysisQuery(scope)`、契約区分・昇格・FA 宣言は `DjangoTeamRepository(scope)` で、すでに範囲で絞れている） | — |
| ペナントの9サービス（`PennantWorldService`・`PennantSeasonService`・`PennantOffseasonService`・`OffseasonViewService`・`PennantHomeService`・`PennantRatingsViewService`・`PennantWorldViewService`・`ClubManagementService`・`ClubDetailsService`） | 変更なし。範囲（またはワークスペースの id）つきで組み立てるだけ | — |

- DTO（`application/dto.py`）: `WorkspaceContext`（key・名前・公開かどうか・自分の権限・世界 id）・`WorkspaceRow`・`MemberRow`・`InvitationView` など。素の dict は使わない。
- 参照の Protocol（`application/queries.py`）: `WorkspaceAccessQuery.access_for(user_id | None, key) -> WorkspaceAccess`、`WorkspaceListQuery`。

### infrastructure

- `orm_models.py`: 5. のとおり。
- `scoping.py`: 3.1 のとおり（`stadiums_in` を足し、条件にワークスペースを加える）。
- `repositories.py` / `queries.py`: epic で `WorldScope` を通した箇所はそのまま使う。追加は球場とメンバーの経路だけ。
- `DjangoTeamPermissionQuery` は `DjangoWorkspaceAccessQuery` に置き換える。1回の要求で読むのは、ワークスペースの行・編集者の行の有無・担当チームの id で、**多くて3クエリ**。判定は domain の `WorkspaceAccess` に任せる。
- `admin.py`: 管理画面は運営者専用にする。
  - 全モデルにワークスペースの列と絞り込みを足す。
  - 外部キーの選択肢は、編集中の行と同じワークスペースのものに限る。
  - 今ある `_REAL` 定数（`admin.py:98`）と `RealDataOnlyMixin`（`_REAL_QUERYSETS`）は、「全ワークスペースの実データを読む」前提に変わる。`DjangoTeamRepository(_REAL)`・`DjangoLeagueRepository(_REAL)` を直接作っている箇所（TeamAdmin・在籍の検査）は、行のワークスペースから範囲を作る。一覧は 3.1 の管理画面専用の関数で読む（Q4）。
  - ダッシュボードのテンプレートタグ `admin_overview` は、範囲を必須にすると呼べなくなる。そこで運営向けの概況（ワークスペース数・ユーザー数・行数の多いワークスペース）に置き換える。
- 管理コマンド: `seed_virtual_players` / `seed_virtual_games` / `measure_pages` / `rebuild_fielding_lines` / `pennant_create`（分岐元のリーグを選ぶ側）は `--workspace <key>` を必須にする。`pennant_advance` / `pennant_close_season` / `pennant_delete` は `--world` から世界のワークスペースを導く。`simulate_sample`・`backup_db` は対象外。

### presentation

- `views.py`:
  - 今ある16個の組み立て口（3.1 の表）は、すべて範囲（またはワークスペースの id）を必須の引数にする（既定値は無し）。ペナント用は「台帳で世界を引いてから `WorldScope.pennant(ws, w)` を作る」補助を1つ置く。
  - 範囲の分岐は `_scope(world_id)` の1か所のまま。W1 の間は一時関数（例 `_legacy_workspace_id()`。id が最小のワークスペース、無ければ 404）で今の URL を唯一のワークスペースに解決し、W2 で消す（決定 2026-10-07 Q2）。`templatetags/admin_overview.py` も W1 ではこの一時関数の範囲で呼ぶ。
  - P4a の `build_world_view_service(world_id)` は、W1b で `build_service(WorldScope.pennant(ws, w))` に吸収するか、引数を `scope` にそろえる。
  - 新しい組み立て口として `build_workspace_service()` / `build_league_setup_service(scope)` / `build_workspace_access_query()` を足す。`test_wiring.py` がすべてを検査する。
- ワークスペースを解決するデコレータ `@workspace_view(write=...)` を作る。
  1. URL の `key` から `WorkspaceAccess` を作る。
  2. 非公開のワークスペースに部外者が来たら 404 を返す（未ログインならログインへ誘導する）。
  3. `request.workspace`（`WorkspaceContext`）を載せる。
  4. POST では権限を検査する（ログインしていなければログインへ、権限が無ければ 403）。
  - 今の `_requires_login` / `_requires_team_permission` はこのデコレータの中へ移す。
- テンプレート: `can_edit*` は `WorkspaceAccess` から計算する。URL はすべて `scoped_url` で引く。

### CLAUDE.md の規則の変更（W1・W2 で、差分をユーザーに見せてから直す）

- 「依存の組み立ては `views.py` の `build_*` だけ」への直しは main で済んでいるので、ここでは扱わない。
- 「リポジトリと参照クエリは範囲（ワークスペースと世界）を必須で受け取る。全範囲を読む API を作らない」を足す。**管理画面専用の関数だけは例外**と書く（3.1、Q4）。
- 「サイト側の権限は `WorkspaceAccess` だけで判定する。`is_staff` は管理画面専用」を足す。

## 5. データとマイグレーション

ペナントの epic を main に入れた後で作る（epic にも複数のマイグレーションがあり、並行して作ると番号が衝突するため）。番号は仮に N とする。2026-10-07 時点では main が `0041` まで、epic が main 取り込みで `0042〜0045` に振り直し中なので、**N は 0046 の見込み**（着手時に確かめ直す）。

| ファイル | 種類 | 内容 |
| --- | --- | --- |
| `00N_workspace` | スキーマ | `Workspace`（`key` は一意。10文字のランダムな英小文字と数字）・`name`・`owner`（PROTECT）・`visibility`（choices は `Visibility` から）・`created_at`。`WorkspaceEditor`（workspace と user の組で一意）。`WorkspaceInvitation`（`token_digest` は一意、role、teams の M2M、`expires_at`、`created_by`）。`League.workspace`・`Stadium.workspace`・`PennantWorld.workspace` を **null 可**で足す |
| `00N+1_backfill_default_workspace` | データ移行 | リーグ・球場・世界の行が1つでもあれば、ワークスペースを1つ作る。公開にする。持ち主は id が最も小さい superuser（いなければ staff。どちらもいなければ「先に createsuperuser を」という日本語の例外で止める）。**ほかの staff は `WorkspaceEditor` にする**（今の全権を失わせないため）。`world IS NULL` のリーグ・全球場・全世界をこのワークスペースに付ける。逆方向の処理も書く |
| `00N+2_workspace_constraints` | スキーマ | `Stadium.workspace` と `PennantWorld.workspace` を null 不可にする。`Stadium.name` の `unique=True` を外し、`UniqueConstraint(workspace, name)` にする。League は epic の `name WHERE world IS NULL`（`unique_real_league_name`）を外して `UniqueConstraint(workspace, name)` にする（`(world, name)` は残す）。CheckConstraint で、`workspace` と `world` のちょうど片方だけを持つようにする |

- **既存データへの影響:** 更新するのはリーグ・球場・世界の数十行だけ。試合と明細（約87万行・64MB）には触らない（範囲はリーグからたどるため）。
- 開発 DB の件数（2026-10-07 時点。リーグ8・球場21・チーム48・選手1,619・在籍の無い選手0・`Team.managers` 0行・ユーザー3人のうち staff 2・superuser 2）は、#155（仮想データの作り直し）の後に変わるので、着手時に測り直す。
- 世界の owner のうち staff でない人は、backfill では編集者にならない（今の開発 DB に該当者はいない見込み）。本番に移すときに該当者がいれば、扱いを決める。
- League と Stadium の表は SQLite の制約変更で作り直しになるが、行数が少ないので問題ない。
- `Team.managers` の既存の行は、移したワークスペースのチーム担当としてそのまま残る。
- **prefetch の罠:** 新しい多段の prefetch は作らない。ワークスペースの削除は、ペナントの世界の削除（子の表から順に、範囲で絞った削除を発行する）と同じ手順にする。Django の CASCADE には任せない（数十万行を Python に集めることになるため）。

## 6. 画面と導線

### URL にワークスペースを入れる

セッションで切り替える方式にしない理由は3つ。

1. 公開したワークスペースを URL で共有する、という要件をセッションでは満たせない。
2. タブを2つ開いて別々のワークスペースを見ると、POST の宛先がずれて別のワークスペースに書き込む事故が起きる。
3. URL だけで権限が決まるので、テストで全 URL を機械的に回せる。

| URL | 画面 | 見る | 書く |
| --- | --- | --- | --- |
| `/` | ログイン時は「あなたのワークスペース」（持っているもの・メンバーとして入っているもの）と作成フォーム。未ログイン時は紹介と、見本のワークスペース（移したデータ）へのリンクと、新規登録 | 誰でも | 作成はログインが必要 |
| `/w/<key>/` 以下 | 今のすべての画面（ダッシュボード・チーム・選手・試合・順位表・リーグ・タイトル・成績・選手検索・スコアブックの API） | 公開なら誰でも、非公開ならメンバーだけ | 今の規則（担当チーム、または全体の編集権） |
| `/w/<key>/setup/leagues/`・`teams/`・`stadiums/` | リーグ・チーム・球場の一覧と作成・編集 | メンバー | 全体の編集権 |
| `/w/<key>/settings/` | 名前・公開/非公開・削除への導線 | 持ち主 | 持ち主 |
| `/w/<key>/members/` | メンバーの一覧と役割（全体 / 担当チーム）・除名・招待リンクの発行と取消 | 持ち主 | 持ち主 |
| `/w/<key>/delete/` | 削除の確認 | 持ち主 | 持ち主 |
| `/invitations/<token>/` | 招待の受諾（未ログインならログインか新規登録へ誘導し、戻ってくる） | トークンを持つ人 | ログインが必要 |
| `/w/<key>/analysis/`・`/w/<key>/team/<id>/analysis/` | 戦力分析（main #144。今の `/analysis/`・`/team/<id>/analysis/`） | 公開なら誰でも、非公開ならメンバーだけ | — |
| `/w/<key>/pennant/...` | ペナント（epic の画面を移す）。W6 で移すのは `advance/`・`season/close/`・`offseason/<year>/`・`club/`・`delete/` と参照画面14本（`urls.py` の `_pennant()`） | ワークスペースに従う | 3.1 |
| `/accounts/...`・`/admin/` | 変更なし（管理画面は運営者専用） | — | — |

- **リンクの引き方:** P4a の `scoped_url` を一般化する。context の `WorkspaceContext`（key と、あれば世界 id）を読んで、`/w/<key>/[pennant/<w>/]...` を引く。今のテンプレートは `{% scoped_url %}` が54か所、素の `{% url %}` が97か所あり、後者を `scoped_url` に直すのが W2 の作業量になる。
- **ヘッダー:** ログイン時はワークスペースの切り替え（ドロップダウン）を出す。ワークスペースの中では、パンくずの起点を「<ワークスペース名>」にし、公開・非公開と自分の権限をバッジで出す。
- **両立しない操作を並べない:** 公開と非公開は、設定画面の1つの選択欄で切り替える。招待を開いた人がすでにメンバーなら、「受諾」は出さずに「既にメンバーです」とだけ出す。削除は確認画面を挟む。
- **書き込みの導線は、権限の無い人に出さない**（今の規則を `WorkspaceAccess` から計算する）。
- **読み書きが同じ URL に同居する画面**: GET はワークスペースの閲覧権、POST は編集権で判定する。
- **並べ替え:** リーグとチームの表示順は、編集フォームの「表示順」の数値で決める（ドラッグは管理画面だけに残す）。

## 7. テスト計画

### domain（`tests/domain/`。DB は使わない）

| ファイル | 検査すること |
| --- | --- |
| `test_workspace_access.py` | 権限の真理値表: 役割（持ち主・編集者・担当・閲覧者・関係なし・未ログイン）× 公開/非公開 × 操作（閲覧・全体の編集・チームの編集・メンバー管理・削除）。担当は試合の2チームのどちらかなら可。`is_staff` で何も増えないこと |
| `test_workspace.py` | 名前の検査。持ち主は編集者に入らない。担当チームが範囲外なら拒否する。上限（3つ目まで作れ、4つ目で `WorkspaceLimitReached`） |
| `test_invitation.py` | 期限切れ。1回だけ使える。`EDIT_TEAMS` でチームが0件なら拒否する |
| `test_pennant_world.py`（epic の既存テストを拡張。`WorldScope` のテストはここにある） | `real(ws)` / `pennant(ws, w)` |
| `test_sample_builder.py` | 同じシードなら同じ見本になる。背番号が重複しない。外国人枠を守る。生成した試合が打席との照合を通る。試合数が目安どおり |

### integration（`tests/integration/`）

| ファイル | 検査すること |
| --- | --- |
| `test_workspace_isolation.py`（**中心**） | URL の解決器から `/w/<key>/` 以下の全パターンを**列挙**し、分類表に無い URL があれば落とす（画面を足したときの検査漏れを防ぐ）。目印の名前を付けたワークスペース A・B を用意して、次を確かめる。(1) A の key の下で B の id を開くと 404。(2) A の画面の HTML に B の目印が出ない。(3) 非公開の B は、未ログインならログインへ、部外者なら 404。(4) すべての POST を他人のワークスペースに送っても、DB の行数と内容が変わらない。(5) A の key の下で B の世界 id を開くと 404。(6) 選手検索に B の選手が出ない。(7) 中の画面のリンクがすべて `/w/<key>/` で始まる |
| `test_cross_workspace_ids.py` | フォームと JSON に他所の id を混ぜる経路（試合登録の `home_team`・スコアブック API の選手 id・チーム編集の球場・移籍先・担当の割り当て・契約区分の昇格と FA 宣言の POST・戦力分析の `?team=`）。すべて拒否され、何も保存されない |
| `test_workspace_permissions.py` | 役割ごとに、書き込みの導線が出る／出ない。POST が通る／403。未ログインならログインへ |
| `test_workspace_service.py` | 作成（見本あり・なし）、上限、削除で全行が消えて他のワークスペースの行は残ること、招待の受諾と除名（除名すると担当も外れる） |
| `test_league_setup_screens.py` | 作成・編集・空のときだけ削除できること。名前の一意がワークスペースの中だけで効くこと |
| `test_world_isolation.py` の `ModelClassificationTest`（epic の `CLASSIFICATION` を拡張。独立したファイルは作らない） | myapp の全モデルを、範囲のたどり方で分類する。分類に漏れがあれば落とす。分類表は下の表 |
| `test_workspace_migration.py` | データのある DB ではワークスペースが1つでき、全リーグ・球場・世界が付き、staff が編集者になる。空の DB では何もできない |
| `test_admin.py`（拡張） | 管理画面の外部キーの選択肢が、同じワークスペースのものに限られる。在籍の無い選手が管理画面の一覧に出る（Q4） |
| `test_seed_commands_scope.py`（拡張） | `--workspace` が必須で、ほかのワークスペースに触れない。`pennant_create --workspace` が他所のリーグを分岐元にできない |
| `test_wiring.py`（拡張） | 16個＋新しい `build_*` のすべてと、範囲が必須であること。`ScopeIsRequiredTest` を「`scope` か `workspace_id` が必須」に広げ、`UNSCOPED` を空にする。管理画面専用の関数を `admin.py` 以外が import していないこと（Q4） |

#### 分類表（`ModelClassificationTest`）

今ある表は「世界の表」の列のとおり。ワークスペースでは「League までの道」＋「League → Workspace の2経路」の形にし、今の道の末尾 `__world` を除いた道を共通にして League だけを特別扱いする。

| モデル | 世界の表（今） | ワークスペースでの分類 | 道 |
| --- | --- | --- | --- |
| `Workspace`（新） | — | 全体（自身） | — |
| `WorkspaceEditor`・`WorkspaceInvitation`（新） | — | メンバー | `workspace` |
| `PennantWorld` | ペナント専用 | 世界経由（直属） | `workspace` |
| `Stadium` | 共有 | ワークスペース直属 | `workspace` |
| `League` | リーグ経由 | 2経路（実データは `workspace`、ペナントは `world__workspace`。CheckConstraint で片方だけ） | — |
| `Team`・`PlayerStint`・`Captaincy` | リーグ経由 | リーグ経由 | `…__league` |
| `PennantFixture`・`PennantClubPlan`・`PennantClubPlanEntry` | リーグ経由 | リーグ経由 | 同上 |
| `Player`・`PennantPlayerRatings`・`PlayerFreeAgentDeclaration`（main #159） | 選手経由 | 在籍経由 | `…stints__team__league` |
| `Game` と明細8つ | 試合経由 | 試合経由 | 同上 |
| `Team.managers` の中間表 | 対象外（`get_models()` に出ない） | メンバーだが表には載らない | — |

- `Stadium` は今「共有＝世界を指さない」と検査されている（`test_shared_models_do_not_point_into_a_world`）。ワークスペースでは直属に変わるので、この検査も直す。
- 在籍経由の3つは「選手の在籍はすべて同じワークスペースにある」が前提で、DB では強制できない（3.3）。

### e2e（スモーク1本）

新規登録 → 見本つきで作成 → 招待リンクを発行 → 別のユーザーで受諾 → 担当チームの選手を編集 → 非公開のあいだは未ログインで見えず、公開にすると見える。

## 8. ドキュメント更新（epic を main に入れるとき）

| 対象 | 更新すること |
| --- | --- |
| Wiki に「ワークスペースと共有」を新設（利用者向け） | 作成・見本・公開と非公開・招待・役割と権限・上限 |
| Wiki「画面の歩き方」 | URL の木を `/w/<key>/` 以下に。役割ごとに、閲覧と書き込みの境界の表 |
| Wiki「チームと選手の管理」 | 「チーム担当者」の節を書き換える（`is_staff` の全権を削り、持ち主・編集者・担当に）。リーグ・チーム・球場はサイトの登録画面から登録する |
| Wiki「アーキテクチャ」 | 範囲（`WorldScope(workspace, world)`）、組み立て口の一覧、権限の置き場所（事実は参照クエリ、規則は `WorkspaceAccess`、適用はデコレータ） |
| Wiki「本番公開」（W0 で新設） | `--workspace` の指定、多数のワークスペースでの運用 |
| Wiki「仮想データの投入」「テストと品質」 | `--workspace`、漏れの検査 |
| `Home.md`・`_Sidebar.md` | 新しいページ |
| ROADMAP | フェーズ9 の各行を ✅ に。フェーズ5の「権限管理（担当者制）」に、管理ユーザーの全権をワークスペースの持ち主に置き換えたことを追記する |

## 9. 段階と順番

### ペナント epic（#26）との順番

**決定（ユーザー）: ペナントを P6 まで終えて main に入れてから、ワークスペースの epic に着手する。** 衝突が最も少ない。

採らなかった案:

- P5 まで入れたところで先に進める案は、ペナントの初期範囲を変えることになる。
- 今すぐ main で進める案は、範囲で絞る仕組みをワークスペース用と世界用に2回、別々のブランチで書くことになる。衝突は漏れに直結する場所（`repositories.py`・`queries.py`・`admin.py`・seed・`test_wiring`）に集中する。
- epic の上に epic を積む案は、打席の epic のときの前例に反する。

W0 はどちらにも依存しないので、先に main に入れる（#76）。

### 段階（`epic/workspaces` の sub-issue。どの段階でもフルテストが通る）

| 段階 | Issue | 内容 | 確認できること |
| --- | --- | --- | --- |
| W0 | #76（main に直接） | gunicorn・Cloudflare Tunnel・本番の settings（SQLite の `transaction_mode="IMMEDIATE"`、`init_command` で WAL と `synchronous=NORMAL`、`timeout` 20秒）、セキュリティ系の設定、静的ファイル、バックアップ | 本番相当の構成で今のアプリが動く |
| W1a | #77 | データの帰属。`domain/workspace.py`（`Visibility`・`Workspace` の最小形）、ORM の `Workspace`・`WorkspaceEditor`・`WorkspaceInvitation`、5. のマイグレーション3本、書き込み経路が新しい列を埋める、管理画面の絞り込み、テスト補助、分類表の新しい形。**範囲の型はまだ変えない** | 単独でフルテストが通る。実データの範囲は今のまま `world IS NULL` で動く。データのある DB ではワークスペースが1つでき、全リーグ・球場・世界が付く |
| W1b | [#172](https://github.com/sumika157/baseball-league-manager/issues/172) | 範囲の一般化。`WorldScope(workspace, world)`、`scoping` の書き換え（`players_in` の肯定形・`stadiums_in`・管理画面専用の関数）、世界の台帳の絞り込み、本拠地の検査、組み立て口16個を範囲必須に、管理画面と管理コマンドの `--workspace`。**URL はまだ変えない**（今の URL は一時関数で唯一のワークスペースに解決する） | 範囲で組み立てたサービスがもう一方のワークスペースの行を返さない。`UNSCOPED` が空。同じ DB で `measure_pages` が前後で変わらない |
| W2 | #78 | `/w/<key>/`、`@workspace_view`、`WorkspaceAccess` と参照クエリ、`is_staff` の特権の廃止、`scoped_url` の一般化、公開・非公開、`/` の一覧、全 URL の漏れの検査 | 移したデータを `/w/<key>/` で読める。非公開にすると未ログインでは見えない |
| W3 | #79 | リーグ・チーム・球場の登録画面、選手のプロフィール欄の編集、`LeagueSetupService` | 持ち主がブラウザだけでリーグを一から作れる |
| W4 | #80 | `WorkspaceService`: 作成（空）・設定・削除・上限・メンバー・招待・ヘッダーの切り替え | 2人目を招待して一緒に編集できる |
| W5 | #81 | 見本の生成（3.6） | 作成直後から順位表とボックススコアが見える。作成の所要時間を記録する |
| W6 | #82 | ペナントを `/w/<key>/pennant/` へ移し、権限をワークスペースに合わせる（3.1） | ペナントがワークスペースの中で動く |
| 仕上げ | #74 | 多数のワークスペースでの実測（10.）、Wiki への吸収、main へのマージ、デプロイ | — |

W1 を W1a と W1b に分けるのは、データの帰属（スキーマとデータ移行）と範囲の型の変更（本番25か所・テスト約130か所の呼び出しの直し）を別々に単独で通せるようにするため（決定 2026-10-07 Q1）。各段階の詳しい手順とテスト計画は Issue に書き、ここには要約だけを置く（#77・#172）。

W3 を W4 より先にするのは、W4 を先にすると、中身を入れられない空のワークスペースが一時的にできてしまうため。

#### 並び順の注記（2026-10-07）

- **#155（仮想データの作り直し。ペナントと支配下が main に入った後）**: 開発 DB の件数が変わる。#155 の後に W1 を始めるなら、`measure_pages` の比較の基準はその DB で取る。
- **戦力分析 G（#130。ペナントの世界の中へ）**: W6 の前後で URL の作り方が変わる。どちらが先かは W6 の着手時に決める。

## 10. パフォーマンスへの影響

| 項目 | 見込み | 確かめ方 |
| --- | --- | --- |
| 実データの画面 | 範囲の結合の深さは epic と同じ（Game → Team → League）。条件が `world IS NULL` から `workspace_id = X` に変わるだけで、FK の索引が効く | W1 の前後で、同じ DB に対して `measure_pages --workspace <key>` |
| 1回の要求ごとの権限の解決 | 多くて +3 クエリ | `measure_pages` のクエリ数 |
| 選手検索 | `Exists(在籍 → チーム → リーグ.workspace)`。epic と同じ形 | 同上 |
| 87万行・64MB の数字 | #155（仮想データの作り直し）の後に変わる | W1 の着手時に測り直す |
| **ワークスペースが多い状態** | 大きい表（打席・進塁）を全体で走査していないか。今の87万行は、1つの範囲の中の数字にすぎない | W6 の後、見本つきのワークスペースを200個作った DB（約 +180万行）で `measure_pages` と `--profile` を流す。SQL の時間ではなく、応答時間とクエリ数を見る |
| 見本の作成 | 約1〜1.5秒（36試合） | W5 で実測 |
| **SQLite の書き込みロック** | 書き込みは DB 全体で1本しか通らない。ペナントの「1週間進める」（2リーグで約1秒、8リーグで8〜11秒）や見本の作成のあいだ、ほかの全ユーザーの書き込みが待たされる | 待ち時間（`timeout`）を20秒にする。ペナントは1回に進める量を抑え、1日ごとにコミットを区切る。gunicorn は同期ワーカー2〜3、タイムアウト60秒 |
| 容量 | 1行あたり約77B。見本1つで約0.7MB。容量の大半はペナントの1シーズン（2リーグで約15MB）が占める | 上限（ワークスペース3つ × ペナントの上限）で頭打ちになる |

## 11. 見送り・縮小の選択肢

| 案 | 内容 | 失うもの | 再開の条件 |
| --- | --- | --- | --- |
| 読むだけの公開デモ（W0 だけ） | 今のアプリを運営者のデータで公開する。新規登録した人は書けない | 「自分で遊ぶ」 | ワークスペースを入れるまでのつなぎとして使える |
| 公開ワークスペースの一覧 | 2. のとおり見送る | 見つけやすさ | 通報と非表示の運用を用意できたら |
| ほかのワークスペースを元にしたペナント・見本の複製 | 範囲をまたぐ写しになる | 他人のリーグで遊ぶこと | 要望が出たら（「公開ワークスペースを自分のところへ複製する」機能として、分岐の仕組みを流用する） |
| 実データも世界の1行にする（3.1 の案 C） | — | — | PostgreSQL へ移すとき、または世界の種類が増えるとき |

## 12. 決定事項（2026-10-03 ユーザー）

| # | 判断 | 決定 |
| --- | --- | --- |
| Q1 | ペナント epic との順番 | ペナントを P6 まで終えてから |
| Q2 | 移した既存データ | 運営者が持ち主の公開ワークスペースにし、トップから見本として案内する |
| Q3 | 公開ワークスペースの見つけ方 | URL を知っている人だけ（一覧は作らない） |
| Q4 | 見本の規模 | 1リーグ6チーム・各25人・約36試合。作成時の既定はオン |
| Q5 | 共同編集するワークスペースでのペナントの世界 | 進める・編成するのは世界の作成者。持ち主は削除もできる |
| Q6 | サイトの登録画面の範囲 | リーグ・チーム・球場の作成と編集、選手のプロフィール欄（過去の在籍と主将の在任歴の編集は後回し） |
| — | 数値 | 1人が持てるワークスペースは3、招待の期限は7日、key は10文字のランダムな英小文字と数字 |
| — | 公開先 | 当面は AWS 想定。最も安い構成にする（14.）。DB は当面 SQLite（2026-10-05） |

### 決定事項（2026-10-07 ユーザー。W1 着手前の照合で。番号は上の表と別に振る）

| # | 判断 | 決定 |
| --- | --- | --- |
| Q1 | W1 の分け方 | W1a（データの帰属）と W1b（範囲の一般化）に分ける（9.） |
| Q2 | W1 の間の今の URL | 一時関数（例 `_legacy_workspace_id()`。id が最小のワークスペース、無ければ 404）で唯一のワークスペースに解決する。W2 で消す |
| Q3 | 世界の台帳 | W1b でワークスペースで絞り、`test_wiring.py` の `UNSCOPED` を空にする。画面（`/pennant/`）は W6 まで変えない |
| Q4 | 管理画面の範囲 | 管理画面だけは全ワークスペースの実データを読んでよい例外。`scoping` に管理画面専用の関数を1つ置き（全ワークスペースの実データ＋在籍の無い選手）、`admin.py` 以外が import していないことをテストで縛る |

## 13. 公開先と費用

> 料金・無料枠・プランの中身は 2026-10 時点の目安で、公式の情報では確かめていない。公開するときに確かめ直す。

### 採用する構成（AWS 想定の最安、2026-10-05 ユーザー）

| 部品 | 選ぶもの | 月額の目安 | 理由 |
| --- | --- | --- | --- |
| サーバー | Lightsail 1GB（IPv4 付き） | 約 $7 | 512MB では主要画面（Python 側が重い）と複数のワーカーが厳しい |
| DB | SQLite（VM の中） | 0 | PostgreSQL を同居させると 2GB のプランが要り、月 +$5 前後になる |
| 入口・HTTPS | Cloudflare Tunnel | 0 | VM の 80/443 を開けずに済み、証明書の管理も要らない。静的 IP も要らない |
| ボット対策 | Cloudflare Turnstile | 0 | 新規登録の差し込み口（3.5）に入れる |
| バックアップの置き場 | Cloudflare R2（無料枠） | 0 | Lightsail の自動スナップショットは有料なので使わない |
| イメージ | GitHub Actions でビルドして GHCR に置く | 0（公開リポジトリなら） | 1GB の VM で Node のビルドまで行うとメモリが足りなくなるおそれがある。CI と一緒に別の Issue で扱う |
| ドメイン | Cloudflare Registrar | 約 $1（年 $10 前後） | Tunnel で正式に公開するにはドメインが要る |

合計は月 $8 前後。gunicorn の同期ワーカーは2本にする（メモリに余裕を残すため）。

さらに削れるが採らないもの:

- **IPv6 だけのプラン（約 $2 安い）**: Tunnel なら外から届く IPv4 は要らない。ただし VM から GitHub に届くか、手元から SSH できるかを確かめる手間が増える。
- **512MB のプラン**: ワーカーが1本になり、遅い画面があると全員が待たされる。

### Terraform で作る（最終形、#89）

上の構成は、最終的には Terraform で作り直せるようにする（2026-10-05 ユーザーの要望）。それまでは Wiki「本番公開」の手順で手で作る。

- 対象: Lightsail のインスタンスとポートの設定、Cloudflare のトンネルと転送の設定・DNS・ゾーンの設定・R2・Turnstile。VM の初期設定は cloud-init で行う
- やらない: ドメインの購入、CI からの `apply`、アプリの更新（デプロイ）。Terraform は初回の起動までを担う
- 前提: W0（#76）と CI（#88。VM は GHCR のイメージを pull する）の後
- 決定（2026-10-05）: state の置き場は Cloudflare R2（非公開バケット。state のバケットだけは手で作る）。Terraform は**アプリの起動まで全部**作る（VM の初期設定は cloud-init、`.env` は Terraform が SSH で書く。秘密を cloud-init に入れないため。`DJANGO_SECRET_KEY` も Terraform が生成する）。秘密が残るのは state だけ。ドメイン・アカウント・API トークンは手作業。実装は `infra/`、手順は Wiki「本番公開」（実機では未確認）

### PostgreSQL へ移る条件

**書き込みの待ちが目に見えるようになったら移る**（ペナントを進めている間に、ほかの人の保存が遅れる、など）。まずは VM の中にコンテナで同居させる（2GB のプランにして、月 +$5 前後）。

移るときに気をつけること（どれもエラーにならずに出る）:

- `AVG` などの集計が `Decimal` を返すことがある
- 昇順で NULL が末尾に並ぶ（SQLite は先頭）
- `icontains` などの大文字小文字の扱い
- 速さの出方が変わる（`measure_pages` で測り直す）
- `backup_db` は SQLite 専用なので、`pg_dump` に置き換える

ワークスペースの epic より前に、単独の作業として移るのが望ましい（epic のテストを最初から PostgreSQL で流せるため）。

### AWS 以外の選択肢（メモ。検討はしていない）

| 種類 | 例 | 費用の目安 | 長所 | 気をつける点 |
| --- | --- | --- | --- | --- |
| 常時無料の VM | Oracle Cloud の Always Free（ARM の VM） | 0 | 無料のわりにメモリが大きい | 使われていない無料のインスタンスを回収される、アカウントが止められるといった話がある。東京リージョンの空きが無いことがある。ARM なのでイメージを arm64 でビルドする |
| 常時無料の VM | Google Cloud の無料枠（e2-micro） | 0 | 規約が比較的明確 | 米国のリージョンに限られる（日本からは遅い）。メモリが小さい |
| 国内の VPS | さくらの VPS・ConoHa VPS・XServer VPS など | 月 数百円〜1,000円程度 | 日本から近い。円で払える | AWS とほぼ同じ運用（自分で OS を管理する） |
| 海外の VPS | Hetzner・Vultr・DigitalOcean など | 月 $4〜6 程度 | 安い。性能あたりの価格がよい | 日本のリージョンが無いところがある |
| PaaS | Fly.io・Render・Railway など | 月 0〜数ドル（従量） | OS の管理が要らない | SQLite を使うには永続ディスクが要る。無料枠はスリープ・削除・縮小が多い |
| 自宅のマシン | 手元の PC やミニ PC ＋ Cloudflare Tunnel | 電気代だけ | Tunnel なのでルーターのポートを開けなくてよい | マシンを止めるとサイトも止まる。回線と停電の影響を受ける |
| Cloudflare だけ | Workers・Pages | — | — | **向かない。** Django は常に起動しているプロセスが要り、SQLite は消えないディスクを要る。D1 は Django の ORM から使えない |

どの選択肢でも、入口は Cloudflare Tunnel、静的ファイルは WhiteNoise、DB は当面 SQLite という W0 の作りをそのまま使える。

## 14. 段階ごとの記録

（段階を終えるごとに、実測値と、実装中に決めたことを追記する）
