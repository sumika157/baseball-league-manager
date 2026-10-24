# 設計: ワークスペース（マルチテナント化）と一般公開

> 状態: 設計確定（W1a の T1＝domain だけ着手）。親 Issue [#74](https://github.com/sumika157/baseball-league-manager/issues/74)、子 Issue は #75（この設計書）・#76〜#82（段階 W0〜W6。W1 は #77 の W1a と #172 の W1b に分ける。9.）。
> 2026-10-11 に main（dd5d44b）と照合して直した（3.1）。ペナントの epic（#26）は #185 で main に入った。
> 着手はペナントモード（#26）を P6 まで終えて main に入れた後（済み）。W0（本番構成）だけは epic の外で先に main へ入れる。
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

#### epic の基点 9994290（main dd5d44b ＋ #193）で確かめた事実（2026-10-11 時点。ペナントの epic は #185 で入った。#184 は main に入った（c666b34）ので T3 の前提は満たされ、epic は T3 の前に main を取り込む。#168 が入ると変わる）

| 項目 | 現状 | 根拠 |
| --- | --- | --- |
| 世界の出典 | `League.world`（FK。null なら実データ） | `orm_models.py` の `League.world` |
| `League` の一意制約 | `world IS NULL` のときの `name`（`unique_real_league_name`）と `(world, name)`（`unique_world_league_name`）。作るのは `0042_pennant_world_and_league_world.py` | `orm_models.py` の `League.Meta` |
| `WorldScope` の形 | `WorldScope(world_id: int \| None)`、`real()`・`pennant(world_id)`、既定値なし。frozen dataclass で `is_real`・`is_pennant`・`__str__` を持つ。`World.scope` が `pennant(id)` を返す | `domain/pennant/world.py:47-77`、`:112-116` |
| `WorldScope` を作る箇所 | 本番コード27か所（views 18・`admin.py:118` の `_REAL`・`DjangoWorldRepository.delete`・`World.scope`・管理コマンド6）。テスト154行・40ファイル（W1b T1 の前。T1 で views は 2 か所の補助に集まり、テストは `tests/helpers.py` の `real_scope()`・`world_scope()` に寄せた。14.） | `git grep` |
| `scoping` の関数 | `leagues_in`・`teams_in`・`stints_in`・`games_in`・`fixtures_in`・`ratings_in`・`players_in`・`club_plans_in`（P5a）。台帳用に `world_condition(path, scope)`・`world_condition_in(path, world_ids)`・`leagues_in_worlds`・`games_in_worlds`・`fixtures_in_worlds`。範囲ではないが同じファイルに `period_covering(year)`（#95） | `scoping.py:22,34,43,47,51,97,102` |
| 選手は世界の列を持たない | 在籍をたどって決まる。ただし実データ側は**否定形**（ペナントの球団に在籍したことが無い選手）で、在籍の無い選手も実データに含まれる（管理画面から隠さないため）。開発 DB の在籍の無い選手は0人 | `scoping.py:82-94` |
| 球場 | 世界をまたいで共有し、ペナントは読むだけ（分類 `SHARED`）。ただし `DjangoTeamRepository.save` は `home_stadium_id` を範囲で検査せずに書く | `repositories.py:233-251` |
| 書き込みの判定 | `PennantWorld.owner`（`_is_owner`・`_requires_world_owner`）。owner は `SET_NULL`。世界の上限は1人5つ（`MAX_WORLDS_PER_OWNER=5`、`count_by_owner`）、シーズンは `MAX_SEASONS_PER_WORLD=10` | `views.py:401,406`、`world.py:28-30` |
| 世界の一覧 | 誰でも見られる。台帳は範囲を持たない設計で、`test_wiring.py:94` の `UNSCOPED = {DjangoWorldRepository, DjangoWorldSummaryQuery}` で検査から外してある。`find_all`・`list_all` は全世界を読む | `repositories.py:1359-`、`queries.py:758-` |
| 組み立て口 | T1 の前は18個（#105 の `build_pennant_history_service`、#193 の `build_world_team_analysis_service` を含む）。T1 で `build_world_view_service`・`build_world_team_analysis_service` を `build_service(scope)`・`build_team_analysis_service(scope)` に吸収して16個。すべて `scope` を既定値なしで受け取る（台帳だけの `build_pennant_world_view` は T3 まで範囲を取らない） | `views.py` |
| `scoped_url` | 入っている。context の `world` が無ければ `reverse(name)`、あれば `reverse("pennant_" + name, (world_id, *args))`。テンプレートで `{% scoped_url %}` 54か所、素の `{% url %}` 97か所（#130 などで変わっている。W2 の着手時に数え直す） | `templatetags/scoped.py:17-22`、`urls.py` の `_pennant()` |
| マイグレーション | main は `0045_pennant_club_plan` まで。#168 が `0046_playerstint_left_via` を取るので、**ワークスペースは 0047 から始まる**（#168 を取り込んだ後に番号を確かめる） | `myapp/migrations/` |

組み立て口（16個。W1b T1 の後）の範囲の受け方:

| 範囲の受け方 | 組み立て口 |
| --- | --- |
| 実データまたはペナントの世界（どちらも `scope` で受ける） | `build_service`・`build_team_analysis_service`（実データなら年の基準は暦の今日、世界ならその世界の今日） |
| 実データ専用（ペナントの範囲なら `InvalidWorld`） | `build_recording_service`・`build_roster_service`・`build_permission_query`・`build_player_search_query`・`build_pennant_world_service`（分岐元が実データ） |
| ペナント専用（実データの範囲なら `InvalidWorld`） | `build_pennant_ratings_service`・`build_pennant_season_service`・`build_pennant_offseason_service`・`build_offseason_view_service`・`build_pennant_home_service`・`build_pennant_history_service`・`build_club_service`・`build_club_details_service` |
| 範囲を持たない | `build_pennant_world_view`（T3 で `workspace_id` を取る） |

補助は `_repositories_for(scope)` と `_world_clock(world_id, ...)`。範囲を作るのは views の `_real_scope()`・`_world_scope(world_id)`（`views.py:185,190`）だけで、`api.py`・`templatetags/admin_overview.py` もこれを使う。共有ビューの範囲を決める分岐は `_scope(world_id)`（`views.py:490`）と、戦力分析用の `_analysis_scope(world_id)`（`views.py:312`）の2か所だが、どちらも「世界の有無で範囲を作って `build_service(scope)` を呼ぶ」だけになった。`test_wiring.py` は全組み立て口を検査している。

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

W1a（T1）で入れるのは `Visibility` と `Workspace` の最小形（`id`・`key`・`name`・`owner_id`・`visibility`。名前と key の検査、key を作る関数）、例外は `InvalidWorkspace` だけ。`Invitation`・`InvitedRole`・`InvalidInvitation` と、その永続化（`WorkspaceInvitation` の表）は **W4 で作る**（決定 2026-10-11。W1a では書く人も読む人もいない表になり、役割の choices とチームの M2M を先に固めることになるため）。残りも後の段階で足す。

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
- `domain/pennant/world.py`: `WorldScope(workspace_id, world_id)`（W1b）、`World.workspace_id: int | None = None`（W1a。保存時に埋まり、W1b で範囲から渡す）
- `domain/repositories.py`: `WorkspaceRepository`（`find_by_key` / `find_by_id` / `save` / `delete`）・`InvitationRepository`（W4）・範囲つきの `StadiumRepository` を Protocol で足す
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
  - 管理画面に登録されているのは League・Team・Stadium・Player・PlayerStint・Game の6モデルだけ（ペナントのモデルは登録しない方針のまま）。一覧にワークスペースの列と絞り込みを足す（League・Stadium は直属、Team・PlayerStint・Game はリーグ経由、Player は在籍経由）。
  - **`Workspace` 自体も登録する**（空の DB で運営者が最初の1つを作る手段。`WorkspaceEditor` はインライン）。W1a の T7。
  - 外部キーの選択肢は、編集中の行と同じワークスペースのものに限る。**追加画面では行のワークスペースがまだ決まっていないので選択肢を絞れない**。そこで `clean()` でも「同じワークスペースか」を検査し、範囲をまたぐ組み合わせはフォームのエラーにする（選択肢を絞るのは導線、検査が本体）。
  - 今ある `_REAL` 定数（`admin.py:111`）と `RealDataOnlyMixin`（`_REAL_QUERYSETS`）は、「全ワークスペースの実データを読む」前提に変わる。`DjangoTeamRepository(_REAL)`・`DjangoLeagueRepository(_REAL)` を直接作っている箇所（TeamAdmin・在籍の検査）は、行のワークスペースから範囲を作る。一覧は 3.1 の管理画面専用の関数で読む（Q4）。
  - ダッシュボードのテンプレートタグ `admin_overview` は、範囲を必須にすると呼べなくなる。そこで運営向けの概況（ワークスペース数・ユーザー数・行数の多いワークスペース）に置き換える。
- 管理コマンド: `seed_virtual_players` / `seed_virtual_games` / `measure_pages` / `rebuild_fielding_lines` / `pennant_create`（分岐元のリーグを選ぶ側）、`simulate_sample --from-real-leagues`（`build_pennant_world_service` 経由で実データのリーグを読む。このときだけ）は `--workspace <key>` を必須にする。`measure_pages` の `--workspace` は W2。`pennant_advance` / `pennant_close_season` / `pennant_delete` は `--world` から世界のワークスペースを導く。`simulate_sample`（`--from-real-leagues` なし）・`backup_db` は対象外。

### presentation

- `views.py`:
  - 今ある組み立て口（3.1 の表。T1 の後は16個）は、すべて範囲（またはワークスペースの id）を必須の引数にする（既定値は無し）。ペナント用は「台帳で世界を引いてから `WorldScope.pennant(ws, w)` を作る」補助を1つ置く。
  - 範囲の分岐は `_scope(world_id)` と `_analysis_scope(world_id)` の2か所（W1b で1か所にまとめられるか見る）。W1 の間は一時関数（id が最小のワークスペース、無ければ日本語の例外）で今の URL を唯一のワークスペースに解決し、W2 で消す（決定 2026-10-07 Q2）。**一時関数は views ではなく infrastructure に置く**（`infrastructure/workspaces.py` の `legacy_workspace_id()`。W1a の書き込み経路＝リポジトリでも要るため。W1b の views もこれを使い、別の実装を置かない）。`templatetags/admin_overview.py` も W1 ではこの一時関数の範囲で呼ぶ。
  - P4a の `build_world_view_service(world_id)` は、W1b で `build_service(WorldScope.pennant(ws, w))` に吸収した（T1 で実施）。
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

ペナントの epic を main に入れた後で作る（epic にも複数のマイグレーションがあり、並行して作ると番号が衝突するため）。番号は仮に N とする。2026-10-11 時点で main は `0045` まで、#168 が `0046_playerstint_left_via` を取るので、**N は 0047**（#168 を epic に取り込んだ後に確かめる）。

| ファイル | 種類 | 内容 |
| --- | --- | --- |
| `00N_workspace` | スキーマ | `Workspace`（`key` は一意。10文字のランダムな英小文字と数字）・`name`・`owner`（PROTECT）・`visibility`（choices は `Visibility` から）・`created_at`。`WorkspaceEditor`（workspace と user の組で一意）。`League.workspace`・`Stadium.workspace`・`PennantWorld.workspace` を **null 可・`on_delete=PROTECT`** で足す（ワークスペースの削除は範囲で絞った削除で行い、Django の CASCADE の collector には任せない）。`WorkspaceInvitation`（`token_digest` は一意、role、teams の M2M、`expires_at`、`created_by`）は **W4 で作る**（決定 2026-10-11。W4 でマイグレーションが1本増える） |
| `00N+1_backfill_default_workspace` | データ移行 | リーグ・球場・世界の行が1つでもあれば、ワークスペースを1つ作る。公開にする。持ち主は id が最も小さい superuser（いなければ staff。どちらもいなければ「先に createsuperuser を」という日本語の例外で止める）。**ほかの staff は `WorkspaceEditor` にする**（今の全権を失わせないため）。`world IS NULL` のリーグ・全球場・全世界をこのワークスペースに付ける。逆方向の処理も書く |
| `00N+2_workspace_constraints` | スキーマ | `Stadium.workspace` と `PennantWorld.workspace` を null 不可にする。`Stadium.name` の `unique=True` を外し、`UniqueConstraint(workspace, name)` にする。League は epic の `name WHERE world IS NULL`（`unique_real_league_name`）を外して `UniqueConstraint(workspace, name)` にする（`(world, name)` は残す）。CheckConstraint で、`workspace` と `world` のちょうど片方だけを持つようにする |

- **既存データへの影響:** 更新するのはリーグ・球場・世界の数十行だけ。試合と明細（約87万行・64MB）には触らない（範囲はリーグからたどるため）。
- 開発 DB の件数（2026-10-11 時点。実データのリーグ8・ペナントのリーグ0・球場21・世界0・チーム48・選手1,619・在籍の無い選手0・`Team.managers` 0行・ユーザー3人のうち staff 2・superuser 2）は、#175（仮想データの作り直し。#155 は閉じた）の後に変わるので、着手時に測り直す。
- **分岐で作る世界のワークスペース（W1a）:** `World` は `workspace_id: int | None = None` を持つが、W1a の範囲（`WorldScope`）はワークスペースを運ばない。そこで `DjangoWorldRepository.save` が、`workspace_id` が None なら `legacy_workspace_id()` を入れる。W1b で `PennantWorldService` が範囲のワークスペースを渡す形に替える。ペナントの世界のリーグ自身は `workspace` を持たない（`world` からたどる）。
- テスト側の直接作成は `League.objects.create` が46か所・23ファイル（e2e 4ファイルを含む）、`Stadium.objects.create` が9か所・4ファイル、`PennantWorld.objects.create` が1か所。T3 でテスト補助に寄せる。
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
| `test_workspace_service.py`（W4 で作る） | 作成（見本あり・なし）、上限、削除で全行が消えて他のワークスペースの行は残ること、招待の受諾と除名（除名すると担当も外れる） |
| `test_league_setup_screens.py` | 作成・編集・空のときだけ削除できること。名前の一意がワークスペースの中だけで効くこと |
| `test_world_isolation.py` の `ModelClassificationTest`（epic の `CLASSIFICATION` を拡張。独立したファイルは作らない） | myapp の全モデルを、範囲のたどり方で分類する。分類に漏れがあれば落とす。分類表は下の表 |
| `test_workspace_migration.py`（`test_jersey_number.py` の `MigrationToTextTest` に倣う） | データのある DB ではワークスペースが1つでき、**実データのリーグ**（ペナントのリーグは除く）・全球場・全世界が付く。持ち主は最小 id の superuser（いなければ staff）、ほかの staff は編集者、staff でも superuser でもない人は編集者にならない。どちらもいなければ日本語の例外で止まる。空の DB では何もできない。逆方向は何もせず、再び適用してもワークスペースが増えない |
| `test_admin.py`（拡張） | 管理画面の外部キーの選択肢が、同じワークスペースのものに限られる。在籍の無い選手が管理画面の一覧に出る（Q4） |
| `test_seed_commands_scope.py`（拡張） | `--workspace` が必須で、ほかのワークスペースに触れない。`pennant_create --workspace` が他所のリーグを分岐元にできない |
| `test_wiring.py`（拡張） | 17個＋新しい `build_*` のすべてと、範囲が必須であること。`ScopeIsRequiredTest` を「`scope` か `workspace_id` が必須」に広げ、`UNSCOPED` を空にする。管理画面専用の関数を `admin.py` 以外が import していないこと（Q4） |

#### 分類表（`ModelClassificationTest`）

今ある表（main の `test_world_isolation.py` の `CLASSIFICATION`）は「世界の表」の列のとおり。ワークスペースでは「League までの道」＋「League → Workspace の2経路」の形にし、今の道の末尾 `__world` を除いた道を共通にして League だけを特別扱いする。

| モデル | 世界の表（今） | ワークスペースでの分類 | 道 |
| --- | --- | --- | --- |
| `Workspace`（新） | — | 全体（自身） | — |
| `WorkspaceEditor`（新。`WorkspaceInvitation` は W4 で足す） | — | メンバー | `workspace` |
| `PennantWorld` | ペナント専用 | 世界経由（直属） | `workspace` |
| `Stadium` | 共有 | ワークスペース直属 | `workspace` |
| `League` | リーグ経由 | 2経路（実データは `workspace`、ペナントは `world__workspace`。CheckConstraint で片方だけ） | — |
| `Team`・`PlayerStint`・`Captaincy` | リーグ経由 | リーグ経由 | `…__league` |
| `PennantFixture`・`PennantClubPlan`・`PennantClubPlanEntry` | リーグ経由 | リーグ経由 | 同上 |
| `Player`・`PennantPlayerRatings`・`PlayerFreeAgentDeclaration`（main #159） | 選手経由 | 在籍経由 | `…stints__team__league` |
| `Game` と明細8つ | 試合経由 | 試合経由 | 同上 |
| `Team.managers` の中間表 | 対象外（`get_models()` に出ない） | メンバーだが表には載らない | — |

- `Stadium` は今「共有＝世界を指さない」と検査されている（`test_shared_models_do_not_point_into_a_world`）。ワークスペースでは直属に変わるので、この検査も直す。
- 球場の扱いに関わる既存のテストも、球場がワークスペース直属になっても意味が通るかを見直す: `test_the_stadium_is_shared`（実データの画面）と `test_editing_a_stadium_does_not_detach_the_world_teams`（管理画面）。
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
| W1a | #77 | データの帰属。`domain/workspace.py`（`Visibility`・`Workspace` の最小形）、ORM の `Workspace`・`WorkspaceEditor`（`WorkspaceInvitation` は W4）、5. のマイグレーション3本（0047〜0049）、書き込み経路が新しい列を埋める（分岐で作る世界を含む。5.）、管理画面の絞り込みと `Workspace` の登録、テスト補助、分類表の新しい形。**範囲の型はまだ変えない**。タスク PR 7本（T1〜T7）に分けて `epic/workspaces` に入れる | 単独でフルテストが通る。実データの範囲は今のまま `world IS NULL` で動く。データのある DB ではワークスペースが1つでき、実データのリーグ・球場・世界が付く |
| W1b | [#172](https://github.com/sumika157/baseball-league-manager/issues/172) | 範囲の一般化。`WorldScope(workspace, world)`、`scoping` の書き換え（`players_in` の肯定形・`stadiums_in`・管理画面専用の関数）、世界の台帳の絞り込み、本拠地の検査、組み立て口を範囲必須に、管理画面と管理コマンドの `--workspace`。**URL はまだ変えない**（今の URL は一時関数で唯一のワークスペースに解決する） | 範囲で組み立てたサービスがもう一方のワークスペースの行を返さない。`UNSCOPED` が空。同じ DB で `measure_pages` が前後で変わらない |
| W2 | #78 | `/w/<key>/`、`@workspace_view`、`WorkspaceAccess` と参照クエリ、`is_staff` の特権の廃止、`scoped_url` の一般化、公開・非公開、`/` の一覧、全 URL の漏れの検査 | 移したデータを `/w/<key>/` で読める。非公開にすると未ログインでは見えない |
| W3 | #79 | リーグ・チーム・球場の登録画面、選手のプロフィール欄の編集、`LeagueSetupService` | 持ち主がブラウザだけでリーグを一から作れる |
| W4 | #80 | `WorkspaceService`: 作成（空）・設定・削除・上限・メンバー・招待（`Invitation`・`InvitedRole` と `WorkspaceInvitation` の表・マイグレーションはここで作る）・ヘッダーの切り替え | 2人目を招待して一緒に編集できる |
| W5 | #81 | 見本の生成（3.6） | 作成直後から順位表とボックススコアが見える。作成の所要時間を記録する |
| W6 | #82 | ペナントを `/w/<key>/pennant/` へ移し、権限をワークスペースに合わせる（3.1） | ペナントがワークスペースの中で動く |
| 仕上げ | #74 | 多数のワークスペースでの実測（10.）、Wiki への吸収、main へのマージ、デプロイ | — |

W1 を W1a と W1b に分けるのは、データの帰属（スキーマとデータ移行）と範囲の型の変更（本番26か所・テスト134か所の呼び出しの直し）を別々に単独で通せるようにするため（決定 2026-10-07 Q1）。各段階の詳しい手順とテスト計画は Issue に書き、ここには要約だけを置く（#77・#172）。

W3 を W4 より先にするのは、W4 を先にすると、中身を入れられない空のワークスペースが一時的にできてしまうため。

#### 並び順の注記（2026-10-07）

- **#175（仮想データの作り直し。#155 は閉じてここに移った）**: 開発 DB の件数が変わる。#175 の後に W1b を始めるなら、`measure_pages` の比較の基準はその DB で取る。
- **戦力分析のペナント版（#130）**: #193 で main に入った。W6 の前後で URL の作り方が変わる点は残る（組み立て口が1つ増え、範囲の分岐点も `_analysis_scope` が加わった。3.1）。

## 10. パフォーマンスへの影響

| 項目 | 見込み | 確かめ方 |
| --- | --- | --- |
| 実データの画面 | 範囲の結合の深さは epic と同じ（Game → Team → League）。条件が `world IS NULL` から `workspace_id = X` に変わるだけで、FK の索引が効く | W1 の前後で、同じ DB に対して `measure_pages --workspace <key>` |
| 1回の要求ごとの権限の解決 | 多くて +3 クエリ | `measure_pages` のクエリ数 |
| 選手検索 | `Exists(在籍 → チーム → リーグ.workspace)`。epic と同じ形 | 同上 |
| 87万行・64MB の数字 | #175（仮想データの作り直し）の後に変わる | W1 の着手時に測り直す |
| **ワークスペースが多い状態** | 大きい表（打席・進塁）を全体で走査していないか。今の87万行は、1つの範囲の中の数字にすぎない | W6 の後、見本つきのワークスペースを200個作った DB（約 +180万行）で `measure_pages` と `--profile` を流す。SQL の時間ではなく、応答時間とクエリ数を見る |
| 見本の作成 | 約1〜1.5秒（36試合） | W5 で実測 |
| **SQLite の書き込みロック** | 書き込みは DB 全体で1本しか通らない。ペナントの「1週間進める」（2リーグで約1秒、8リーグで約3秒。負荷時は 8〜11秒。#100 の後は1回の書き込みロックが最長約1〜2秒）や見本の作成のあいだ、ほかの全ユーザーの書き込みが待たされる | 待ち時間（`timeout`）を20秒にする。ペナントは1回に進める量を抑える（画面から進めるのは150試合まで）。書き込みは `FLUSH_EVERY_GAMES`（200試合）ごとの短いトランザクションに分け、画面の1回の「進める」は複数のトランザクションになる（1日ごとにコミットするのではない）。検査と行の組み立ては `atomic` の外で済ませ、中は対戦を消して書くだけにする。シーズンを締める処理も外で計算し、中で読み直して書く（詳細は Wiki「ペナントモードの仕組み」の「書き込みとロック」）。gunicorn は同期ワーカー2〜3、タイムアウト60秒 |
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
| — | 数値 | 1人が持てるワークスペースは3、招待の期限は7日、key は10文字のランダムな英小文字と数字、ワークスペースの名前は100文字まで（`domain/workspace.py` の `MAX_NAME_LENGTH`。T2 の ORM の `max_length` はこの定数を import し、100 を直書きしない） |
| — | 公開先 | 当面は AWS 想定。最も安い構成にする（14.）。DB は当面 SQLite（2026-10-05） |

### 決定事項（2026-10-07 ユーザー。W1 着手前の照合で。番号は上の表と別に振る）

| # | 判断 | 決定 |
| --- | --- | --- |
| Q1 | W1 の分け方 | W1a（データの帰属）と W1b（範囲の一般化）に分ける（9.） |
| Q2 | W1 の間の今の URL | 一時関数（例 `_legacy_workspace_id()`。id が最小のワークスペース、無ければ 404）で唯一のワークスペースに解決する。W2 で消す |
| Q3 | 世界の台帳 | W1b でワークスペースで絞り、`test_wiring.py` の `UNSCOPED` を空にする。画面（`/pennant/`）は W6 まで変えない |
| Q4 | 管理画面の範囲 | 管理画面だけは全ワークスペースの実データを読んでよい例外。`scoping` に管理画面専用の関数を1つ置き（全ワークスペースの実データ＋在籍の無い選手）、`admin.py` 以外が import していないことをテストで縛る |

### 決定事項（2026-10-11 ユーザー。W1a の計画の見直しで）

| # | 判断 | 決定 |
| --- | --- | --- |
| R1 | epic を切る時期 | 今切り、T1（domain だけ）から始める。T2 は #168（`0046`）が main に入って epic に取り込んだ後、T3 は #184 の後 |
| R2 | 招待の表（`WorkspaceInvitation`）を作る段階 | W4 で作る。W1a では作らない（5.・9.） |

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

### W1a（#77）

- 決定（2026-10-11）: `legacy_workspace_id()` は `infrastructure/workspaces.py` に置く（views には置かない）。W1a のリポジトリの書き込み経路と W1b の views が同じ関数を使い、W2 で消す。
- 決定（2026-10-11）: 招待の表（`WorkspaceInvitation`）と `Invitation`・`InvitedRole` は W4 で作る。
- T1（`feature/workspace-domain`）: `domain/workspace.py`（`Visibility`・`Workspace`・key の生成と検査）、`InvalidWorkspace`、`World.workspace_id` を入れた。ORM・マイグレーションは T2 以降。
- T2（`feature/workspace-schema`）: マイグレーション `0055_workspace`（main の `0054_remove_player_debut_year` の次。当初は 0048、のち 0049・0052 で、#168 段階B を取り込むときに 0049 へ、#204・#248 を取り込むとき（2026-10-16）に 0052 へ振り直した。スキーマだけ）で `Workspace`・`WorkspaceEditor` を作り、`League`・`Stadium`・`PennantWorld` に null 可・PROTECT の `workspace` を足した。`Workspace.visibility` と `name` の長さはドメイン（`Visibility.choices()`・`MAX_NAME_LENGTH`）から取り、既定値は持たせない（ドメインの `Workspace` と同じ）。`DjangoWorldRepository` が `workspace_id` を読み書きする（保存時に埋める `legacy_workspace_id()` は T4）。分類表は「League までの道（今の道から末尾の `__world` を除く）」の形にし、`Workspace`＝全体・`WorkspaceEditor`＝メンバー・`PennantWorld`／`Stadium`＝直属・`League`＝2経路（`LEAGUE_ROUTES`）とした。`test_shared_models_do_not_point_into_a_world` は「直属・メンバーのモデルはリーグ以下を指さない」に替えた（例外は `PennantWorld.managed_team` だけ）。実測値は無い（スキーマだけで、読み取りの経路は変えていない）。
- T4（`feature/workspace-write-paths`）: `infrastructure/workspaces.py` の `legacy_workspace_id()`（id が最小のワークスペース。無ければ `WorkspaceNotFound`＝`DomainError` の日本語メッセージなので、世界の作成の画面・管理コマンドは既存の `except DomainError` でメッセージにする）を入れた。書き込み経路で新しい行を作るのは次の3つだけだった（管理コマンドの `seed_*` は球団・選手・試合だけでリーグ・球場・世界は作らない。`bulk_create` はどれも試合・成績・能力・日程の行）。
  - `DjangoLeagueRepository.save`: 新しいリーグだけ帰属を決める。実データの範囲は `workspace_id = legacy_workspace_id()`・`world_id = None`、ペナントの範囲は `workspace_id = None`・`world_id = scope.world_id`。既存のリーグの workspace・world は書き換えない。分岐のリーグはここを通るので workspace は空のまま。
  - `DjangoWorldRepository.save`: `world.workspace_id` が空なら `legacy_workspace_id()` を入れ、集約にも書き戻す（分岐で作る世界）。W1b で `PennantWorldService` が範囲のワークスペースを渡す形に替える。
  - 管理画面: League と Stadium のフォームに `workspace` を出して必須にした（`WorkspaceRequiredForm`）。選び忘れはフォームのエラーで、500 にならない。球場の管理画面は管理画面から作るだけで、リポジトリを通らない。
- T5（`feature/workspace-backfill`）: マイグレーション `0056_backfill_default_workspace`（0055 の次。当初は 0049、のち 0050・0053。`RunPython` だけでスキーマ変更は無い）。
  - リーグ・球場・世界のどれかが1行でもあれば、ワークスペースを1つ作る（公開。名前は「既定のワークスペース」。管理画面から後で変えられる）。何も無い DB では作らない。
  - 持ち主は id が最小の superuser、いなければ id が最小の staff。どちらもいなければ `RuntimeError`（「先に createsuperuser を実行してください」を含む日本語）で止まり、マイグレーションは適用されない。ほかの staff は `WorkspaceEditor`、staff でない人は編集者にしない。`Team.managers` には触れない。
  - `world IS NULL` のリーグ・全球場・全世界に付ける。ペナントのリーグには付けない。
  - key はマイグレーション内で `random.SystemRandom()` から作る（英小文字と数字の10文字。domain の `KEY_LENGTH`・`KEY_ALPHABET` を写した。重複したら引き直す）。
  - 全権を持っていた人は「staff または superuser」（`infrastructure/queries.py` と同じ）なので、持ち主以外のその両方を編集者にする。持ち主・編集者とも `is_active` な人だけ。エラーの文には本番の復旧コマンド（`docker compose -f docker-compose.prod.yml run --rm --no-deps --entrypoint python web manage.py createsuperuser`）を入れた。再起動を繰り返すコンテナには `exec` が効かないため。
  - 逆方向は何もしない（`noop`）。0048 では列が残るので帰属はそのままでよく、0048 より前へ戻れば列ごと消える。消す形にすると、戻して再び適用したときに、あとから作られたワークスペースを消したり、非公開のものを1つの公開ワークスペースに混ぜたりしてしまう。順方向も、ワークスペースが1つでもあれば何もしない。
  - テスト: `tests/integration/test_workspace_migration.py`（`TransactionTestCase` 1クラス・4テスト。帰属と逆方向／superuser がいない場合／持ち主がなれる人がいない場合／空の DB）。
  - 開発 DB の写し（`/tmp/w1a-t5/db.sqlite3`、`DJANGO_DB_PATH` で指して実施。共有の `db.sqlite3` には当てていない。試行後に写しは削除）: 0048・0049 の適用に 2.6 秒（プロセス起動を含む）。ワークスペース1つ（公開・持ち主 `admin`＝id 1 の superuser）、編集者1人（`smoketest`＝superuser かつ staff）、リーグ 8/8（ペナントのリーグは 0 で付いていない）、球場 21/21、世界 0/0。ユーザーは3人（admin・s-sato・smoketest）で、staff でない `s-sato` は編集者にならない。`migrate myapp 0048` で3つの表の `workspace` が全部 NULL、`Workspace`・`WorkspaceEditor` が 0 行に戻ることを確かめた。
- T6（`feature/workspace-constraints`）: マイグレーション `0057_workspace_constraints`（0056 の次。当初は 0050、のち 0051・0054。スキーマだけ）。
  - `Stadium.workspace` と `PennantWorld.workspace` を null 不可にした。`League.workspace` は、ペナントのリーグが持たないので null 可のまま。
  - 一意制約: `Stadium.name` の `unique=True` を `unique_workspace_stadium_name`（workspace, name）に、`unique_real_league_name`（`world IS NULL` の条件つきで name）を `unique_workspace_league_name`（workspace, name）に替えた。`unique_world_league_name` は残した。ペナントのリーグは workspace が NULL なので前者に引っかからず、どのワークスペースの実データとも同名でよい（世界の中の一意は後者が保つ）。
  - `CheckConstraint` `league_workspace_xor_world`: workspace と world がちょうど片方（`condition=`。前例 `game_teams_differ`）。
  - 違反メッセージ（`violation_error_message`）は日本語。管理画面のリーグ・球場のフォームは `ModelForm` の検証が UniqueConstraint を見るので、同名の重複は 500 ではなくフォームのエラーになる。world は管理画面のリーグのフォームに出ていないため、「両方持つ」はフォームからは作れない（モデル・DB で弾く）。
  - ドメイン・リポジトリに名前の重複検査は無かった（一意は DB 制約だけ）ので、直すものは無い。ペナントの分岐が世界へ写すリーグ名は `unique_world_league_name` の範囲。
  - マイグレーションは `makemigrations` の自動生成ではなく手書き（null 不可への変更は既定値を尋ねられるため。0049 で帰属済みなので既定値は不要）。`makemigrations --check` は差分なし。
  - テスト: `tests/integration/test_workspace_constraints.py`（名前の一意の範囲・ペナントの同名・帰属の排他と必須・管理画面の重複）。既存テストの修正は不要（T3 のヘルパーが帰属を渡している）。
  - 逆方向（0049 まで戻す）が成功するのは、リーグ名・球場名がワークスペースをまたいで重複していないときだけ（一意制約を名前だけに戻すため）。
  - 0050 は、リーグ・球場・世界に帰属の無い行（NULL）があると、素の NOT NULL・CHECK のエラーで止まる。起きるのは、異例のロールバックと当て直しの後だけ（通常のデプロイは 0048〜0050 を続けて当てる）。
  - 設計 3.3 は「リポジトリが名前の一意違反を日本語のエラーに直す」としているが、実データの範囲で `DjangoLeagueRepository.save` を呼ぶ画面は今無い。この翻訳は最初の呼び出し元（リーグ設定画面）が現れる W3 に回す（W3 の TODO）。
  - ペナントの世界の作成（`PennantWorldService.create_world`）は、分岐元に同名のリーグが2つ以上あると `InvalidWorld`（日本語）で弾く。名前がワークスペースの中でしか一意でなくなり、別のワークスペースの同名リーグを両方選ぶと世界の中の `unique_world_league_name` で 500 になるため。世界を作る前に検査する。
  - 管理画面のリーグのフォームは `world` を除外した（ペナントのリーグは世界側で作り、workspace との排他を崩さない）。
  - 開発 DB の写し（`/tmp/w1a-t6/db.sqlite3`、`DJANGO_DB_PATH` で指して実施。共有の `db.sqlite3` には当てていない。試行後に写しは削除）: 0048〜0050 が通った（数秒）。ワークスペース1つ、リーグ 8（帰属なし 0）、球場 21（帰属なし 0）、世界 0。`check_constraints()` と全リーグ・全球場の `validate_constraints()` が通った。

- T7（`feature/workspace-admin`）: 管理画面。マイグレーションは無い。
  - `Workspace` を登録した（`WorkspaceAdmin`。`WorkspaceEditor` はインライン。トップの並びは先頭）。`WorkspaceForm`: **key は空欄なら `generate_workspace_key(random.SystemRandom())` で作る**（重複していなければ。入力されたら `validate_workspace_key` の形で検査）。**登録済みの行では key は読み取り専用**（URL に入る識別子なので）。公開範囲の選択肢はモデルの choices（ドメインの `Visibility` から写したもの）をそのまま使う。
  - 一覧: League・Stadium は `workspace` の列とフィルタ、Team・PlayerStint・Game はリーグ経由の列（メソッド）とフィルタ（`league__workspace` など）、Player はフィルタだけ（在籍経由 `stints__team__league__workspace`。複数のワークスペースに在籍しうるので列は置かない）。
  - 変更画面の選択肢: チームのリーグと本拠地（`TeamForm`）、在籍のチーム（`PlayerStintForm`・インラインは `PlayerWorkspaceChoicesMixin` が選手の在籍から絞る）、主将在任のチーム、球場の「本拠地とするチーム」（**(d) の W1b 送りをやめ、T7 の計画どおりここで絞った**）、リーグの中のチームの本拠地（`TeamInlineFormSet`）。絞った欄の `invalid_choice` は日本語の文言に替えた。
  - 追加画面の検査（`clean()`）: 範囲をまたぐと「「X」は別のワークスペースに属しています。…」のフォームのエラーになり、何も保存されない。対象は、チームのリーグ×本拠地・リーグの中のチームの本拠地・球場×本拠地チーム・在籍のチーム×選手の保存済みの在籍・同時に送った在籍どうし（`PlayerStintFormSet`）・主将在任のチーム×選手の在籍・試合のホーム×ビジター。選手の在籍がまだ無いときは縛らない。
  - **登録済みのリーグ・球場は `workspace` を読み取り専用にした**（`get_readonly_fields`。リポジトリも帰属を書き換えないのに合わせた。ユーザー不在の間に決めた既定で、戻すのは `get_readonly_fields` の1か所）。読み取り専用で欄がフォームから消えると、ModelForm は（workspace, name）の一意制約の検査を丸ごと飛ばし、同名の重複が 500（IntegrityError）になる。`WorkspaceRequiredForm._get_validation_exclusions` で `workspace` を検査に残して直した（T6 のテストが落ちて気づいた）。
  - `_REAL`・`RealDataOnlyMixin`・`DjangoTeamRepository(_REAL)` の直接生成は今のまま（Q4 の専用関数は W1b）。
  - **`WorkspaceNotFound` の文言を替えた**: 「ワークスペースがありません。運営者が管理画面で最初のワークスペースを作るまで、この操作はできません。」 元の「管理画面で作成してください」は運営者には正しいが、ペナントの世界を作ろうとした一般の利用者（管理画面には入れない）には、自分で作れと言われているように読める。**運営者が作るまで待つしかない**ことを書いた。メッセージを出すのは `legacy_workspace_id()` だけ。
  - テスト: `tests/integration/test_admin_workspaces.py`（登録・読み取り専用・一覧の絞り込み・変更画面の選択肢・範囲をまたぐ組み合わせ・読み取り専用でも同名が 500 にならない）。`AdminDoesNotShowThePennantWorldTest` は変更なしで通る。
  - 絞った選択肢は、オートコンプリート（在籍のチーム・チームの本拠地）の候補までは絞れない（候補は別の URL が返す）。候補から別のワークスペースのものを選んでも、送信時に日本語のエラーで弾く。
  - レビューでの追加（1）: 選手が属すワークスペースは、**保存済みの在籍と主将在任の和**で決める（`_player_workspace_ids`）。在籍の無い選手に過去の主将在任だけを足し、あとから別のワークスペースの在籍を足すと、選手が2つの範囲にまたがったため。
  - レビューでの追加（2）: 在籍と主将在任のフォームセットを互いにつなぎ（`link_stints()`・`link_captaincies()`。FA 宣言と同じ形）、`_ensure_rows_share_workspace` が、送信後の行（削除する行を除く）・**POST から落とされて DB に残る保存済みの行**・先の行と比べる。選択肢を絞る処理が外れても `clean()` が本体として弾く。フォームセット全体の `is_valid()` は互いに呼ばない（循環する）。
  - レビューでの追加（3）: 編集者に持ち主は加えられない（追加画面・持ち主を既存の編集者に変える場合も。0053（当時の番号は 0049）が持ち主を編集者から除いたのに合わせた）。チームの選択肢は `select_related("league")` で引く。
  - 残した軽微な点: `_get_validation_exclusions` は Django の非公開 API（Django 更新時に確かめる）／持ち主・編集者は素のプルダウン（W4 まで）／`WorkspaceForm` はドメインの `Workspace` を組み立てない／2つのワークスペースにまたがる選手は管理画面から作れない（在籍の追加で弾く）。
  - 残した軽微な点（権限）: 閲覧だけの権限のインライン（superuser でない staff に変更権限を与えない場合）では、これらの `clean()` が 500 になりうる。今の管理画面は運営者（superuser）専用なので起きない。W4 で staff の役割を設計するときに見直す。


#### T7 で必ず行うこと（T4 のレビューより。すべて T7 で完了）

- [x] (a) `Workspace` を管理画面に登録する。受け入れ条件: 空の DB で、運営者が管理画面から最初のワークスペースを作り、続けてリーグ・球場を作れる。`WorkspaceNotFound` のメッセージ（「ワークスペースがありません。管理画面で作成してください。」）がこの手順と合っていること。ペナントの画面だけを使う一般の利用者にどう読めるかも見直す。
- [x] (b) 同じコミットで、Wiki `開発環境` の管理ユーザーの作成（createsuperuser）の次と、`本番公開` の createsuperuser の2か所（164 行目付近と 402 行目付近）に「最初にワークスペースを1つ作る」を足す。
- [x] (c) 既存のリーグ・球場で `workspace` を管理画面から読み取り専用にするか決める（`get_readonly_fields`）。リポジトリは帰属を書き換えないが、管理画面は今は書き換えられる。どちらにしても、変更フォームのテストを添える。
- [x] (d) 球場の `home_teams` の選択肢は全ワークスペースのチームを並べる。~~W1b の範囲~~ → T7 の計画に従い T7 で絞った（上の T7 の記録）。

#### epic を main に入れるときに必ず行うこと（T5 のレビューより）

- 0056（バックフィル。当時の番号は 0049）は、データがあるのに superuser も staff もいない DB ではエラーで止まる。ワークスペースの epic をデプロイする前に、superuser がいることを確かめる。止まったときの復旧手順（上の `run --rm --no-deps --entrypoint python web manage.py createsuperuser`）を、epic を main に入れるときに Wiki「本番公開」のデプロイ（更新）の節に足す。

### W1b（#172）

#### T0 基準値（2026-10-15）

`epic/workspaces` 00a4f84（W1a の T1〜T7 と main b093741 相当を取り込んだ先端）で測った。T2（範囲にワークスペースを持たせる）・T3（台帳をワークスペースで絞る）の後に**同じ手順**で測り直し、この表と並べる。マシンは開発用コンテナ（他のエージェントと共有。±10% 程度の揺れは出る）。

**手順**（PowerShell から。`<DB>` は `/tmp/w1b-t0/db.sqlite3`）

1. 開発 DB の写し: コンテナの中で Python の `sqlite3` の `backup()` を使う（コンテナは止めない。共有の `/app/db.sqlite3` は読み取り専用で開く）。`pragma integrity_check` が `ok`、試合 3,480 件を確かめる。
2. 以降は共通の接頭辞を付ける: `docker compose exec -w /app/.claude/worktrees/<測る worktree> -e PYTHONPYCACHEPREFIX=/tmp/pycache -e DJANGO_DB_PATH=<DB> web <コマンド>`（Bash から叩くなら `MSYS_NO_PATHCONV=1`）。
3. `python manage.py migrate`（epic の 0055〜0057 が当たる。2026-10-16 に 0052〜0054、2026-10-24 に 0055〜0057 へ振り直した）。
4. 実データ: `python manage.py measure_pages` を3回流し、画面ごとに中央値。
5. ペナント: `python manage.py pennant_create --name w1b-baseline --league 1 --league 2 --year 2030 --seed 20261015 --owner admin --managed-team 1`（世界 id=1・リーグ2・球団12・選手966人）→ `python manage.py pennant_advance --world 1 --to week`（6日・36試合）。
6. `python /tmp/w1b-t0/measure_pennant.py 1`（下の概要）を流す。

**`measure_pennant.py` の概要**: `django.setup()` のあと `admin`（superuser）で `Client(SERVER_NAME="localhost")` に `force_login` し、世界 1 の URL を1つにつき3回 `get`。`CaptureQueriesContext(connection)` で SQL 時間とクエリ数を取り、応答時間・SQL 時間・クエリ数の中央値を出す（3回は同じプロセスの連続なので2回目以降は暖まっている）。対象は下の表の URL。世界のリーグ・球団は世界 1 の最初の行（リーグ 9・球団 50）。スクリプトはリポジトリに入れていない（使い捨て）。上の概要から書き直せる。

**実データ（`measure_pages` 3回の中央値）**

| 画面 | 応答 ms | SQL ms | クエリ |
| --- | ---: | ---: | ---: |
| ダッシュボード | 518 | 70 | 10 |
| チーム一覧 | 16 | 6 | 2 |
| 順位表 | 102 | 3 | 4 |
| 順位表(2026) | 71 | 3 | 4 |
| 選手一覧(野手) | 278 | 12 | 27 |
| 選手一覧(投手) | 273 | 8 | 27 |
| 選手詳細 | 95 | 3 | 15 |
| 選手検索 | 15 | 1 | 4 |
| リーグ詳細 | 74 | 9 | 5 |
| リーグ成績 | 190 | 7 | 8 |
| リーグタイトル | 70 | 9 | 7 |
| 試合一覧 | 113 | 19 | 6 |
| 試合詳細 | 516 | 35 | 17 |
| 戦力分析 | 42 | 9 | 8 |
| 戦力分析（起用マップ） | 31 | 9 | 8 |
| 戦力分析（FA取得） | 47 | 14 | 13 |

**ペナント（`measure_pennant.py` 3回の中央値）**

| 画面 | URL | 応答 ms | SQL ms | クエリ |
| --- | --- | ---: | ---: | ---: |
| ペナント一覧 | `/pennant/` | 27 | 6 | 14 |
| 世界ダッシュボード | `/pennant/1/` | 158 | 5 | 48 |
| 世界の順位表 | `/pennant/1/standings/` | 12 | 0 | 10 |
| 世界のリーグ詳細 | `/pennant/1/league/9/` | 15 | 1 | 11 |
| 世界のリーグ成績 | `/pennant/1/league/9/stats/?pos=batter` | 136 | 0 | 16 |
| 世界のチーム一覧 | `/pennant/1/teams/` | 10 | 1 | 8 |
| 世界の選手一覧 | `/pennant/1/team/50/?pos=batter` | 135 | 2 | 40 |
| 世界の試合一覧 | `/pennant/1/games/` | 16 | 1 | 12 |
| 世界の戦力分析 | `/pennant/1/team/50/analysis/` | 46 | 1 | 14 |
| 世界の球団ホーム | `/pennant/1/club/` | 61 | 4 | 25 |

- 世界の選手検索は URL が無い（`/players/` は実データ専用の `build_player_search_query()`）。計画の「世界の選手検索」は、近い画面の世界の選手一覧・リーグ成績で代える。
- 実データのダッシュボード（518ms）と試合詳細（516ms）が既に 500ms の線を超えている（T2・T3 の劣化の判定はこの2つの比でも見る）。
- 実データの `measure_pages` は 3 回とも ±3% 以内で揃った。ペナントの値は暖まった状態なので、初回アクセスはこれより遅い。

#### T1

- T1（`refactor/scope-taking-builders`）: 振る舞いは変えない。`WorldScope` の形はまだ変えない。
  - 決定（2026-10-15、ユーザー承認）: `build_world_view_service(world_id)` を `build_service(scope)` に、`build_world_team_analysis_service(world_id)` を `build_team_analysis_service(scope)` に吸収した。ペナントの範囲なら年の基準は `_world_clock(world_id, ...)`（世界の今日）、実データなら `date.today`。
  - 組み立て口は16個、すべて `scope: WorldScope` を既定値なしで受け取る。例外は台帳だけの `build_pennant_world_view()`（T3 で `workspace_id` を取る）。ペナント専用の8つは実データの範囲で、実データ専用の5つ（スコアブックの保存・契約区分・権限判定・選手検索・世界の作成）はペナントの範囲で、`InvalidWorld`（組み立てるときに判定。DB は読まないので `SimpleTestCase` で通る）。権限判定・選手検索も実データ専用に含めた（計画は3つだけ挙げていたが、どちらも今は実データ固定のため同じ扱いにした）。
  - 範囲を作るのは views の `_real_scope()`・`_world_scope(world_id)` だけ。`api.py`・`templatetags/admin_overview.py` はこれを使う。`admin.py`（観客数の警告）は `_REAL`、管理コマンド（`pennant_*`・`estimate_check`・`multi_season_check`）は `WorldScope.real()`／`WorldScope.pennant(options["world"])` を渡す（T2・T5 で替える）。
  - 数え直した値（T1 の前、epic 00a4f84）: 組み立て口18個、`WorldScope` を作る本番27か所・テスト154行・40ファイル、組み立て口の呼び出しは本番64・テスト258行（正規表現の数え方で 262 とずれる）。T1 の後: 本番の `WorldScope.real()`/`pennant()` は17か所、テストは `test_wiring.py`・`tests/domain/test_pennant_world.py`・`helpers.py`（定義の1行ずつ）だけが直接作り、ほかは `real_scope()`・`world_scope(id)` の呼び出し約329か所（組み立て口とリポジトリ・参照クエリ）。
  - テスト: `test_wiring.py` は「すべての組み立て口が `scope` だけを既定値なしで受け取る」（`BuildersRequireScopeTest`）、範囲の取り違えで `InvalidWorld`、吸収した2つの検査（`WorldViewScopeTest`）は `build_service(WorldScope.pennant(7))` の形。実測値は無い（読み取りの経路は変えていない）。
  - CLAUDE.md の組み立て口の一覧は W1b の最後の PR（T6）で直す。

#### T2

- T2（`feature/workspace-scope`）: 範囲がワークスペースを持つ。マイグレーションは無い。
  - domain: `WorldScope(workspace_id, world_id)`。作り方は `real(ws)`・`pennant(ws, w)`（どちらも引数必須）。ワークスペースの id は `__post_init__` で検査し、0・負・bool・int でなければ `InvalidWorkspace`（直接 `WorldScope(...)` を作っても同じ）。`__str__` は「ワークスペース 2 の世界 3」。`World.scope` は `pennant(self.workspace_id, self.id)` で、ワークスペースの無い世界は `InvalidWorld`。
  - `scoping.py`: 実データの条件は `{path}__workspace_id = X`（ペナントのリーグは workspace を持たないので、これだけで世界の行も外れる）。ペナントは `world_id = W` のまま。`players_in` は実データもペナントも肯定形の `Exists(在籍 → チーム → 範囲の条件)` 1本にした（在籍の無い選手はどの範囲にも出ない）。
  - **Q4 の関数は `admin_real_queryset(model)`**（`scoping.py` の末尾）。League・Team・PlayerStint・Game はリーグに workspace があるもの（＝全ワークスペースの実データ）、Player は今までの否定形（ペナントの球団に在籍したことが無い選手。在籍の無い選手を含む）、ほかのモデルは None。使うのは `admin.py` だけ（`_real_queryset`・`_real_teams` の2つの部品経由）。`test_wiring.py` の `AdminOnlyScopeExceptionTest` が `myapp/` の .py（テストを除く）を走査し、名前を出すのが `scoping.py` と `admin.py` だけかを確かめる。
  - `DjangoLeagueRepository.save`: 実データの新しいリーグは `scope.workspace_id` に属す（`legacy_workspace_id()` を外した）。`DjangoWorldRepository.delete` は世界の行から workspace を読んで範囲を作る（存在の確認と同じ1本）。`DjangoWorldRepository.save` の暫定は T3 のまま。
  - presentation: **一時関数は1つの要求で1回だけ読む。** `_workspace_id(request)` が `legacy_workspace_id()` を呼び、結果を要求の属性に覚える（プロセスには覚えない）。`_real_scope(request)`・`_world_scope(request, world_id)`・権限の判定・`api.py`（範囲を1回作って3つの組み立て口に渡す）・`_world_context`・`_next_season_year` はすべてここを通る。ワークスペースが1つも無ければ 404（Q2）。`templatetags/admin_overview.py` は `takes_context` で要求を受け取り `_real_scope(request)` の範囲で概況を出す。ワークスペースが無いときは概況を出さない（運営者が最初の1つを作る管理画面のトップは開ける）。
  - **計画からのずれ（1）: `build_pennant_world_view(workspace_id)` を T2 で受け取る形にした**（計画は T3）。世界ごとの範囲を作るラムダが `_world_scope` を呼ぶと、世界の一覧で世界の数だけ一時関数を読むため。台帳（`DjangoWorldSummaryQuery`）を絞るのは T3 のまま。`test_wiring.py` は台帳の組み立て口が `workspace_id` を既定値なしで受け取ることを確かめる。`build_pennant_world_service` は受け取った実データの範囲のワークスペースを `PennantWorldService(workspace_id=...)` に渡し、作る世界はそのワークスペースに属す（レビュー M1。一時関数のワークスペースではない）。日程の生成（`ensure_schedule`）も同じワークスペースで世界の範囲を作るので、`World.scope` と食い違わない。`DjangoWorldRepository.save` の暫定（空なら一時関数で補う）はやめ、ワークスペースの無い世界は `InvalidWorld` で拒む（レビュー2周目）。
  - admin.py: `_REAL`・`_REAL_QUERYSETS` をやめた。一覧・選択肢・フィルタは `admin_real_queryset`、選択肢をワークスペースで絞る箇所（T7）は `teams_in(WorldScope.real(ws))`・`leagues_in(WorldScope.real(ws))`。リポジトリを直接作る3組（支配下の上限・外国人枠・選手の外国人の変更）と観客数の警告は `_row_scope(league)`（行のリーグのワークスペースの実データ）。
  - 管理コマンド: 範囲は `myapp/management/workspaces.py` の `legacy_real_scope()`・`legacy_world_scope(world)`（一時関数のワークスペース。無ければ日本語の `CommandError`）で作る（T5 で `--workspace` と世界からの導出に替える）。`seed_virtual_players`・`seed_virtual_games` は1回の実行で1回だけ決めて覚える（`Command._real_scope()`）。`estimate_check.py`・`multi_season_check.py` も同じ暫定。スコアブックの保存 API は、ワークスペースが無ければほかのエラーと同じ JSON（`{"ok": false, ...}`・404）を返す。
  - **計画からのずれ（2）: T6 の「同名のリーグを2つ分岐元に選べない」テスト**は、他所のリーグが分岐元の範囲に入らず `LeagueNotFound` になるので、「他所のリーグ（同名でも）を分岐元にできない」テスト（`test_world_fork.py`）に替えた。検査そのものは残した（安い保険）。
  - テスト: `test_workspace_isolation.py`（新）。`WorkspaceCase` は A＝`BaseCase` の既定のワークスペース（最初に作るので id が最小）と、目印の名前の B（リーグ・球場・チーム2つ・選手・前後の年の試合2つ。能力・日程・編成・戦力外・守備の明細の行も ORM で直接置く）。`repositories`・`queries` の `Django*` 21クラスを列挙し、検査の表（`CHECKS`）に無いクラスがあれば落とす（台帳2つは T3）。どの検査も、実データの条件を `world__isnull` に戻すと落ちることを確かめた（40件）。ほかに `find_by_id` の相互の NotFound・`players_in`（B の選手も A の世界の選手も出ない・在籍の無い選手はどこにも出ない）・今の URL の画面に B の目印が出ない／B の id は 404・一時関数を1つの要求で1回だけ読む（試合一覧・チームの選手一覧で `myapp_workspace` を読むクエリが1本）・ワークスペースが無いとサイトは 404 で管理画面のトップは開ける。`test_admin.py` に Q4（A・B・在籍の無い選手が一覧に出てペナントの行は出ない・B の試合も一覧に出る・観客数の警告は B の球場の収容人数で出す）。
  - テストの補助: `real_scope(workspace=None)` は既定のワークスペース、`world_scope(world_id)` は世界の行の workspace（無い世界の id なら既定のワークスペース）。`register_players` はチームのリーグのワークスペースで読み書きする。モジュール定数 `REAL = real_scope()` を置いていた8ファイルは、import の時点で DB を読むので呼び出しに替えた。ワークスペースを作らずに今の URL を開いていた `test_navigation.py`・`test_templates.py` は既定のワークスペースを作る。戦力分析の画面のクエリ数の上限（`test_team_analysis.py`）は 6→7（一時関数の1本）。
  - **#39 S1（`epic/postseason`）と合わせるとき**: S1 は `games_in(scope, *, stages)` などに区分の条件（`stage_condition`・`game_rows_condition`）を足す。区分とワークスペースは独立した条件なので、`world_condition(path, scope) & stage_condition(...)` のように**別々の `Q` を `&` で重ねる**（`world_condition` の中に区分を混ぜない）。T2 が書き換えたのは `world_condition` の実データの分岐・`players_in` の本体・末尾の `admin_real_queryset` だけ。`admin_real_queryset(Game)` で試合を読む管理画面は、S1 の取り込みで `StageFilter.ALL_STAGES` 相当（区分で絞らない）にする。取り込んだ後は `test_workspace_isolation.py` と `test_postseason_isolation.py` の両方が通ることで確かめる。

**計測（T0 と同じ手順。2026-10-15）**: 開発 DB を `backup()` で `/tmp/w1b-t2/db.sqlite3` に写し（`integrity_check` ok・試合 3,480）、0049〜0051 を当て、`measure_pages` を流した後、`pennant_create`（世界 1・リーグ2・球団12・選手966人）→ `pennant_advance --to week`（6日・36試合）→ `measure_pennant.py 1`。共有コンテナの負荷が T0 の日より高かった（同じ基点のコードでも T0 より 10〜20% 遅い）ので、**同じ写しで基点 ca1f371（使い捨ての worktree）と T2 を交互に3回ずつ**流し、それぞれの中央値を並べた。写しと使い捨ての worktree は計測後に消した。

| 画面 | T0（応答/SQL/クエリ） | 基点・同時刻 | T2 |
| --- | ---: | ---: | ---: |
| ダッシュボード | 518 / 70 / 10 | 559 / 77 / 10 | 536 / 71 / 11 |
| チーム一覧 | 16 / 6 / 2 | 16 / 6 / 2 | 16 / 6 / 3 |
| 順位表 | 102 / 3 / 4 | 115 / 4 / 4 | 113 / 3 / 5 |
| 順位表(2026) | 71 / 3 / 4 | 78 / 3 / 4 | 70 / 3 / 5 |
| 選手一覧(野手) | 278 / 12 / 27 | 346 / 14 / 27 | 298 / 14 / 28 |
| 選手一覧(投手) | 273 / 8 / 27 | 320 / 12 / 27 | 290 / 10 / 28 |
| 選手詳細 | 95 / 3 / 15 | 117 / 5 / 15 | 98 / 3 / 16 |
| 選手検索 | 15 / 1 / 4 | 19 / 2 / 4 | 16 / 1 / 5 |
| リーグ詳細 | 74 / 9 / 5 | 86 / 12 / 5 | 77 / 9 / 6 |
| リーグ成績 | 190 / 7 / 8 | 237 / 11 / 8 | 206 / 8 / 9 |
| リーグタイトル | 70 / 9 / 7 | 85 / 11 / 7 | 86 / 11 / 8 |
| 試合一覧 | 113 / 19 / 6 | 128 / 24 / 6 | 124 / 20 / 7 |
| 試合詳細 | 516 / 35 / 17 | 609 / 42 / 17 | 594 / 39 / 18 |
| 戦力分析 | 42 / 9 / 8 | 50 / 12 / 8 | 49 / 9 / 9 |
| 戦力分析（起用マップ） | 31 / 9 / 8 | 39 / 9 / 8 | 37 / 9 / 9 |
| 戦力分析（FA取得） | 47 / 14 / 13 | 55 / 14 / 13 | 54 / 14 / 14 |
| ペナント一覧 `/pennant/` | 27 / 6 / 14 | 30 / 6 / 14 | 31 / 6 / 15 |
| 世界ダッシュボード | 158 / 5 / 48 | 182 / 5 / 48 | 182 / 5 / 49 |
| 世界の順位表 | 12 / 0 / 10 | 15 / 0 / 10 | 14 / 0 / 11 |
| 世界のリーグ詳細 | 15 / 1 / 11 | 17 / 1 / 11 | 18 / 1 / 12 |
| 世界のリーグ成績 | 136 / 0 / 16 | 155 / 1 / 16 | 164 / 0 / 17 |
| 世界のチーム一覧 | 10 / 1 / 8 | 12 / 1 / 8 | 12 / 2 / 9 |
| 世界の選手一覧 | 135 / 2 / 40 | 149 / 2 / 40 | 155 / 3 / 41 |
| 世界の試合一覧 | 16 / 1 / 12 | 20 / 1 / 12 | 19 / 1 / 13 |
| 世界の戦力分析 | 46 / 1 / 14 | 43 / 1 / 14 | 49 / 2 / 15 |
| 世界の球団ホーム | 61 / 4 / 25 | 68 / 5 / 25 | 73 / 5 / 26 |

- クエリ数はすべての画面でちょうど +1（一時関数の1本）。基点・同時刻のクエリ数は T0 と同じで、写しと手順が同じであることの確かめにもなる。
- 応答時間は、同時刻の基点と比べて誤差の範囲（実データの画面は ±5% 以内か T2 が速い。ペナントの画面は −1〜+9ms）。肯定形の `Exists` に替えた選手検索も 19→16ms で劣化は無い。完了条件（クエリ数は +1 まで・応答時間は誤差の範囲）を満たす。

#### T3

- T3（`feature/workspace-world-ledger`）: 世界の台帳をワークスペースで絞る（Q3）。マイグレーションは無い。
  - infrastructure: `scoping.py` に `worlds_in(workspace_id)`（そのワークスペースの `PennantWorld`）。`DjangoWorldRepository(workspace_id)`・`DjangoWorldSummaryQuery(workspace_id)`（既定値なし）は、読み・削除をすべて `worlds_in` から引く。ほかのワークスペースの世界の id は `find_by_id`・`get`・`delete` で `WorldNotFound`、`find_all`・`list_all` に出ない。`world_condition_in`・`*_in_worlds` に渡すのは `worlds_in` で絞った行の id だけ（docstring に明記）。
  - **保存の乗っ取りの防ぎ方**: `update_or_create(id=...)` をやめた。`world.workspace_id` が空なら `InvalidWorld`（T2 のまま）、台帳と違うワークスペースなら `InvalidWorld`（「ほかのワークスペースの世界は保存できません。」）。id の無い世界は台帳のワークスペースに `create`。id のある世界は `worlds_in(ws).filter(id=...).update(...)` の1クエリで更新し、0行なら `WorldNotFound`（ほかのワークスペースの世界の id でも、台帳に無い id でも、何も書かず新しい行も作らない）。`workspace_id` は更新の対象に入れない（ワークスペースは作るときに決まり、動かない）。
  - `count_by_owner` は全ワークスペースで数える（世界の数の上限は1人あたり。読み替えは W6）。docstring とドメインのプロトコルに書いた。
  - presentation: 世界の台帳を作る組み立て口（`build_pennant_world_view`・`build_pennant_world_service`・`build_pennant_season_service`・`build_pennant_offseason_service`・`build_pennant_ratings_service`・`build_club_service`・`build_offseason_view_service`）はすべて範囲のワークスペースを渡す。`_world_clock(scope, …)` は範囲を受け取り、その範囲のワークスペースの台帳で世界の開幕年を引く。
  - **`_world_scope(request, world_id)` の照合**: 今の URL のワークスペースの台帳で世界を引けたときだけ `pennant(ws, w)` を作り、引けなければ 404。世界の画面は必ず先に `_world_context`（見出し）を作り、見出しは絞った `DjangoWorldSummaryQuery.get` で読むので、`_world_context` が引けた世界の id を要求に覚え（`_verified_world_ids`）、`_world_scope` はそれを見るだけにした。**世界の画面のクエリ数は変わらない**（下の計測）。見出しを読んでいない要求だけ、`_world_scope` が台帳を1回読む（今の画面には無い。テストで確かめた）。
  - 管理コマンド: `legacy_world_scope(world_id)` は世界の行からワークスペースを導く（`infrastructure/workspaces.py` の `workspace_id_of_world`。無い世界は日本語の `CommandError`）。`pennant_delete` は新しい `legacy_world_real_scope(world_id)`（世界のワークスペースの実データの範囲）で組み立てる。`pennant_create`・`estimate_check`・`multi_season_check` は一時関数のワークスペースで作り、同じワークスペースの台帳で消すので変えていない。
  - **計画からのずれ（1）: `workspace_id_of_world` を T3 で足した**（計画は T5）。`--world` から台帳のワークスペースを導くのに T3 の時点で要るため。T5 は `workspace_id_for_key` と `--workspace` だけを足す。台帳を通さずに世界のワークスペースを決める関数なので、`test_wiring.py` の `WorldWorkspaceResolverTest` が、名前を出すのが `infrastructure/workspaces.py`・`management/workspaces.py` だけかを確かめる（画面が使うと、今の URL のワークスペースに無い世界を開けてしまう）。
  - テスト:
    - `test_wiring.py`: `UNSCOPED` を空にし、空であることを検査する。`ScopeIsRequiredTest` は「台帳（`LEDGERS`）は `workspace_id`、ほかはすべて `scope` だけを既定値なしで受け取る」。`ProtocolConformanceTest` の台帳は引数つき。`LedgersAreScopedToTheWorkspaceTest`: 範囲のワークスペースを 3 にして全組み立て口を作り、持っている台帳（7つの組み立て口）と世界の「今日」の台帳（`build_service`・`build_team_analysis_service`・`build_pennant_ratings_service`・`build_club_details_service`）がそのワークスペースのものか。
    - `test_workspace_isolation.py`: `WorkspaceCase` に A の世界（`A_WORLD`）と B の世界（目印の名前。オーナーは `b_owner`）を足し、`CHECKS` に台帳2つ（A の台帳に A の世界だけ・B の世界の id は `WorldNotFound`）を載せた（`LEDGERS` の除外は消した）。`WorldLedgerWritesTest`: A の台帳で B の世界を保存（そのまま→`InvalidWorld`・ワークスペースを A に書き換えた乗っ取り→`WorldNotFound`）・削除（`WorldNotFound`）しても、世界・B の世界のリーグと球団・戦力外の行が変わらない。A の台帳で自分の世界は更新できる（空振りの防止）。**T2 で外した戦力外の書き込み**: `pennant(A, A の世界)` の範囲で B の世界の球団の戦力外を指名すると `TeamNotFound` で何も変わらない。`WorldPagesOfTheOtherWorkspaceTest`: 今の URL で B の世界の17画面（見出し・歴代・オフ・削除・編成・順位表・リーグ・チーム・試合・戦力分析・選手）を B のオーナーで開いても 404、進める・締める・消すの POST も 404 で世界と試合が変わらない、`/pennant/` に A の世界だけが出る。`WorldScopeIsVerifiedAgainstTheLedgerTest`: B の世界の `_world_scope`・`_world_context` は 404、見出しの後の `_world_scope` は0クエリ、見出しの無い `_world_scope` は2回呼んでも台帳を1回だけ読む。どれも `worlds_in` を `PennantWorld.objects.all()` に戻すと落ちることを確かめた（30件）。
    - `test_world_fork.py`: id が最小でないワークスペースの範囲で作った世界は、そのワークスペースに属し、その台帳で引けて既定のワークスペースの台帳では `WorldNotFound`。`pennant_delete --world` は、ほかのワークスペースの世界も行から導いて消す。`test_repositories.py`: 台帳と違うワークスペースの世界・台帳に無い id は保存しない（新しい行を作らない）、台帳は自分のワークスペースの世界だけを読み・消す、`count_by_owner` は全ワークスペースで数える。
    - テストの補助: `world_ledger(workspace=None)`・`world_summary_query(workspace=None)`（省くと既定のワークスペース）。

**計測（T0 と同じ手順。2026-10-15）**: 開発 DB を `backup()` で `/tmp/w1b-t3/db.sqlite3` に写した（`integrity_check` ok・試合 3,480）。**共有の開発 DB には main の `0049_league_foreign_active_limits`・`0050_remove_league_foreign_player_limits`（#204）が当たっていて、epic の 0049〜0051 と番号がぶつかる**（そのまま epic の `migrate` を流すと、リーグの外国人枠の列に列名の文字列が入った壊れた写しになり、`pennant_create` が `TypeError` で落ちる）。写しの上だけで、main の作業ツリーのコードで `migrate myapp 0048_pennant_release_plan`（#204 の2本を戻す。外国人枠は逆方向の既定値の 登録5・出場3 になる）→ T3 の worktree で `migrate`（0049〜0051）としてから、`pennant_create`（世界 1・リーグ2・球団12・選手966人）→ `pennant_advance --to week`（6日・36試合）。**同じ写しで基点 49b305b（T2 の後。使い捨ての worktree）と T3 を交互に3回ずつ**流し、それぞれの中央値を並べた。写しと使い捨ての worktree は計測後に消した。T5 以降も同じ写しを作るときは、この戻しの手順が要る（epic のマイグレーションを main の後ろへ振り直すまで。2026-10-16 の main の取り込みで 0052〜0054 に振り直したので、以後は戻さずに `migrate` するだけでよい）。

| 画面 | T0（応答/SQL/クエリ） | T2 の後（基点 49b305b）・同時刻 | T3 |
| --- | ---: | ---: | ---: |
| ダッシュボード | 518 / 70 / 10 | 514 / 68 / 11 | 499 / 68 / 11 |
| チーム一覧 | 16 / 6 / 2 | 16 / 6 / 3 | 15 / 6 / 3 |
| 順位表 | 102 / 3 / 4 | 96 / 3 / 5 | 96 / 3 / 5 |
| 順位表(2026) | 71 / 3 / 4 | 70 / 3 / 5 | 69 / 3 / 5 |
| 選手一覧(野手) | 278 / 12 / 27 | 289 / 11 / 28 | 286 / 12 / 28 |
| 選手一覧(投手) | 273 / 8 / 27 | 270 / 8 / 28 | 268 / 8 / 28 |
| 選手詳細 | 95 / 3 / 15 | 91 / 2 / 16 | 90 / 3 / 16 |
| 選手検索 | 15 / 1 / 4 | 14 / 1 / 5 | 15 / 1 / 5 |
| リーグ詳細 | 74 / 9 / 5 | 72 / 9 / 6 | 73 / 9 / 6 |
| リーグ成績 | 190 / 7 / 8 | 178 / 6 / 9 | 184 / 7 / 9 |
| リーグタイトル | 70 / 9 / 7 | 68 / 9 / 8 | 70 / 9 / 8 |
| 試合一覧 | 113 / 19 / 6 | 109 / 19 / 7 | 112 / 19 / 7 |
| 試合詳細 | 516 / 35 / 17 | 506 / 35 / 18 | 500 / 35 / 18 |
| 戦力分析 | 42 / 9 / 8 | 42 / 9 / 9 | 42 / 9 / 9 |
| 戦力分析（起用マップ） | 31 / 9 / 8 | 31 / 9 / 9 | 31 / 9 / 9 |
| 戦力分析（FA取得） | 47 / 14 / 13 | 47 / 14 / 14 | 46 / 13 / 14 |
| ペナント一覧 `/pennant/` | 27 / 6 / 14 | 26 / 6 / 15 | 27 / 6 / 15 |
| 世界ダッシュボード | 158 / 5 / 48 | 155 / 3 / 49 | 150 / 4 / 49 |
| 世界の順位表 | 12 / 0 / 10 | 12 / 0 / 11 | 11 / 0 / 11 |
| 世界のリーグ詳細 | 15 / 1 / 11 | 14 / 1 / 12 | 15 / 1 / 12 |
| 世界のリーグ成績 | 136 / 0 / 16 | 128 / 0 / 17 | 130 / 0 / 17 |
| 世界のチーム一覧 | 10 / 1 / 8 | 9 / 1 / 9 | 9 / 1 / 9 |
| 世界の選手一覧 | 135 / 2 / 40 | 124 / 2 / 41 | 128 / 2 / 41 |
| 世界の試合一覧 | 16 / 1 / 12 | 16 / 1 / 13 | 15 / 1 / 13 |
| 世界の戦力分析 | 46 / 1 / 14 | 36 / 1 / 15 | 38 / 1 / 15 |
| 世界の球団ホーム | 61 / 4 / 25 | 57 / 4 / 26 | 58 / 4 / 26 |

- **クエリ数は T2 の後とすべての画面で同じ**（台帳の条件は既存のクエリの `WHERE` に1つ足されるだけで、`_world_scope` の照合は見出しの読み取りを使い回す）。T0 との差は T2 の一時関数の1本だけで、完了条件（+1 まで）を満たす。
- 応答時間は同時刻の基点と誤差の範囲（実データ −15〜+6ms、ペナント −5〜+4ms）。この日の共有コンテナの負荷は T2 の計測の日より低く、基点の値は T0 とほぼ同じだった。

**レビューの後に直したこと・後に回すこと（T3）**

- 直した（M1）: 世界の一覧の作成フォームの上限（`PennantWorldList.at_limit`）は、このワークスペースの自分の世界の数（`mine`）ではなく、作成の検査と同じ `count_by_owner`（全ワークスペース）で決める。ほかのワークスペースに上限まで世界を持つ人に、送れば拒まれるフォームを見せないため（`PennantWorldViewService` が台帳を受け取る。ログインしている人の `/pennant/` だけクエリが1本増える）。上限の文言は `domain/pennant/world.py` の `WORLD_LIMIT_MESSAGE` を出典にし、作成の拒否と画面の理由の両方で使う（「ほかのワークスペースの世界も数えます」を足した）。W6 でワークスペースごとの上限に読み替えるときは、ここと `count_by_owner` を一緒に直す。
- 直した: `DjangoWorldSummaryQuery._read` はインスタンスのメソッドにし、世界の行を必ず自分で `worlds_in(このワークスペース)` から引く（呼ぶ側に QuerySet を渡させない。`*_in_worlds` に渡す id の出どころを形で守る）。
- 後に回す（a）多重の防御: ペナントの条件（`world_condition` の `world_id = W`）に `world__workspace_id = X` を足すと、範囲を作る側の照合が漏れても SQL で止まる。代わりに League → PennantWorld の結合が1つ増える。入れるかは測ってから決める（W2 で URL がワークスペースを運ぶようになった後が候補）。
- 後に回す（b）W2: 今の URL のワークスペースは1つの要求で1つに決まるので、`_verified_world_ids` は世界の id だけで覚えている。W2 で URL の key からワークスペースを解決するようになったら、`(workspace_id, world_id)` で覚える形にする（1つの要求で2つのワークスペースを扱う経路ができても取り違えないように）。

#### main の取り込み（2026-10-16）

`refactor/merge-main-into-workspaces`（#172）で、epic bd67b6f に main 541ff89 を取り込んだ。

- **main が持ち込んだもの**: #219 段階0〜3（`TeamApplicationService` から `RosterService`（ロスターの更新）・`GameRecordingService`（試合の登録・編集画面の材料）・`GameViewService`（試合の一覧と詳細）へ移し、組み立て口 `build_game_view_service()`・`build_world_game_view_service(world_id)` を足した）、#204 段階A（外国人枠を1軍登録の3列 `foreign_active_limit`・`foreign_active_pitcher_limit`・`foreign_active_batter_limit` にし、`ForeignPlayerRules` に。マイグレーション `0049_league_foreign_active_limits`・`0050_remove_league_foreign_player_limits`。管理画面の在籍・選手のフォームから保有の枠の検査を外した）、#248（ペナントの表彰 `PennantAward`・`DjangoAwardRepository`・`DjangoAwardQuery`・`awards_in`・管理コマンド `pennant_record_awards`。マイグレーション `0051_pennant_award`）、CI と見回りのスクリプトの変更。
- **振り直し**: epic のマイグレーションを main の後ろへ移した。`0049_workspace` → `0052_workspace`（依存を `0051_pennant_award` に）、`0050_backfill_default_workspace` → `0053_backfill_default_workspace`、`0051_workspace_constraints` → `0054_workspace_constraints`。依存・docstring・コメント（`admin.py` の編集者の行・`test_jersey_number.py`・`test_workspace_constraints.py`）・`test_workspace_migration.py` の BEFORE/AFTER・この設計書の T2/T5/T6 の見出しと T0 の手順を直した。空の DB からの `migrate` と `makemigrations --check` を確かめた。T3 の計測の記録にある「#204 の2本を戻してから epic を当てる」手順は、以後は要らない。
- **main の新しいコードをワークスペースの規則に合わせたところ**:
  - 組み立て口: `build_game_view_service` は `scope` を既定値なしで受け取る形にし、`build_world_game_view_service(world_id)` を吸収した（T1 で `build_world_view_service` を `build_service(scope)` に吸収したのと同じ形）。分類は `BOTH_BUILDERS`。試合の画面の `_game_scope(request, world_id)` は `_real_scope(request)`・`_world_context`・`_world_scope` で範囲を作る。`_next_season_year` も `build_game_view_service(_real_scope(request))`。
  - main が `build_recording_service()`・`build_roster_service()` を新たに呼ぶようにした箇所（試合の登録・編集画面・選手の登録・選手の編集の退団／主将／更新・スコアブックの保存 API）は、すべて `_real_scope(request)` を渡す。API は範囲を1回作って `build_recording_service(scope)`（試合のチームを引く・保存）と `build_permission_query(scope)` に渡す。
  - `build_pennant_offseason_service` に main が足した `titles=build_world_view_service(world_id)` は `build_service(scope)`。選手ページの表彰（`_player_awards`）は `build_pennant_history_service(_world_scope(request, world_id))`。
  - `pennant_record_awards` は他の `pennant_*` と同じく `legacy_world_scope(--world)` で範囲を作る。
  - `DjangoAwardRepository`・`DjangoAwardQuery` は main の時点で `scope` 必須・`awards_in`（`world_condition("league", scope)`）経由だったので、実装は変えていない。`test_workspace_isolation.py` の `CHECKS` に2つを足し、B の世界に表彰の行を1つ置いて、A の実データ・A の世界の範囲から読めない・A の世界の範囲で B の世界のリーグの表彰を書けない（`InvalidAward`）ことを確かめる（`awards_in` を全件に戻すと落ちることを確かめた）。
  - `DjangoGameRepository._ensure_years_not_closed`（締めた年の試合の書き込みを拒む。#248）は世界の開幕年を `PennantWorld.objects` から直接読んでいたので、範囲のワークスペースの台帳（`worlds_in(scope.workspace_id)`）から読むようにした。引けないとき（取り違えた範囲 `pennant(A, B の世界)`）は検査を飛ばさず `WorldNotFound` で拒む（`test_workspace_isolation.py` の `MisbuiltWorldScopeGameWritesTest`。飛ばす形に戻すと落ちることを確かめた）。
  - 分類表（`test_world_isolation.py`）に `PennantAward` を「League までの道」の形（`"league"`）で足した。世界の削除の順（`_WORLD_ROWS_CHILD_FIRST`）には main が `"league"` の道で入れていて、epic の形と同じなので変えていない。
  - リーグの保存（`DjangoLeagueRepository.save`）は epic の帰属の分岐（作るときだけ workspace／world を決める）を残し、外国人枠は main の `foreign_rule_values(league.foreign_rules)` で書く。管理画面のリーグのフォーム（T7）は workspace の扱いが変わらず、新しい3列は main のフィールドセットのまま出る。テストの管理画面の POST（`test_admin.py`・`test_workspace_constraints.py`）を新しい3列に替えた。`make_league` は変えていない（新しい列はモデルの既定値＝協約の値）。0054 の制約は外国人枠の列に関わらない。
  - テスト: main が足した・書き換えたテストの `WorldScope.real()`／`WorldScope.pennant(id)`・`build_*()`・`build_world_view_service(id)`・リーグの直接作成を、`real_scope()`・`world_scope(id)`・`build_service(world_scope(id))`・`make_league` に替えた（`test_pennant_awards.py`・`test_club_plan.py`・`test_club_screen.py`・`test_world_roster_check.py`・`test_repositories.py` など）。`BaseCase` は範囲を1回作って `service`・`roster`・`game_view` に渡す。`helpers.py` の `build_world_game_view_service` の再輸出は消した。

#### main の取り込み（2026-10-24）

- main から来たもの: #97 段階3（ペナントの疲労の表示）・#108（入団年をドラフトの年・順位・育成に置き換え。マイグレーション 0052〜0054）。
- 振り直し: #108 が 0052〜0054 を使ったので、epic のマイグレーションを `0055_workspace`（依存は `0054_remove_player_debut_year`）・`0056_backfill_default_workspace`・`0057_workspace_constraints` に移した。依存・`test_workspace_migration.py`・コメントをそろえた。
- 衝突は2つ（`admin.py` の import と `test_pennant_fatigue.py` の import）。main の新しいテストのうち、古い形の範囲（`WorldScope.real()`）を使っていた `test_seed_virtual_players.py` の1か所を `real_scope()` に直した。#108 で新しい組み立て口・リポジトリ・モデルは増えていない（`Player` の列だけ）。
- #108 のマイグレーションのテスト（`test_migration_draft_year.py`）は、後始末で最新まで当て直すときに epic の 0056 が持ち主を求めるので、`test_jersey_number.py` と同じく古い状態に superuser を足した。マイグレーションのテストでリーグを作るときは、これからも同じ手当てが要る。

#### T4

- T4（`feature/workspace-stadiums`）: 球場を範囲で引き、本拠地を検査する。マイグレーションは無い。基点は epic の main 取り込み（2026-10-24、090921b）。
  - `scoping.py`: 球場の範囲の条件は `_stadium_condition(path, scope)` の1か所（実データでもペナントでも `workspace_id = scope.workspace_id`）。これを使うのが `stadiums_in(scope)`（範囲の球場の QuerySet）と `home_stadium_in(scope)`（球団の本拠地を範囲の球場に限って結合する `FilteredRelation`。範囲の外の球場を指す球団は本拠地なしとして読む）。`admin_real_queryset(Stadium)` は今までどおり None（管理画面は全ワークスペースの球場を扱う）。
  - domain: `StadiumNotFound`（`DomainError`。メッセージは「球場が見つかりません（id=…）。」）。
  - **球場を読む経路（`myapp/` を `Stadium`・`home_stadium`・`capacity`・`city` で洗った）**: チーム一覧（`DjangoTeamListQuery`。球場名と所在地）・観客数の警告（`DjangoHomeStadiumQuery`）・試合のシミュレーションの収容人数（`DjangoSimulationContextQuery.teams`。ペナントの進行）・`seed_virtual_games` の収容人数（`Team.objects` の直読みだったのを `teams_in` ＋ `home_stadium_in` に）。どれも `select_related("home_stadium")`／`home_stadium__…` を `home_stadium_in(scope)` の結合に替えた（結合の数は同じ）。チームの詳細・戦力分析は球場を読んでいない（集約の `Team.home_stadium_id` は id だけ）。
  - **書く経路**: `DjangoTeamRepository.save` だけ（分岐で写す本拠地もここを通る。写し先はペナントの範囲で、ワークスペースは分岐元と同じなので通る）。**新しいチームと、本拠地を変えたときだけ** `stadiums_in(scope)` で引き、無ければ `StadiumNotFound` で何も書かない（リーグ・チームの検査の後、`update_or_create` の前）。チームの存在の確認を `.exists()` から `.values("home_stadium_id").first()` に替え、今の本拠地も同じ1本で読むので、本拠地を変えない保存（シーズンを締める処理など）のクエリ数は変わらない。本拠地を外す（None）保存も引かない。範囲の外の球場を指す行（ORM の直書き）は、集約も参照クエリと同じく本拠地なしとして読み（`_team_rows` が `home_stadium_in` で `scoped_home_stadium_id` を読む）、その集約を保存すると外の球場の id は消える（レビュー M1。分岐はその球団を本拠地なしで写し、「球場が見つかりません」で世界を作れなくはならない）。**ペナントの範囲では `stadiums_in` が、範囲の世界が球場のワークスペースに属すことも同じ1クエリの結合で見る**（レビュー M2。`pennant(A, B の世界)` のように取り違えた範囲で、B の世界の球団に A の球場を書けない）。
  - 管理画面: T7 で絞った本拠地の選択肢（チームの変更画面・リーグの中のチーム）の `Stadium.objects.filter(workspace_id=…)` を `stadiums_in(WorldScope.real(ws))` にした。球場の「本拠地とするチーム」（T7 (d)）は T7 で済んでいたので変えていない。`admin.py` に W1b の TODO の文言は残っていなかった。管理画面の `save_related`（球場の側から `Team.home_stadium` を `update` で書く）はリポジトリを通らないが、`clean()` が同じワークスペースかを検査する（T7）。
  - テスト:
    - `test_workspace_isolation.py`: `WorkspaceCase` が世界を作った後に、A の相手チームと A の世界のその写しの本拠地を ORM で B の球場に向ける（`_plant_stray_stadiums`）。`CHECKS` の `DjangoTeamListQuery`・`DjangoHomeStadiumQuery`・`DjangoSimulationContextQuery` はその球団の本拠地が空（名前・所在地・収容人数が出ない）で、A の球場は読めることを確かめる。今の URL のチーム一覧にも B の球場名が出ない。`StadiumsInTest`: 4つの範囲（A・A の世界・B・B の世界）の `stadiums_in` がそれぞれのワークスペースの球場だけ・A の世界の範囲の読み取り・B の世界の分岐が B の球場を写せた。`HomeStadiumWritesTest`: 実データと A の世界の範囲で、既存の球団（選手を1人足した集約）と新しい球団の本拠地を B の球場にすると `StadiumNotFound` で、チーム・選手・在籍の行が変わらない／同じワークスペースの球場なら実データでも世界でも付けられる。新しい `Django*` は無いので `CHECKS` の行は増えていない。
    - `test_repositories.py`（`HomeStadiumCheckQueriesTest`）: 本拠地を変えた保存は変えない保存よりちょうど1本多く、外す保存は同じ。新しいチームに同じワークスペースの球場を付けられる。
    - `test_admin_workspaces.py`: チームの変更画面で B の球場を送ると選択肢のエラーで本拠地が変わらない（ほかの組み合わせ（追加画面・リーグの中のチーム・球場の本拠地チーム）は T7 のテストのまま）。
    - `test_world_isolation.py` の `test_the_stadium_is_shared`・`test_editing_a_stadium_does_not_detach_the_world_teams` は「同じワークスペースの中で共有」と読めるので、docstring だけ直した。
    - **変異の確かめ**: `_stadium_condition` を `id IS NOT NULL`（全球場）に替えると、上の検査と管理画面の選択肢の検査が19件落ちることを確かめ、戻した。
    - レビューで足したテスト: 集約も範囲の外の球場を本拠地なしとして読み保存で消える（実データ・A の世界）、その球団のリーグから分岐した世界の写しは本拠地なし（`test_workspace_isolation.py`）、`MisbuiltWorldScopeStadiumWritesTest`（`pennant(A, B の世界)` で B の世界の既存・新しい球団に A の球場を書くと `StadiumNotFound` で何も変わらない）、`seed_virtual_games` は別のワークスペースの球場を指す球団のホーム試合に観客数を作らない（`test_seed_virtual_games_game_day.py`）。

**計測（T0 と同じ手順。2026-10-24）**: 開発 DB を `backup()` で `/tmp/w1b-t4/db.sqlite3` に写し（`integrity_check` ok・試合 3,480）、`migrate`（0055〜0057）→ `pennant_create`（世界 1・リーグ2・球団12・選手966人）→ `pennant_advance --to week`（6日・36試合）。**同じ写しで基点 090921b（使い捨ての worktree）と T4 を交互に3回ずつ** `measure_pages` と `measure_pennant.py 1` を流し、中央値を並べた（応答 ms / SQL ms / クエリ）。写しと使い捨ての worktree は計測後に消した。

| 画面 | 基点 090921b・同時刻 | T4 |
| --- | ---: | ---: |
| ダッシュボード | 486 / 64 / 11 | 502 / 65 / 11 |
| チーム一覧 | 14 / 6 / 3 | 11 / 3 / 3 |
| 順位表 | 90 / 3 / 5 | 94 / 3 / 5 |
| 順位表(2026) | 66 / 3 / 5 | 67 / 3 / 5 |
| 選手一覧(野手) | 260 / 9 / 28 | 259 / 9 / 28 |
| 選手一覧(投手) | 256 / 7 / 28 | 265 / 7 / 28 |
| 選手詳細 | 84 / 2 / 16 | 91 / 2 / 16 |
| 選手検索 | 13 / 1 / 5 | 14 / 1 / 5 |
| リーグ詳細 | 68 / 9 / 6 | 68 / 6 / 6 |
| リーグ成績 | 173 / 6 / 9 | 185 / 6 / 9 |
| リーグタイトル | 64 / 9 / 8 | 63 / 9 / 8 |
| 試合一覧 | 100 / 17 / 7 | 95 / 14 / 7 |
| 試合詳細 | 486 / 33 / 18 | 508 / 34 / 18 |
| 戦力分析 | 41 / 9 / 9 | 38 / 6 / 9 |
| 戦力分析（起用マップ） | 31 / 9 / 9 | 28 / 6 / 9 |
| 戦力分析（FA取得） | 48 / 14 / 14 | 42 / 10 / 14 |
| ペナント一覧 `/pennant/` | 26 / 6 / 16 | 23 / 3 / 16 |
| 世界ダッシュボード | 143 / 3 / 49 | 148 / 3 / 49 |
| 世界の順位表 | 10 / 0 / 11 | 11 / 0 / 11 |
| 世界のリーグ詳細 | 14 / 1 / 12 | 13 / 1 / 12 |
| 世界のリーグ成績 | 125 / 0 / 17 | 127 / 0 / 17 |
| 世界のチーム一覧 | 10 / 1 / 9 | 8 / 1 / 9 |
| 世界の選手一覧 | 116 / 2 / 41 | 121 / 2 / 41 |
| 世界の試合一覧 | 14 / 1 / 13 | 14 / 1 / 13 |
| 世界の戦力分析 | 35 / 1 / 15 | 35 / 1 / 15 |
| 世界の球団ホーム | 56 / 4 / 26 | 56 / 4 / 26 |

- **クエリ数はすべての画面で基点と同じ**。球場を読むチーム一覧（実データ・世界）は 14→11ms・10→8ms で劣化は無い。ほかの画面の差（−6〜+22ms）は球場を読まない画面にも同じ幅で出ていて、共有コンテナの揺れの範囲。
- `/pennant/` のクエリ数（16）が T3 の表（15）より1本多いのは、T3 のレビュー M1（ログインしている人の作成フォームの上限に `count_by_owner` を読む）による。T4 の差ではない。

**レビューで残した軽微な点（T4）**

- 検査（`stadiums_in` の `exists()`）と書き込みの間に球場のワークスペースが変わる隙はあるが、登録済みの球場のワークスペースは読み取り専用なので実害は無い。
- 管理画面のオートコンプリート（チームの本拠地）の候補は別の URL が返すので、ほかのワークスペースの球場も並びうる（T7 と同じ。Wiki「チームと選手の管理」に書いてある）。送信すると選択肢の検査で弾くので、そのままにした。
