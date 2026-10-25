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
| サイトの登録画面 | リーグ（外国人枠と支配下選手の登録枠 `registered_player_limit` を含む）・チーム（所属リーグ・本拠地。表示順は一覧のドラッグ。W3 T8）・球場の作成・編集・削除（中が空のときだけ）。選手のプロフィール欄の編集 |
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
| 組み立て口 | T1 の前は18個（#105 の `build_pennant_history_service`、#193 の `build_world_team_analysis_service` を含む）。T1 で `build_world_view_service`・`build_world_team_analysis_service` を `build_service(scope)`・`build_team_analysis_service(scope)` に吸収して16個。その後 main の取り込みで `build_game_view_service(scope)`（2026-10-16）・`build_team_view_service(scope)`（2026-10-24 その2）が加わって18個、W2 T1 でワークスペースの台帳の `build_workspace_access_query()`・`build_workspace_list_query()` が加わって20個、main の取り込み（2026-10-24 その3）で `build_service` が `build_league_view_service` に改名され、ポストシーズンの `build_pennant_postseason_service`・`build_pennant_postseason_view_service`・`build_pennant_settings_service` が加わって23個、W2 T5 で `build_permission_query` を消して今は22個。すべて `scope` を既定値なしで受け取る（台帳だけの `build_pennant_world_view` は T3 まで範囲を取らない） | `views.py` |
| `scoped_url` | 入っている。context の `world` が無ければ `reverse(name)`、あれば `reverse("pennant_" + name, (world_id, *args))`。テンプレートで `{% scoped_url %}` 54か所、素の `{% url %}` 97か所（#130 などで変わっている。W2 の着手時に数え直す）。**W2 の後（T6 で数え直した）**: どちらも context の `workspace` の key を先頭に足す。`{% scoped_url %}` 74か所・`{% workspace_url %}`（名前を切り替えない。T3）90か所・`{% workspace_entry_url %}`（`/` の一覧の行ごとの入口。T4）2か所。素の `{% url %}` は29か所で、すべてワークスペースの外（管理画面・ログイン・ログアウト・新規登録・パスワード変更・ロゴと `404.html` の `home`）。中の画面を素の `{% url %}` で引くと `test_templates.py` の `WorkspaceUrlTagTest` が落とす | `templatetags/scoped.py`、`urls.py` の `_w()`・`_pennant()` |
| マイグレーション | main は `0045_pennant_club_plan` まで。#168 が `0046_playerstint_left_via` を取るので、**ワークスペースは 0047 から始まる**（#168 を取り込んだ後に番号を確かめる） | `myapp/migrations/` |

組み立て口（W1b T1 の後は16個。main の取り込みで2つ増え、2026-10-24 その2 の後は18個。W2 T1 の台帳の2つを含めると20個。2026-10-24 その3 の後は23個。W2 T5 で `build_permission_query` を消して22個）の範囲の受け方:

| 範囲の受け方 | 組み立て口 |
| --- | --- |
| 実データまたはペナントの世界（どちらも `scope` で受ける） | `build_league_view_service`（2026-10-24 その3 までは `build_service`）・`build_team_view_service`・`build_game_view_service`・`build_team_analysis_service`（実データなら年の基準は暦の今日、世界ならその世界の今日） |
| 実データ専用（ペナントの範囲なら `InvalidWorld`） | `build_recording_service`・`build_roster_service`・`build_player_search_query`・`build_pennant_world_service`（分岐元が実データ） |
| ペナント専用（実データの範囲なら `InvalidWorld`） | `build_pennant_ratings_service`・`build_pennant_season_service`・`build_pennant_offseason_service`・`build_offseason_view_service`・`build_pennant_home_service`・`build_pennant_history_service`・`build_club_service`・`build_club_details_service`・`build_pennant_postseason_service`・`build_pennant_postseason_view_service`・`build_pennant_settings_service` |
| 範囲を持たない | `build_pennant_world_view`（T3 で `workspace_id` を取る）。ワークスペースの台帳 `build_workspace_access_query()`・`build_workspace_list_query()`（W2 T1。何も取らない。使うのは `views.py` だけ） |

補助は `_repositories_for(scope)` と `_world_clock(world_id, ...)`。範囲を作るのは views の `_real_scope()`・`_world_scope(world_id)`（`views.py:185,190`）だけで、`api.py` もこれを使う（管理画面の概況のテンプレートタグは W2 T2 で範囲を取らない運営向けの概況になった）。共有ビューの範囲を決める分岐は `_scope(world_id)`（`views.py:490`）と、戦力分析用の `_analysis_scope(world_id)`（`views.py:312`）の2か所だが、どちらも「世界の有無で範囲を作って `build_league_view_service(scope)` を呼ぶ」だけになった。`test_wiring.py` は全組み立て口を検査している。

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
| `WorkspaceAccess` | 値オブジェクト（frozen） | 1回の要求で、その人がそのワークスペースで何をできるか。`role`（`OWNER` / `EDITOR` / `TEAM_MANAGER` / `VIEWER` / `NONE`）・`managed_team_ids`・`visibility` を持つ。メソッドは `can_view()`・`can_edit_all()`・`can_edit_teams(team_ids)`（どちらか1チームの担当なら可）・`can_manage_members()`・`can_delete()`。W2 T1 で `can_create_world()`（＝`can_edit_all()`。2026-10-24 決定）・`is_member` と、役割を導く `WorkspaceRole.of()`・組み立ての `WorkspaceAccess.of()` を足した（14. の W2 T1） |
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
| `LeagueSetupService`（W3 T1、`application/league_setup.py`。実データ専用） | 更新だけ。リーグ・チーム・球場の作成・編集・削除（中が空のときだけ。規則は `domain/setup.py`）。**選手のプロフィールの更新はここに置かず `RosterService` に置いた**（W3 の決定 4。名前・背番号と同じ1回の集約の保存）。一覧・初期値・消せない理由は参照クエリ `SetupQuery`（`build_setup_query(scope)`） | 範囲つきの `LeagueRepository`・`TeamRepository`・`StadiumRepository` |
| `LeagueViewService`（#219 段階5 までは `TeamApplicationService`） | **メソッドは足さない。** 範囲つきで組み立てられるだけ | — |
| `GameRecordingService` | 変更なし（範囲つきで組み立てる） | — |
| `TeamAnalysisService`・`RosterService` | 範囲つきで組み立てるだけ（戦力分析は `DjangoTeamAnalysisQuery(scope)`、契約区分・昇格・FA 宣言は `DjangoTeamRepository(scope)` で、すでに範囲で絞れている）。W3 T4 で `RosterService.update_player` にプロフィール（`ProfileInput`）を足した | — |
| ペナントの9サービス（`PennantWorldService`・`PennantSeasonService`・`PennantOffseasonService`・`OffseasonViewService`・`PennantHomeService`・`PennantRatingsViewService`・`PennantWorldViewService`・`ClubManagementService`・`ClubDetailsService`） | 変更なし。範囲（またはワークスペースの id）つきで組み立てるだけ | — |

- DTO（`application/dto.py`）: `WorkspaceContext`・`WorkspaceRow`・`MemberRow`・`InvitationView` など。素の dict は使わない。
  - **W2 T1 で入れた形**: `WorkspaceContext` は `workspace_id`・`key`・`name`・`access: WorkspaceAccess`。公開範囲は `access.visibility` を返す**プロパティ**（欄にしない）。世界 id は持たない（世界はビューが URL から台帳で引く）。`WorkspaceRow` は `key`・`name`・`visibility`・`role`。
  - ~~`WorkspaceContext`（key・名前・公開かどうか・自分の権限・世界 id）~~（当初の案。W2 T1 で上の形に置き換えた）
- 参照の Protocol（`application/queries.py`）:
  - **W2 T1 で入れた形**: `WorkspaceAccessQuery.resolve(key: str, user_id: int | None) -> WorkspaceContext`（無い key は `WorkspaceNotFound`）・`WorkspaceListQuery.list_for(user_id: int) -> list[WorkspaceRow]`。実装はどちらも引数なしで組み立てる台帳（`build_workspace_access_query()`・`build_workspace_list_query()`）。
  - ~~`WorkspaceAccessQuery.access_for(user_id | None, key) -> WorkspaceAccess`~~（当初の案。W2 T1 で `resolve` に置き換えた。ワークスペースの id・名前も同じ1本で読むため）

### infrastructure

- `orm_models.py`: 5. のとおり。
- `scoping.py`: 3.1 のとおり（`stadiums_in` を足し、条件にワークスペースを加える）。
- `repositories.py` / `queries.py`: epic で `WorldScope` を通した箇所はそのまま使う。追加は球場とメンバーの経路だけ。
- `DjangoTeamPermissionQuery` は `DjangoWorkspaceAccessQuery` に置き換える。1回の要求で読むのは、ワークスペースの行・編集者の行の有無・担当チームの id で、**多くて3クエリ**。判定は domain の `WorkspaceAccess` に任せる。
  - W2 T1 で入れた形: 編集者の行の有無はワークスペースの行と同じ1本（`Exists` の注釈）で読むので、未ログイン・持ち主・編集者は1クエリ、担当・ログインした部外者は2クエリ（14. の W2 T1）。置き換え（`DjangoTeamPermissionQuery` の削除）は T5。
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
  - P4a の `build_world_view_service(world_id)` は、W1b で `build_service(WorldScope.pennant(ws, w))` に吸収した（T1 で実施）。`build_service` は main の #219 段階5 で `build_league_view_service` に改名され、main の `build_world_league_view_service(world_id)` は 2026-10-24 その3 の取り込みで `build_league_view_service(scope)` に吸収した。
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
| `/w/<key>/` 以下 | 今のすべての画面（ダッシュボード・チーム・選手・試合・順位表・リーグ・タイトル・成績・選手検索・スコアブックの API）。W2 T4 で移した（古い URL は 404。リダイレクトしない） | 公開なら誰でも、非公開ならメンバーだけ | `WorkspaceAccess`（担当チーム、または全体の編集権。W2 T5。`is_staff` は何も足さない） |
| `/w/<key>/setup/leagues/`・`teams/`・`stadiums/`（W3。それぞれ `<id>/` が編集、`<id>/delete/` が削除の確認と削除。URL 名は `setup_leagues`・`setup_league_edit`・`setup_league_delete` などの9本） | リーグ・チーム・球場の一覧と作成・編集・削除（中が空のときだけ） | 全体の編集権（W3 の推奨で進めるもの 1。押しても弾かれるフォームだけの画面を担当・閲覧に見せない。中身は既存の画面で読める） | 全体の編集権 |
| `/w/<key>/settings/` | 名前・公開/非公開・削除への導線 | 持ち主 | 持ち主 |
| `/w/<key>/members/` | メンバーの一覧と役割（全体 / 担当チーム）・除名・招待リンクの発行と取消 | 持ち主 | 持ち主 |
| `/w/<key>/delete/` | 削除の確認 | 持ち主 | 持ち主 |
| `/invitations/<token>/` | 招待の受諾（未ログインならログインか新規登録へ誘導し、戻ってくる） | トークンを持つ人 | ログインが必要 |
| `/w/<key>/analysis/`・`/w/<key>/team/<id>/analysis/` | 戦力分析（main #144。今の `/analysis/`・`/team/<id>/analysis/`） | 公開なら誰でも、非公開ならメンバーだけ | — |
| `/w/<key>/pennant/...` | ペナント。**W2 T4 で移した**（2026-10-24 の決定。計画は W6）: 世界の一覧・世界の画面と `_pennant()` の全パターン（進める・締める・オフ・編成・ポストシーズン・設定・削除と参照画面）。古い `/pennant/` は 404 | ワークスペースに従う | 世界の作成は持ち主・編集者（W2）。進める・締める・編成・設定・削除は世界の作成者。上限・持ち主による削除・分岐元は W6（3.1） |
| `/accounts/...`・`/admin/` | 変更なし（管理画面は運営者専用） | — | — |

- **リンクの引き方:** P4a の `scoped_url` を一般化する。context の `WorkspaceContext`（key と、あれば世界 id）を読んで、`/w/<key>/[pennant/<w>/]...` を引く。今のテンプレートは `{% scoped_url %}` が54か所、素の `{% url %}` が97か所あり、後者を `scoped_url` に直すのが W2 の作業量になる。**W2 の後**: 中の画面は `{% scoped_url %}`（74か所）・`{% workspace_url %}`（90か所）・`{% workspace_entry_url %}`（2か所）で引き、素の `{% url %}`（29か所）はワークスペースの外だけ（3.1 の表）。views と API は `_url(request, …)`。
- **ヘッダー:** ログイン時はワークスペースの切り替え（ドロップダウン）を出す。ワークスペースの中では、パンくずの起点を「<ワークスペース名>」にし、公開・非公開と自分の権限をバッジで出す。
- **両立しない操作を並べない:** 公開と非公開は、設定画面の1つの選択欄で切り替える。招待を開いた人がすでにメンバーなら、「受諾」は出さずに「既にメンバーです」とだけ出す。削除は確認画面を挟む。
- **書き込みの導線は、権限の無い人に出さない**（今の規則を `WorkspaceAccess` から計算する）。
- **読み書きが同じ URL に同居する画面**: GET はワークスペースの閲覧権、POST は編集権で判定する。
- **並べ替え:** リーグとチームの表示順は、~~編集フォームの「表示順」の数値で決める（ドラッグは管理画面だけに残す）~~ **登録画面の一覧の行のドラッグで決める**（2026-10-25 ユーザー決定。W3 T8。編集フォームの表示順の欄は消した）。

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
| Wiki「仮想データの投入」「テストと品質」 | 漏れの検査（コマンド例の `--workspace`（`simulate_sample --from-real-leagues` を含む）は W1b T5 で直した） |
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
| W2 | #78 | `/w/<key>/`、`@workspace_view`、`WorkspaceAccess` と参照クエリ、`is_staff` の特権の廃止、`scoped_url` の一般化、公開・非公開、`/` の一覧、全 URL の漏れの検査。**ペナントの画面も `/w/<key>/pennant/` へ移し、世界の作成を持ち主・編集者に絞った**（2026-10-24 の決定。計画は W6）。完了（2026-10-25。14. の「W2 のまとめ」） | 移したデータを `/w/<key>/` で読める。非公開にすると未ログインでは見えない |
| W3 | #79 | リーグ・チーム・球場の登録画面、選手のプロフィール欄の編集、`LeagueSetupService`、管理画面の削除とチーム名の検査をサイトと同じ規則に。完了（2026-10-25。14. の「W3 のまとめ」） | 持ち主がブラウザだけでリーグを一から作れる |
| W4 | #80 | `WorkspaceService`: 作成（空）・設定・削除・上限・メンバー・招待（`Invitation`・`InvitedRole` と `WorkspaceInvitation` の表・マイグレーションはここで作る）・ヘッダーの切り替え | 2人目を招待して一緒に編集できる |
| W5 | #81 | 見本の生成（3.6） | 作成直後から順位表とボックススコアが見える。作成の所要時間を記録する |
| W6 | #82 | ペナントの権限をワークスペースに合わせる（3.1）: 世界の数の上限をワークスペースごとに読み替える・ワークスペースの持ち主による世界の削除・メンバーでなくなった作成者の扱い・分岐元。**URL の移動（`/w/<key>/pennant/`）と世界の作成の権限（持ち主・編集者）は W2 で済ませた** | ペナントがワークスペースの中で動く |
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

W2 で増えたもの（W2 の各段階の記録より。2026-10-25 の T6 でまとめた）:

- **既存の staff が既定のワークスペースの編集者になっているかを確かめる。** W2 T5 でサイトの `is_staff`・superuser の特権を廃止した。0060（バックフィル）が実行時点の staff・superuser を編集者（持ち主は superuser の1人目、いなければ staff の1人目）にするので、既定のワークスペースで今できていたことは失わない。ただし**バックフィルの後に staff にした人はサイトで何の権限も持たない**。デプロイの後、管理画面のワークスペースの編集者の一覧と、サイトで書いている人（運営者・チーム担当）を突き合わせ、足りない人を編集者に足す。チーム担当（`Team.managers`）は今のまま。
- **本番の `.env` に `SAMPLE_WORKSPACE_KEY` を足す**（見本のワークスペースの key。W2 T4）。無くても動く（未ログインのトップ `/` に見本へのリンクが出ないだけ）。key は管理画面のワークスペースの一覧で確かめ、そのワークスペースが公開であることも確かめる。手順は Wiki「本番公開」（反映済み）。
- **古い URL が 404 になることを周知する。** W2 T4 で中の画面をすべて `/w/<key>/` の下へ移し、古い URL（`/teams/`・`/players/`・`/pennant/` など）は**リダイレクトせず 404** にした。ブックマークや共有したリンクは開かなくなる。デプロイの告知で、新しい入口（`/` からログインしてワークスペースを選ぶ・見本のリンク）を知らせる。
- **ログイン後の行き先が `/` に替わった**（`LOGIN_REDIRECT_URL = "home"`。W2 T4）。ワークスペースが1つも無い人（一般の新規登録者）の `/` は「まだどのワークスペースにも入っていません」の案内だけになる（作成は W4）。
- デプロイの後、`measure_pages --workspace <既定の key>` を未ログイン・`--as <運営者>` で流し、W2 T5 の計測（14. の T5 の記録）と桁が変わらないことを確かめる。

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

#### T5

- T5（`feature/workspace-commands`）: 管理コマンドが `--workspace <key>` を受け取る。マイグレーションは無い。基点は T4（9e2c817。当初は d0b16c7）。
  - infrastructure: `infrastructure/workspaces.py` に `workspace_id_for_key(key)`（無い key は `WorkspaceNotFound`「ワークスペースが見つかりません（key=…）。」）。`workspace_id_of_world` は T3 で入っている。
  - `management/workspaces.py`: `real_scope_for_key(key)`（`--workspace` のワークスペースの実データの範囲）と `add_workspace_argument(parser, help=…)`。T3 の `legacy_world_scope`・`legacy_world_real_scope` は暫定ではないので `world_scope`・`world_real_scope` に名前を替えた。`legacy_real_scope` は `measure_pages` 専用で残す（W2 で消す）。
  - **省いたときは必ずエラー**（計画どおり）。ワークスペースが1つしか無くても推し量らない（取り違えた投入はエラーにならずにほかのワークスペースを書き換えるので、「1つなら黙って選ぶ」も入れない）。argparse の `required=True` は省いたときのエラーが英語になるので使わず、`real_scope_for_key` が日本語の `CommandError`（「--workspace でワークスペースの key を指定してください。」）にする。
  - コマンドごとの範囲:

    | コマンド | ワークスペース |
    | --- | --- |
    | `seed_virtual_players`（投入・`--refresh-schools`・`--rename-existing`・`--assign-readings`）・`seed_virtual_games`・`rebuild_fielding_lines` | `--workspace`（必須）。`handle` の最初に1回決める |
    | `pennant_create` | `--workspace`（必須）。分岐元はそのワークスペースのリーグだけ（ほかのリーグは「リーグが見つかりません」で世界を作らない）。世界はそのワークスペースに属す |
    | `simulate_sample` | `--from-real-leagues`（`--seasons` つきも）のときだけ `--workspace` が必須。既定・`--season` は DB を読まないので取らない（渡すとエラー）。`estimate_check.run_check`・`multi_season_check.run_seasons` は範囲を引数で受け取る |
    | `pennant_advance`・`pennant_close_season`・`pennant_record_awards` | `--world` の世界の行から導く（`world_scope`。T3 のまま） |
    | `pennant_delete` | `--world` の世界のワークスペースの実データの範囲で台帳を組む（`world_real_scope`。T3 のまま） |
    | `measure_pages` | 一時関数のワークスペース（`legacy_real_scope`）。`--workspace` は W2（決定の「推奨で進めるもの」3） |
    | `backup_db`・`runserver` | 対象外 |

  - **計画からのずれ**: (1) Wiki のコマンド例（「仮想データの投入」「アーキテクチャ」「成績と順位の見方」「ペナントモードの仕組み」）を T5 で直した（計画は epic を main に入れるとき）。epic の Wiki は main に入るまで公開されないので、コードと同時に公開される。8. の表もそう直した。(2) テストの土台は `WorkspaceCase` ではなく、`test_seed_commands_scope.py` の `WorkspaceCommandsCase`（A と B に試合を組める2球団のリーグを置き、`seed_virtual_games` で試合を入れる）にした。`WorkspaceCase` の B は相手チームに選手がおらず、試合の投入・世界の進行ができないため。
  - テスト:
    - `test_seed_commands_scope.py`: 既存の世界の検査は `--workspace` を付けて残した。`WorkspaceOptionTest`: 7通り（`seed_virtual_players` の投入と補正・`seed_virtual_games --replace`・`rebuild_fielding_lines`・`pennant_create`・`simulate_sample --from-real-leagues`（`--seasons` つきも））で、省くと・無い key だと日本語のエラーで A も B も変わらない。`simulate_sample` の既定は `--workspace` なしで動き、`--from-real-leagues` なしの `--workspace` は「--workspace は --from-real-leagues と一緒に指定してください。」で止まる（黙って捨てると実データで回したと読み違えるため）。`SeedCommandsWorkspaceTest`: B を指した投入・補正3つ・`--replace`・別の年の投入（打席に B 以外の選手が立たない）・守備成績の導き直しで、B は変わり A の行（選手・在籍・試合・守備・世界・世界の試合）は変わらない。`PennantCommandsWorkspaceTest`: `pennant_create --workspace A --league <B のリーグ>` は世界を作らない・B を指せば世界は B に属す・`pennant_advance --world <B の世界>` は B の世界を進め A は変わらない・`simulate_sample --workspace B --from-real-leagues <A のリーグ>`（`--seasons 1` つきも）はリーグが見つからない（A は id が最小なので、一時関数に落ちる退行なら読めてしまう向きで確かめる）。
    - `test_wiring.py`（`LegacyRealScopeTest`）: `legacy_real_scope` の名前を出すのは `management/workspaces.py` と `measure_pages.py` だけ、`legacy_workspace_id` は `infrastructure/workspaces.py`・`management/workspaces.py`・`presentation/views.py` だけ（新しいコマンドが一時関数で範囲を作ると、ワークスペースが複数あるときに黙って id が最小のものを書き換える）。
    - 既存のコマンドのテスト（`test_seed_virtual_*`・`test_fielding.py`・`test_world_fork.py`・`test_simulate_sample.py`）は既定のワークスペースの key（`DEFAULT_WORKSPACE_KEY`）を渡す。
    - **変異の確かめ**: `seed_virtual_players` と `rebuild_fielding_lines` の範囲を一時関数のワークスペースに戻すと `SeedCommandsWorkspaceTest` の3件（サブテストを含め6件）が落ち、`real_scope_for_key` が key を見ずに一時関数を使うと `WorkspaceCommandsCase` の準備（B への投入）が落ちることを確かめ、戻した。
  - T6 に残すもの: 設計書 14. の W1b のまとめ、CLAUDE.md の差分（組み立て口の一覧・範囲の規則・例外）をユーザーに見せてから直す、`.claude/rules/tests.md`・`ddd-boundary-reviewer.md` の `build_service()` の表記、#172 の本文の更新。`measure_pages --workspace` と `legacy_real_scope`・`legacy_workspace_id` の削除は W2。

#### W1b のまとめ（T6、2026-10-24）

**何をしたか**: 範囲（`WorldScope`）がワークスペースを持ち、実データ・世界の台帳・球場・管理コマンドのすべての読み書きをワークスペースで絞った。

| 段階 | PR | 内容 |
| --- | --- | --- |
| T0 | 00a4f84（直接）・#252 | main の取り込みと、W1b の前後を比べる基準値 |
| T1 | #254 | 組み立て口がすべて範囲を必須で受け取る（振る舞いは変えない）。2組をまとめて16個に |
| T2 | #265 | `WorldScope(workspace_id, world_id)`。実データはワークスペースで絞る。`players_in` は肯定形。管理画面専用の `admin_real_queryset`（Q4） |
| T3 | #273 | 世界の台帳をワークスペースで絞る（Q3）。保存の乗っ取りを防ぐ。世界の画面は台帳で引けた世界だけ |
| — | #276・#278 | main の取り込み（#219 段階0〜3・#204・#248・#108・#97）。マイグレーションを 0055〜0057 に振り直した |
| T4 | #282 | 球場を範囲で引き、本拠地がほかのワークスペースの球場なら保存を拒む |
| T5 | #283 | 管理コマンドが `--workspace <key>` を受け取る（省くと必ずエラー）。`--world` は世界の行から導く |
| T6 | この PR | 記録と CLAUDE.md・`.claude/rules/tests.md`・`ddd-boundary-reviewer.md` |

**決定**（すべて #172 のコメント）: 2026-10-07 の Q1〜Q4、2026-10-11 の R1・R2、2026-10-15 の「T1 を先に始める」「推奨で進めるもの 4点」「T2 は #39 S1 を待たずに始める」、2026-10-16 の「#219 段階4・5 と並行し、epic で吸収する」、2026-10-24 の CLAUDE.md の文面。

**計測**（T0 の手順で、同じ日の基点と交互に3回ずつ）: クエリは T2 で全画面ちょうど +1（ワークスペースを引く1本）、T3・T4 は増減なし（T3 の後の直しで、ログインした人の `/pennant/` だけ +1）。応答時間は揺れの範囲。

**漏れを拾えることの確かめ**（変異）: 実データの条件を戻すと 40件（T2）、`worlds_in` を全件にすると 30件（T3）、`_stadium_condition` を全件にすると 19件（T4）、コマンドの範囲を一時関数に戻すと 3件（T5）落ちる。

**W2 以降に残したもの**
- ~~今の URL のワークスペースは一時関数 `legacy_workspace_id()`（id が最小）で決める。W2 で URL の key から決める。`measure_pages --workspace` と `legacy_real_scope` の削除も W2~~ → W2 T3（`measure_pages --workspace`・`legacy_real_scope` の削除）と T4（URL の key から `workspace_view` が解決。`legacy_workspace_id` の削除）で済んだ
- ~~確かめた世界の id を要求に覚える仕組みは、W2 で（ワークスペース, 世界）の組で覚える~~ → W2 T4 で済んだ
- ~~管理画面のトップの概況は一時関数のワークスペースだけを数える（W2 で運営向けに替える）~~ → W2 T2 で運営向けの概況にした
- ペナントの条件に `world__workspace_id` を足す二重の防御（結合が1段増えるので、測ってから決める）
- 世界の数の上限は全ワークスペースで数える（W6 で見直す）
- 名前の一意の違反を日本語の例外に訳すのは W3
- 管理画面のオートコンプリートの候補は絞っていない（保存で弾く）
- epic を main に入れるとき: 上の「epic を main に入れるときに必ず行うこと」（superuser の確認と本番公開の手順）と、main を取り込むたびのマイグレーション番号の確認

### W2（#78）

#### T1 `WorkspaceAccess` と参照クエリ（`feature/workspace-access`、2026-10-24）

画面はまだ使わない（T4 の `workspace_view`・T5 の権限の切り替えが使う）。マイグレーションは無い。

- domain（`domain/workspace.py`）: `WorkspaceRole`（`OWNER`・`EDITOR`・`TEAM_MANAGER`・`VIEWER`・`NONE`。表示名「持ち主」「編集者」「チーム担当」「閲覧」「関係なし」もここが出典）と `WorkspaceRole.of(owner_id, visibility, user_id, is_editor, manages_a_team)`（役割の優先 持ち主 > 編集者 > 担当 > 閲覧）。`WorkspaceAccess(role, visibility, managed_team_ids)` は `of()` で作り、`can_view()`・`can_edit_all()`・`can_edit_teams(team_ids)`・`can_create_world()`・`can_manage_members()`・`can_delete()` を持つ。`is_staff` は引数に無い。
  - `can_create_world()` は `can_edit_all()` と同じ（2026-10-24 の決定。世界の作成は持ち主・編集者だけ）。計画の5つのメソッドに、名前で呼べるよう足した。
  - 値の組み合わせを `__post_init__` で検査する: 担当チームを持つのはチーム担当だけ（持ち主・編集者が担当を兼ねても持たない）、非公開の `VIEWER`・公開の `NONE` は作れない、未ログインは編集者・担当になれない。
- application: `WorkspaceAccessQuery.resolve(key, user_id) -> WorkspaceContext`（無い key は `WorkspaceNotFound`）・`WorkspaceListQuery.list_for(user_id) -> list[WorkspaceRow]`。DTO の `WorkspaceContext` は `workspace_id`・`key`・`name`・`access`。**公開範囲は `access.visibility` を返すプロパティにした**（計画は欄だったが、同じ値を2つの欄に持たないため）。
- infrastructure（`infrastructure/queries.py`）: `DjangoWorkspaceAccessQuery`（1本目でワークスペースの行と、ログインしていれば `Exists(WorkspaceEditor)` の注釈。持ち主・編集者・未ログインはここで終わる。それ以外は2本目で `teams_in(WorldScope.real(ws)).filter(managers=…)` の id）。`DjangoWorkspaceListQuery`（`Exists` の注釈2つ（編集者・担当）と持ち主の条件で1本。名前順。メンバーでないワークスペースは公開でも出さない）。担当の相関副問い合わせのためのたどり方は `scoping.real_teams_of_outer_workspace()` に置いた（`teams_in` の、ワークスペースの id が外側の行で決まる版）。
- presentation: 組み立て口 `build_workspace_access_query()`・`build_workspace_list_query()`（何も取らない台帳）。`tests/helpers.py` が再輸出する。
- テスト: domain `test_workspace_access.py`（役割5 × 公開範囲2 × 操作9 の真理値表を表のまま持つ・役割の優先・`is_staff` の入る余地が無い・組み合わせの検査）。integration `test_workspace_access_query.py`（key で引ける・無い key・役割が表の事実から決まる・staff かつ superuser の部外者は何も得ない・担当はそのワークスペースの実データのチームだけ（B のチーム・A の世界の球団は入らない）・クエリ数・一覧）。`test_wiring.py` は新しい分類 `WORKSPACE_LEDGER_BUILDERS`・`WORKSPACE_LEDGERS` を足し、何も取らない組み立て口と実装がこの2つずつだけであることを名前で固定した。`test_workspace_isolation.py` の `CHECKS` に2つを足した（A の key で B の行が出ない・B の持ち主・編集者・担当は A では閲覧者・一覧に入っていないワークスペースは出ない）。

**クエリ数**（`assertNumQueries` で固定）

| 呼び出し | 未ログイン | 持ち主 | 編集者 | 担当 | ログインした部外者 | 無い key |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `resolve` | 1 | 1 | 1 | 2 | 2 | 1 |
| `list_for` | — | 1（ワークスペースの数によらない） | 同 | 同 | 同 | — |

**変異の確かめ**: `resolve` が key を見ない → 16件、担当チームを範囲で絞らない → 7件、`can_edit_teams` が常に真 → 24件、一覧が公開ワークスペースも出す → 7件落ちる（どれも戻した）。

**T6 で直すもの**（CLAUDE.md。差分をユーザーに見せてから）: 範囲の規則の2つの例外の書き方が、T1 で入れたものを名前で挙げていない。

- 「全範囲を読む API を作らない。例外は `admin_real_queryset` だけ」に、`scoping.real_teams_of_outer_workspace`（外側の `Workspace` に相関した副問い合わせとしてだけ働く。`infrastructure/queries.py` だけが使い、`test_wiring.py` の `OuterWorkspaceTeamsTest` が縛る）を足す。
- 「組み立て口はすべて範囲を受け取る。例外は `build_pennant_world_view`」に、引数を取らないワークスペースの台帳2つ（`DjangoWorkspaceAccessQuery`・`DjangoWorkspaceListQuery`）とその組み立て口（`build_workspace_access_query()`・`build_workspace_list_query()`。key・利用者からワークスペースを引く）を足す。

#### main の取り込み（2026-10-24 その2）

W2 の T0。`refactor/merge-main-into-workspaces-3` で、epic 2e76eae に main b4d0b26 を取り込んだ。

- main から来たもの: #219 段階4（`TeamApplicationService` から球団と選手の画面の参照を `TeamViewService` へ。組み立て口 `build_team_view_service()`・`build_world_team_view_service(world_id)`）、#204 段階B0・B（FA の推定の材料の読み口を `DjangoServiceHistoryQuery` に切り出し、FA 資格を取得した外国人を翌年から外国人枠の対象外に。`ForeignQuotaExemptions` を記録・進行・締め・編成のサービスに渡す。シミュレーションの外国人を `counts_as_foreign` に改名）、依存の更新（#277）。**マイグレーションは無い**ので振り直しは無し（epic は 0055〜0057 のまま）。
- 衝突は9ファイル（`views.py`・`test_wiring.py`・`helpers.py`・`base.py`・`test_pennant_screens.py`・`test_pennant_season_stats.py`・`test_splits.py`・`.claude/rules/tests.md`・Wiki「アーキテクチャ」）。どれも main の分割（`TeamViewService`）と epic の範囲の形を両方残した。
- 組み立て口: `build_team_view_service` は `scope` を既定値なしで受け取る形にし、`build_world_team_view_service(world_id)` を吸収した（年齢の基準日は `_today_for(scope)`）。分類は `BOTH_BUILDERS`。`_team_view_scope(request, world_id)`・`_analysis_scope` は `_real_scope(request)`・`_world_context`・`_world_scope` で範囲を作る。選手の編集画面は `build_team_view_service(_real_scope(request))`、GM ホームの主力は `build_team_view_service(scope)`。main が `build_world_team_analysis_service` に足した `service_history_query` は `build_team_analysis_service(scope)` に足した。
- FA の対象外（#204 段階B）: `ForeignQuotaExemptions(DjangoServiceHistoryQuery(scope))` は各組み立て口の範囲で作る（記録は実データ専用、進行・締め・編成はペナント専用の範囲）。`DjangoServiceHistoryQuery` は main の時点で `scope` 必須で、出場は `world_condition`、在籍は `stints_in`、選手は `players_in`、記録のある最初の年は `games_in` で読むので、実装は変えていない。FA 宣言は今まで選手の id だけで絞っていた（ほかのワークスペースの選手の id を渡されると漏れる）ので、`scoping.py` に `declarations_in(scope)`（`players_in` と同じ在籍の条件を、宣言の行ごとに相関した `Exists` で見る。同じ1本の中なのでクエリ数は変わらない。`player_id__in=players_in(scope)` の形は相関しない副問い合わせで、チームを読むたびに全ワークスペースの選手を走査するのでやめた（レビュー））を足し、宣言を読む3か所（`DjangoServiceHistoryQuery._declarations`・`DjangoTeamAnalysisQuery._moves`・`repositories._declarations_of`。`_RosterData.for_players` に範囲を渡す）をそこへ通した。チームの保存の取り消した宣言の削除（集約の選手の id と宣言の id で絞る書き込み）と管理画面は変えていない。
- テスト: `test_workspace_isolation.py` の `CHECKS` に `DjangoServiceHistoryQuery` を足し、`DjangoTeamAnalysisQuery` の検査にあった `load_service_history` をそこへ移して `load_fa_subjects`（B の選手が載らない・A の選手は載る・記録のある最初の年が B に引きずられない）を足した。B の打者に FA 宣言を1つ置き、A と B の選手の id を混ぜて渡しても、宣言の読み口（FA の推定の材料の2つと `_declarations_of`）と集約に A の宣言だけが出ることを確かめる（`declarations_in` を全件に戻す・`_declarations_of` を id だけの絞り込みに戻すと落ちることを確かめた）。`test_wiring.py` は `TeamViewService` の検査を範囲の形にし、記録・進行・締め・編成の FA の対象外の読み口が組み立て口の範囲で作られることと、世界の「今日」の台帳の検査に `build_team_view_service` を足した。main の新しいテスト（`test_pennant_foreign_quota.py`・`test_team_analysis_fa.py`）の `WorldScope.real()`／`WorldScope.pennant(id)`・`build_pennant_*_service(world_id)` を `real_scope()`・`world_scope(id)` に替えた。`test_team_analysis_fa.py` のクエリ数の検査（4本・0本）は、範囲を作る読み取り（ワークスペースを引く1本）を数える区間の外で済ませる形にした。
- **計測（T0 の手順。2026-10-24）**: 開発 DB を `backup()` で `/tmp/w2-merge/db.sqlite3` に写し（`integrity_check` ok・試合 3,480）、`migrate`（0055〜0057）。同じ写しで基点 2e76eae（使い捨ての worktree）とこの取り込みを交互に3回ずつ `measure_pages`。中央値（応答 ms / SQL ms / クエリ）: チーム一覧 11/3/3 → 11/3/3、選手一覧(野手) 281/10/28 → 277/9/28、選手一覧(投手) 265/7/28 → 266/7/28、選手詳細 90/2/16 → 91/2/16、戦力分析（FA取得） 41/11/14 → 42/10/14。**クエリ数はすべての画面で同じ**で、応答時間は揺れの範囲。ペナントの画面は測っていない。写しと使い捨ての worktree は計測後に消した。

#### T2 管理画面の運営向けの概況（`feature/workspace-operator-overview`、2026-10-24）

管理画面のトップの概況を、一時関数のワークスペースの数字から、運営向けの全ワークスペースの数字に替えた（W1b の TODO）。マイグレーションは無い。

- 置き場所: 全ワークスペースを読む集計なので、Q4 の例外（`admin_real_queryset` は `admin.py` だけ）の中に置いた。`admin.py` の `operator_overview() -> OperatorOverview`。テンプレートタグ `templatetags/admin_overview.py` は `takes_context` と `_real_scope` をやめ、これを呼ぶだけ（`admin` は描画の時点で読み込み済みなので、循環を避けて関数の中で引く）。**例外の範囲は広げていない**（`AdminOnlyScopeExceptionTest` は変えずに通る。テンプレートタグは関数の名前も出さない）。
- 中身（`application/dto.py` の `OperatorOverview`・`OperatorWorkspaceRow`）: ワークスペース数（公開・非公開の内訳）・ユーザー数・全ワークスペースの実データのリーグ・チーム・選手・試合の数、試合の多いワークスペース上位5つ（`OPERATOR_TOP_WORKSPACES`。名前・key・持ち主・試合数。同数は古い順）。行数は `admin_real_queryset` で数えるので、**リンク先の一覧（全ワークスペースの実データ）と件数がそろう**（選手は在籍の無い選手も数える。ペナントの世界の行は数えない）。上位の試合数は、ワークスペースに相関した副問い合わせ（実データの試合をホームのリーグのワークスペースで数える）を `Workspace` の注釈にした。
- **クエリ数は7本で、ワークスペースの数によらない**（ワークスペースの集計1・ユーザー1・リーグ・チーム・選手・試合の4・上位1）。
- ワークスペースが1つも無い DB でも、概況はすべて0で出し、最初のワークスペースを作る導線（ワークスペースの追加画面）を出す。管理画面のトップは開ける。
- 消したもの: `TeamApplicationService.get_admin_overview` と `AdminOverview`（呼ぶ側が無くなった）。前の概況にあった注記（成績が未入力の選手・選手のいないチーム・退団した選手の数）は、計画の中身に無く、全ワークスペースの集約を組み立てないと出せない（成績の未入力はドメインの規定打席の判定を使う）ので出さない。在籍選手の「うち投手」も同じく外した。
- #219 段階5（PR #288）との関係: main で `get_admin_overview` が `AdminOverviewService`（組み立て口 `build_admin_overview_service()`）へ移る。次の main の取り込みで、そのサービス・組み立て口・`test_wiring.py` の分類を消し、この運営向けの概況を残す（計画の「衝突」の表のとおり）。
- テスト: integration `test_admin_overview.py`（A・B 両方の行を数える・ペナントの行を数えない・公開と非公開の内訳・上位の並びと件数の上限・一覧の `result_count` と一致・クエリ数がワークスペースを増やしても7本・トップに出る・ワークスペースが無くてもすべて0で開く）。`test_admin.py` の前の概況の検査4件を消し、`test_workspace_isolation.py` の `NoWorkspaceTest` を「概況を出さない」から「最初の1つを作る導線を出す」に替えた。
- **変異の確かめ**: 行数を `admin_real_queryset` でなく全行で数える → 5件、上位を試合数で並べない → 2件、上位の試合数をワークスペースで相関させない → 2件落ちる（どれも戻した）。

#### T3 URL の組み立てを補助に寄せる（`refactor/workspace-url-helpers`、2026-10-24）

**URL も振る舞いも変えない**（T4 の差分を URL と解決の切り替えだけにする）。マイグレーションは無い。補助はどれも今は `reverse(name, args=args)` と同じで、T4 でワークスペースの key を先頭の引数に足す。

- `myapp/urls.py`: ワークスペースの中の画面43本（実データ20・`pennant_index`・`pennant_world`・`_pennant()` の21本）を `workspace_urlpatterns` にまとめ、`urlpatterns = [path("", include(workspace_urlpatterns)), signup]`。T4 は `""` を `w/<wskey:key>/` に替える。`config/urls.py` は変えていない。計画の登録の補助（`_w(...)`。`workspace_view` で包む）は T4 で足す（T3 では包むものが無い）。
- テンプレート（`templatetags/scoped.py`）: `{% workspace_url %}`（名前を切り替えない）を足し、`scoped_url` と共通の `_workspace_reverse(context, name, args)` を通す（T4 で key を足すのはこの1か所）。素の `{% url %}` で中の画面を引いていた **74か所**を `workspace_url` に替えた（`scoped_url` 69か所は書き換え不要）。`{% load scoped %}` の無かった15ファイルに足した（`{% extends %}` の無い部品は、出力に改行を足さないよう次のタグと同じ行に置いた）。
  - **計画からのずれ: 素の `{% url %}` のまま残したのは4か所**（計画はロゴと `404.html` の2か所）。ワークスペースの外の画面（パスワード変更の2画面。`registration/password_change_form.html`・`password_change_done.html`）の「チーム一覧へ」も、外の画面にはワークスペースが無く key を足せないので、ロゴ・`404.html` と同じく T4 で `home` へ向け替える。計画の「78か所」は、管理画面のドキュメント（`django-admindocs-docroot`）2つを中に数えていた（素の `{% url %}` 97 = 管理画面15・ログイン等4・中74・外から中4）。
  - 残りの素の `{% url %}` 23か所は、管理画面・ログイン・ログアウト・新規登録と、上の4か所。
- views・API: `_url(request, name, *args)`（`views.py`）。`reverse`・`redirect("名前", …)` の20か所（`views.py`。キーワード引数の `redirect` 4か所を含む）と `api.py` の `redirect_url` 1か所を寄せた。スコアブックの埋め込みの `urls` もここを通る。`_plan_notices` は URL を組まない（タブの名前だけ）ので変えていない。
- テスト: `tests/helpers.py` に `ws_url(name, *args, workspace=None)`（`workspace` は `orm_models.Workspace`。T3 では使わない。DB も読まない）と `header_pages()`（ヘッダーの出口。3ファイルが同じ定数を持っていた）。`reverse(…)` 434か所（AST で書き換え。52ファイル）・引数の名前が変数の `reverse` 7か所・直書きの URL 約30か所（`test_world_isolation.py`・`test_team_analysis_world.py`・`test_pennant_*`・e2e の `test_smoke.py`・`test_pennant_flow.py`）を替えた。管理画面・ログイン・パスワード変更・`LOGIN_REDIRECT_URL` の `reverse` は替えていない。計画の「直書き59行」は数え方の違いで、URL の直書きとして残っているのは、部分一致で見る目印（`"/edit/"`・`"/history/"`・`f"/team/{id}/"` の `assertNotIn` など。key が入っても意味が変わらない）と、`RequestFactory().get("/")`（ビューの内部を呼ぶための要求で、URL の解決を通らない）と `test_csrf.py`（未知のホストで 400）だけ。e2e の `wait_for_url` の正規表現（`/pennant/\d+/$` など）は部分一致なので、key が前に付いても通る。ログイン後の `wait_for_url(f"{live}/")` は T4 でも `/`（`home`）なので変えていない。
- 検査（`test_templates.py` の `WorkspaceUrlTagTest`、`SimpleTestCase`）: `workspace_urlpatterns` から URL 名を集め、(1) 素の `{% url %}` が中の名前を引いていたら落とす（例外は上の4か所の表 `OUTSIDE_LINKS`。T4 で `home` に替えたら表から消す）、(2) 例外の表が古くなっていない、(3) `workspace_url`・`scoped_url` は中の名前だけを引く。`game_form.html` の1か所を素の `{% url %}` に戻すと (1) が場所つきで落ちることを確かめた。`epic/postseason` を取り込むとき、新しいテンプレートの素の `{% url 'pennant_…' %}` はここが場所を示す。`test_scoped_url.py` に `workspace_url` の検査（`{% url %}` と同じ・世界の中でも名前を切り替えない）を足した。
- `measure_pages`: `--workspace <key>`（必須。`management/workspaces.py` の `add_workspace_argument`・`real_scope_for_key`。省いた・無い key は日本語の `CommandError`）、`--as <ユーザー名>`（`force_login` して測る。無いユーザーは `CommandError`）、`--world <id>`（ペナントの10画面も測る。W1b T0 の `measure_pennant.py` と同じ並び・リーグと球団は世界の最初の行。ほかのワークスペースの世界は `CommandError`）。URL は `_url(key, name, *args)`（`reverse`）で組む。`--profile` は `--as` のユーザーで測る。`legacy_real_scope` を消し、`test_wiring.py` の `LegacyRealScopeTest` は「どの管理コマンドも使わない」と「`legacy_workspace_id` は定義と views だけ」に替えた。新しい `test_measure_pages.py`（必須・無い key・16画面が 200・id は指したワークスペースから拾う・`--as`＋`--world` で26画面が 200・ほかのワークスペースの世界・無いユーザー）。T3 の時点では今の URL が開くのは id が最小のワークスペースだけなので、ほかの key を指すと画面は 404 になる（docstring と Wiki に書いた）。
- 振る舞いを変えていないことの確かめ: 下の計測の写しの上で、基点 0f694b1（使い捨ての worktree）と T3 のコードで、実データ22・ペナント22・ログイン・新規登録の46の URL を未ログイン・`admin`・担当の3通り（138要求）描き、状態コード・リダイレクト先・HTML（CSRF のトークンを伏せる）のハッシュが**すべて一致**した。
- **T4 で一緒に直すもの**（レビューで挙がった、URL を組む処理ではないが key が入ると変わる箇所）: `config/settings.py` の `LOGIN_REDIRECT_URL = "dashboard"`（外から中への出口。計画どおり `home` に）。`presentation/csrf.py` が API かどうかを `request.path.startswith("/api/")` で決めている（`/w/<key>/api/...` に移ると、CSRF の失敗が JSON ではなく 403.html になる。`test_csrf.py` が `ws_url` 経由なので T4 で落ちて気づける。URL 名（`resolver_match`）で判定する形に替える）。
- Wiki「アーキテクチャ」（URL の組み立ての補助・`measure_pages` の引数）・「ペナントモードの仕組み」・「仮想データの投入」、`.claude/rules/templates.md`（中の画面は `scoped_url`/`workspace_url`）・`.claude/rules/performance.md`・`run-tests` スキルの `measure_pages` を直した。

**W2 の計測の基準（T0 の手順。2026-10-24）**: 開発 DB を `backup()` で `/tmp/w2-t3/db.sqlite3` に写し（`integrity_check` ok・試合 3,480）、`migrate`（0055〜0057。既定のワークスペースの key は `h8ta3y38rs`）→ `pennant_create --workspace h8ta3y38rs --name w2-baseline --league 1 --league 2 --year 2030 --seed 20261015 --owner admin --managed-team 1`（世界 1・リーグ2・球団12・選手966人）→ `pennant_advance --world 1 --to week`（6日・36試合）。写しの上だけで `s-sato`（staff でない）を球団 1 の担当にした（`Team.managers`）。`admin` は superuser で、既定のワークスペースの持ち主。同じ写しで、基点 0f694b1（使い捨ての worktree。`measure_pages` は引数を取らないので未ログインの実データだけ）と T3 の3通り（`--workspace h8ta3y38rs`／`--as admin --world 1`／`--as s-sato`）を交互に3回ずつ流した。中央値（応答 ms / SQL ms / クエリ）。写しと使い捨ての worktree は計測後に消した。T4・T5 はこの手順で写しを作り直し、T3 の先端と交互に流して比べる。

| 画面 | 基点・未ログイン | T3・未ログイン | T3・持ち主（admin） | T3・担当（s-sato） |
| --- | ---: | ---: | ---: | ---: |
| ダッシュボード | 456/59/11 | 449/60/11 | 489/60/13 | 453/60/13 |
| チーム一覧 | 10/3/3 | 10/3/3 | 11/3/5 | 11/3/5 |
| 順位表 | 82/3/5 | 83/3/5 | 66/3/7 | 87/3/7 |
| 順位表(2026) | 63/3/5 | 64/3/5 | 63/3/7 | 62/3/7 |
| 選手一覧(野手) | 253/8/28 | 258/7/28 | 256/8/31 | 263/8/31 |
| 選手一覧(投手) | 247/6/28 | 254/6/28 | 253/6/31 | 253/7/31 |
| 選手詳細 | 86/2/16 | 84/2/16 | 84/2/19 | 89/2/19 |
| 選手検索 | 14/1/5 | 12/1/5 | 13/1/7 | 15/1/7 |
| リーグ詳細 | 67/6/6 | 63/6/6 | 104/6/8 | 68/6/8 |
| リーグ成績 | 164/5/9 | 159/5/9 | 133/5/11 | 175/6/11 |
| リーグタイトル | 64/8/8 | 63/8/8 | 65/8/10 | 64/8/10 |
| 試合一覧 | 96/14/7 | 96/14/7 | 97/14/10 | 97/14/10 |
| 試合詳細 | 479/32/18 | 478/32/18 | 479/32/21 | 473/32/21 |
| 戦力分析 | 35/6/9 | 36/6/9 | 38/6/11 | 37/6/11 |
| 戦力分析（起用マップ） | 27/6/9 | 25/5/9 | 28/6/11 | 28/6/11 |
| 戦力分析（FA取得） | 40/10/14 | 39/10/14 | 41/10/16 | 41/11/16 |

ペナント（T3・持ち主 `admin`。`measure_pages --world 1`）: ペナント一覧 24/3/16、世界ダッシュボード 155/3/53、世界の順位表 11/0/11、世界のリーグ詳細 13/1/12、世界のリーグ成績 113/0/17、世界のチーム一覧 8/1/9、世界の選手一覧 111/2/45、世界の試合一覧 12/1/13、世界の戦力分析 32/1/15、世界の球団ホーム 56/4/30。

- 未ログインは基点と T3 で**全画面のクエリ数が同じ**、応答時間は揺れの範囲（URL を組む補助は `reverse` を1段包むだけ）。
- ログインした人は、全画面で +2（セッションと利用者の行）。権限を判定する画面（選手一覧・選手詳細・試合一覧・試合詳細）はさらに +1（`DjangoTeamPermissionQuery`。持ち主・担当とも1本）。T4 の完了条件（担当・部外者は +1 まで）と T5 の見込み（持ち主・編集者は判定のある画面で −1）は、この表の持ち主・担当の列と比べる。
- ペナントの画面は W1b T0（未ログインではなく superuser で測った同じ手順）と比べて、クエリは一覧 +2・世界の画面 +1〜+5（W1b T0 の後の変更の分。内訳は追っていない）。応答時間は同程度。

#### T4 URL にワークスペースを入れる（`feature/workspace-urls`、2026-10-24）

W2 の本体。ワークスペースの中の画面43本（実データ20・ペナント23）を `/w/<key>/` の下へ移し、URL の key から解決するようにした。マイグレーションは無い。

- URL: `config/urls.py` が `path("w/<wskey:key>/", include(workspace_urlpatterns))`、`/` は新しいトップ `home`。パスコンバータ `presentation/converters.py` の `WorkspaceKeyConverter`（正規表現はドメインの `KEY_LENGTH`・`KEY_ALPHABET` から作る）。形の合わない key は URL の解決で外れ、DB を読まずに 404。古い URL（`/teams/`・`/pennant/` など）は 404（リダイレクトしない）。
- 登録の補助 `myapp/urls.py` の `_w(route, view, name, *, login=False, json=False)`（`_pennant()` も通す）が**必ず** `views.workspace_view` で包む。`login=True` は試合の登録・編集と選手の編集（`@login_required` は外した）、`json=True` はスコアブックの保存 API。
- `workspace_view`（`views.py`）: `build_workspace_access_query().resolve(key, user_id)`（1要求で1回）→ 無い key は 404 → `can_view()` でなければ未ログインはログインへ（`next` つき）・ログイン済みは 404 → `login=True` の画面と未ログインの POST はログインへ → `request.workspace`（`WorkspaceContext`）を載せて key 抜きの引数でビューを呼ぶ。`json=True` は未ログイン 403・無い／見えない 404 の JSON（未ログインは key を引く前に 403。API の中の未ログインと「ワークスペースがありません」の分岐は消した）。
- `_workspace_id(request)` は `request.workspace.workspace_id`（載っていなければ `WorkspaceNotResolved`。黙って既定に落とさない）。`_url(request, …)` も同じ key を足す。`_verified_world_ids` は（ワークスペースの id, 世界の id）の組。`infrastructure/workspaces.py` の `legacy_workspace_id()` を消し、`test_wiring.py` の `LegacyWorkspaceIsGoneTest` が `legacy_workspace_id`・`legacy_real_scope`・`_legacy_workspace` の名前がテスト以外の `myapp/` に無いことを固定する。
- テンプレート: コンテキストプロセッサ `presentation/context_processors.workspace`（`settings.TEMPLATES` に登録）が `workspace` を渡し、`scoped_url`・`workspace_url`（`_workspace_reverse`）がその key を先頭に足す。`workspace` が無ければ `WorkspaceMissing`（key の無い URL を作らない）。`/` の一覧は行ごとの key で入口を引く `{% workspace_entry_url key %}` を足した（計画に無い。素の `{% url 'dashboard' key %}` は T3 の検査が中の名前として弾くため）。
- `base.html`: ロゴは `home`。ワークスペースの中（`{% if workspace %}`）でだけ、名前（ダッシュボードへ）・公開範囲・自分の役割（ログイン時。表示名は `Visibility`・`WorkspaceRole`）のバッジと、選手検索・戦力分析・ペナントの導線。パンくずの起点の「ダッシュボード」17か所をワークスペースの名前に。`404.html` とパスワード変更の2画面の「チーム一覧へ」は「トップへ」（`home`）。`test_templates.py` の `OUTSIDE_LINKS` と「例外の表が古くない」検査は表ごと消した。
- `home`（`/`）: ログイン時は `build_workspace_list_query().list_for(user_id)`（名前順・名前・公開範囲・役割・入口へのリンク）。未ログイン時は紹介・見本のワークスペースへのリンク（`settings.SAMPLE_WORKSPACE_KEY`。環境変数。設定があり・key があり・公開のときだけ）・新規登録とログイン。作成フォームは無い（W4）。`LOGIN_REDIRECT_URL = "home"`。`.env.example` に `SAMPLE_WORKSPACE_KEY=`。
- ペナントの世界の作成: `request.workspace.access.can_create_world()`（持ち主・編集者）に絞り、作成フォームもその人にだけ出す。ほかのログインした人の POST は 403。空の一覧の文言を「世界を作れるのは、このワークスペースの持ち主と編集者です（ログインが必要です）」に。進める・締める・編成・削除は世界の作成者のまま。
- CSRF の失敗（`presentation/csrf.py`）: API かどうかを `request.path.startswith("/api/")` から URL 名（`resolver_match`、無ければ `resolve()`。`api_` で始まる）に替えた。
- `measure_pages`: `_url` が key を足す。docstring・Wiki「仮想データの投入」の「T3 の時点ではほかの key は 404」を消した。
- チームの書き込みの判定（`DjangoTeamPermissionQuery`。staff の特権が残る）は替えていない（T5）。

**計画からのずれ**

- 未ログインの POST をデコレータでログインへ送るので、読むだけの画面（`require_GET`・`require_safe`）への未ログインの POST は 405 ではなく 302 になる（ログインした人は今どおり 405）。既存のテスト2つ（戦力分析・球団史）を「未ログインは 302、ログインした人は 405」に替えた。
- `{% workspace_entry_url %}` を足した（上）。
- 計測の写し: 共有の開発 DB に `epic/postseason` のマイグレーション（`myapp_game.stage` 列ほか）が当たっていたので、写しから `stage` 列を落としてから `migrate` した（epic/workspaces のコードはこの列を知らず、世界の進行が `NOT NULL` で落ちる）。ほかの差（使われない表）は残した。既定のワークスペースの key は写しを作るたびに変わる（`ye8gj0k9h4`）。

**テスト**

- `test_workspace_isolation.py`（中心）: `EveryWorkspaceUrlTest` を足した。分類表 `URL_TABLE`（URL 名 → 引数（`Side` の属性）・種類（画面・書き込み・API）・ログインが要るか）と `workspace_urlpatterns` の名前が一致すること・全パターンの callback が `workspace_view` の包み（`__code__`）で、`login`・`json` が表と同じこと・(1)(5) A の key の下で B の id（チーム・選手・試合・リーグ・世界とその中の id）は 404（API は JSON の 404）・(2)(6)(7) A の画面（未ログイン・A の持ち主）に B の目印が出ず（選手検索を含む）、リンク（`href`・`action`）は `/w/<A の key>/` か `/`・`/accounts/`・`/admin/`・`/static/`・CDN だけ・(3) B を非公開にすると、全パターンで未ログインは `next` つきでログインへ（API は 403）、A の持ち主（B の部外者）は 404 で `request.workspace` が載らない、B の編集者と担当は門を通る（`request.workspace` が B）・公開の B は部外者も読める（空振りの防止）。`CurrentUrlsShowOnlyTheLegacyWorkspaceTest` は `UrlsShowOnlyTheirWorkspaceTest`（B の key では B だけが見える）に、`NoWorkspaceTest` は「中の URL は 404・`/` は開く・管理画面のトップは開く」に替えた。`WorldPagesOfTheOtherWorkspaceTest` は新しい URL のまま残し、作成フォームの検査は B のオーナーを A の編集者にして確かめる。`WorldScopeIsVerifiedAgainstTheLedgerTest` は解決済みのワークスペースを載せた要求で、`workspace_view` を通らない要求は `WorkspaceNotResolved`・B で確かめた世界の id を A の範囲で通さないことを足した。
- `test_workspace_urls.py`（新）: 古い URL は 404・形の合わない key は 404 で `assertNumQueries(0)`・形はドメインの定数から・無い key は 404・`/` の未ログイン（見本のリンクは設定があり、key があり、公開のときだけ）・ログイン時の一覧（名前順・役割と公開範囲・入っていない公開ワークスペースは出ない）と、一覧のクエリ数がワークスペースの数によらない・ログイン後は `/`・非公開の URL からログインして同じ URL に戻る・部外者は 404・リダイレクトが同じ key の下（選手の登録と退団・試合の登録・`analysis_index` の `?team=`・ペナントの進める・締める（GET と不正な POST）・削除）・API の `redirect_url`・`_url` が要求の key を足す・views と API が `_url` の外で `reverse`／名前の `redirect` を使わない（AST）・`404.html` がワークスペースの外でも描ける（`DEBUG=False`。再発防止）と、中の 404 はヘッダーを保つ。
- CSRF の失敗が `/w/<key>/api/...` でも JSON になることは、`test_csrf.py`（`ws_url` 経由で key つきの URL を叩く）が確かめる。
- ほか: `ws_url` が key を足す（`workspace` を省けば既定の `defaultws0`。DB は読まない）、`make_editor()`・`header_pages()` に `/`、`test_scoped_url.py`（key の先頭・外では例外・`workspace_entry_url`）、世界の作成のテストは作る人を編集者に、`game_edit_payload.json` の URL、e2e（既定のワークスペースを作る・世界を作る人を編集者に）。
- 件数: domain＋integration 3,499（T3 の先端より +21）、e2e 13。

**変異の確かめ**（domain＋integration を流し、どれも戻した）

| 変異 | 落ちたテスト |
| --- | --- |
| `workspace_view` が公開範囲を見ない | 3件（81 の部分テスト） |
| `resolve` が key を見ず、id が最小のワークスペースを返す | 14件 |
| `can_edit_teams` が常に真 | 5件（T5 で画面の検査が足される） |
| 確かめた世界を世界の id だけで覚える | 1件 |
| `_w` を通さず `path()` で登録する（試合一覧だけ） | 33件 |
| 世界の作成をログインした人なら誰でもに戻す | 2件 |

**計測（2026-10-24）**: 「W2 の計測の基準」と同じ手順。開発 DB を `backup()` で `/tmp/w2-t4/db.sqlite3` に写し（`integrity_check` ok・試合 3,480。上の `stage` 列を落とした）、T4 のコードで `migrate` → `pennant_create`（2リーグ・世界 1・球団12・選手966人）→ `pennant_advance --to week`（36試合）。写しの上だけで `s-sato`（staff でない）を球団 1 の担当にした。基点 e5aadc4（T3 の先端。使い捨ての worktree）と T4 を交互に3回ずつ、未ログイン・持ち主（`admin`。`--world 1` でペナントも）・担当（`s-sato`）で流した。中央値（応答 ms / SQL ms / クエリ）。写しと使い捨ての worktree は計測後に消した。

| 画面 | 基点・未ログイン | T4・未ログイン | 基点・持ち主 | T4・持ち主 | 基点・担当 | T4・担当 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ダッシュボード | 493/61/11 | 517/64/11 | 517/65/13 | 507/62/13 | 492/62/13 | 517/62/14 |
| チーム一覧 | 10/3/3 | 12/3/3 | 13/3/5 | 13/3/5 | 13/3/5 | 14/3/6 |
| 順位表 | 69/3/5 | 69/3/5 | 71/3/7 | 71/3/7 | 69/3/7 | 72/3/8 |
| 順位表(2026) | 66/3/5 | 67/3/5 | 66/3/7 | 68/3/7 | 66/3/7 | 72/3/8 |
| 選手一覧(野手) | 262/9/28 | 285/10/28 | 259/9/31 | 274/10/31 | 254/9/31 | 281/10/32 |
| 選手一覧(投手) | 249/6/28 | 272/8/28 | 247/6/31 | 267/7/31 | 256/6/31 | 274/7/32 |
| 選手詳細 | 91/2/16 | 93/2/16 | 91/2/19 | 91/3/19 | 93/2/19 | 94/2/20 |
| 選手検索 | 15/1/5 | 13/1/5 | 15/1/7 | 16/1/7 | 15/1/7 | 16/2/8 |
| リーグ詳細 | 106/6/6 | 101/6/6 | 114/6/8 | 109/6/8 | 108/6/8 | 115/6/9 |
| リーグ成績 | 134/5/9 | 140/7/9 | 141/6/11 | 143/6/11 | 139/6/11 | 146/6/12 |
| リーグタイトル | 70/9/8 | 67/9/8 | 68/9/10 | 69/9/10 | 69/9/10 | 71/9/11 |
| 試合一覧 | 102/15/7 | 109/15/7 | 101/14/10 | 104/14/10 | 103/16/10 | 109/15/11 |
| 試合詳細 | 506/33/18 | 511/32/18 | 492/32/21 | 488/32/21 | 504/34/21 | 513/34/22 |
| 戦力分析 | 38/6/9 | 35/6/9 | 36/6/11 | 40/6/11 | 39/6/11 | 40/6/12 |
| 戦力分析（起用マップ） | 28/6/9 | 28/6/9 | 26/5/11 | 28/5/11 | 29/6/11 | 30/6/12 |
| 戦力分析（FA取得） | 43/11/14 | 44/10/14 | 43/10/16 | 42/9/16 | 43/11/16 | 46/11/17 |

ペナント（持ち主 `admin`。基点 → T4）: ペナント一覧 25/3/16 → 26/3/16、世界ダッシュボード 151/4/53 → 158/4/53、世界の順位表 11/0/11 → 11/0/11、世界のリーグ詳細 14/1/12 → 14/1/12、世界のリーグ成績 122/0/17 → 121/0/17、世界のチーム一覧 9/1/9 → 9/1/9、世界の選手一覧 122/2/45 → 118/2/45、世界の試合一覧 14/1/13 → 14/1/13、世界の戦力分析 36/1/15 → 36/1/15、世界の球団ホーム 63/4/30 → 63/4/30。

- **完了条件を満たす**: 未ログインと持ち主は全画面で基準と同じクエリ数（一時関数の1本が key の1本に替わっただけ。持ち主は編集者の有無を同じ1本の注釈で読む）。担当は全画面で +1（担当チームの id の2本目）。ペナントの10画面も同じ。
- 応答時間は揺れの範囲（選手一覧が未ログインで +9% ほどだが、クエリ数も SQL 時間も同じで、持ち主・担当でも同程度。共有コンテナの揺れの幅）。
- `/`（同じ写しで、使い捨てのユーザーとワークスペースを足して）: 未ログイン 0 クエリ（見本の設定なし。設定すれば key を引く1本）。ログインしてワークスペース1つのユーザー・4つ（持ち主3・編集者1）のユーザーとも**3クエリ**（セッション・利用者・一覧1本）、応答 3ms。数に比例しない。

**レビュー（`ddd-boundary-reviewer`）**: 重要な指摘なし。軽微3件: (1) 未ログインの人には、無い key（404）とある非公開の key（ログインへ）で応答が分かれる → 計画どおり（ログインへ送る便利さを取る。key は推測しにくい）として Wiki「アーキテクチャ」に明記した。(2) `DjangoTeamPermissionQuery` は `is_staff` に範囲のどのチームの書き込みも許すので、URL にワークスペースが入った今、公開ワークスペースにメンバーでない運営者が書き込める（閲覧の判定と書き込みの判定が食い違う）→ T5 で直す。**T5 が入るまで epic を main に入れない**（**W2 T5 で解消した**。下の T5 の記録）。(3) `myapp/urls.py` の docstring の検査の参照先 → 直した。

**T5 に回すもの**: チームの書き込みの判定（`_requires_team_permission`・`can_edit_team` などのフラグ・`game_create`・`game_edit`・`player_edit`・API）を `request.workspace.access` へ替え、`DjangoTeamPermissionQuery`・`build_permission_query` を消す（上の (2) の食い違いを閉じる）。権限の真理値表の画面の検査（`test_workspace_permissions.py`）・他所の id を混ぜる経路（`test_cross_workspace_ids.py`）・全 POST を B の key に送る検査。Wiki「ワークスペースと共有」の新設。

#### main の取り込み（2026-10-24 その3）

`refactor/merge-main-into-workspaces-4` で、epic 6847e7f（W2 T4 の後）に main 02bcf3a を取り込み（マージコミット c8e8373）、続けて main 5dcc088（#300）を2つ目のマージコミットで取り込んだ。T5 の前。

- **main から来たもの**: #219 段階5（PR #288。`TeamApplicationService` の残りを `LeagueViewService`（`application/league_view.py`）に改名し、組み立て口を `build_league_view_service()`・`build_world_league_view_service(world_id)` に。管理画面の概況を `AdminOverviewService`・`build_admin_overview_service()` に分けた）と、ペナントのポストシーズン #39（PR #294。試合の区分 `Game.stage` と、試合を読む入口の必須の引数 `StageFilter`（`games_in(scope, *, stages)`・`game_rows_condition`・`batting_totals(within, *, stages)` など）、日本一の記録 `PennantChampionship`、世界のポストシーズンの方式 `PennantWorld.postseason_format`、サービス `PennantPostseasonService`・`PennantPostseasonViewService`・`PennantSettingsService`・`PostseasonBracketReader`・`PostseasonHistoryReader`、画面 `pennant_postseason`・`pennant_postseason_by_year`・`pennant_postseason_stats`・`pennant_settings`、`pennant_create --postseason`・`pennant_advance --to postseason-end`。マイグレーション `0055_game_stage`・`0056_pennantworld_postseason_format`・`0057_pennant_championship`）。
- **振り直し**: main が 0055〜0057（#294）と 0058（#300 の `0058_pennantworld_draft_order_rule`）を使ったので、epic のマイグレーションを `0059_workspace`（依存は `0058_pennantworld_draft_order_rule`）・`0060_backfill_default_workspace`・`0061_workspace_constraints` に移した（1つ目のマージでは 0058〜0060 に付け直し、#300 を取り込むときに1つずつ後ろへずらした）。依存・docstring・コメント（`admin.py` の編集者の行・`test_jersey_number.py`・`test_migration_draft_year.py`・`test_workspace_constraints.py`）・`test_workspace_migration.py` の BEFORE/AFTER をそろえた。空の DB からの `migrate` と `makemigrations --check` を確かめた。
- 衝突は43ファイル（views 29か所・`test_wiring.py` 13か所ほか。Wiki 3ページ・`.claude/rules/tests.md`・`ddd-boundary-reviewer.md` を含む）。どれも main の改名・ポストシーズンと epic の範囲の形を両方残した。
- **#300（#198 D1。ドラフトを指名順の規則つきの会議に）の追加の取り込み**: `PennantWorld.draft_order_rule`（`DraftOrderRule`）・ドラフト会議の domain（`draft_meeting`・`draft_order`）・締める処理と勝ち上がりが共有する最終順位の読み取り `LeagueFinishReader`・設定の画面の指名順のフォームと書き込みの URL `pennant_draft_order_settings`・`pennant_create --draft-order`・`simulate_sample --draft-order`。新しい `Django*` は無い（`DjangoOffseasonQuery` の列と `DjangoWorldRepository` の列が増えただけ）。衝突は8ファイル（views・`test_wiring.py`・`repositories.py`・`pennant_world.py`・`simulate_sample.py`・`multi_season_check.py`・domain の `test_pennant_world.py`・Wiki「ペナントモード」）。
  - `_league_finishes(scope)` は範囲を受け取るペナント専用の内側の読み取りにし（`build_league_view_service(scope)` と `DjangoGameListQuery(scope)`）、`build_pennant_offseason_service(scope)` と `_postseason_brackets(scope, *, finishes)` が同じ1つを共有する（main の形のまま）。
  - `pennant_draft_order_settings` は `_world_context(request, …)`・`_world_scope(request, …)`・`_url(request, …)` を通し、設定の画面のフォームの `action` は `{% workspace_url %}`（`settings.html` に `{% load scoped %}` を足した）。`URL_TABLE` に書き込み（`WRITE`）として足した。
  - 世界の保存（epic の「台帳の行だけを `update`」の形）に `draft_order_rule` を足した。`run_seasons`（`simulate_sample --from-real-leagues --seasons`）は epic の `scope` と main の `draft_order` を両方受け取る。
  - `test_pennant_draft_order.py` を範囲の形（`world_scope`・`world_ledger`・`ws_url`）にし、オーナーを編集者に、`pennant_create` に `--workspace` を渡した。`test_wiring.py` の main の追加（`_finishes` の範囲）は `WORLD7` で書いた。
  - 計測はやり直していない（#300 は世界の作成・設定・締める処理の変更で、`measure_pages` の画面の読み取りは変えない）。
- **組み立て口**（23個。`test_wiring.py` の分類）:
  - `build_league_view_service(scope)`（両用。`BOTH_BUILDERS`）。main の `build_world_league_view_service(world_id)` は T1 と同じやり方でここに吸収した（年齢の基準日は `_today_for(scope)`）。`_scope`・`_league_view_of`・`_game_scope` は `request` を受け取り、`_real_scope(request)`・`_world_scope(request, …)` で範囲を作る（main の `_game_scope` がリーグの参照を返さなくなった形に合わせ、試合一覧は `_league_view_of(request, world)` で別に組み立てる）。
  - **`AdminOverviewService`・`build_admin_overview_service()`・`application/admin_overview.py`・`test_wiring.py` のその分類と検査・`test_admin.py` の概況の検査4件を消し**、W2 T2 の運営向けの概況（`admin.py` の `operator_overview`）を残した（計画の「衝突」の表のとおり）。`admin_real_queryset` の例外は広げていない（main の `_REAL_QUERYSETS` の試合の `ALL_STAGES` は、`admin_real_queryset(Game)` が区分で絞らないのと同じ）。
  - `build_pennant_postseason_service(scope)`・`build_pennant_postseason_view_service(scope)`・`build_pennant_settings_service(scope)` はペナント専用（`PENNANT_ONLY_BUILDERS`。実データの範囲なら `InvalidWorld`）。世界の台帳は `DjangoWorldRepository(scope.workspace_id)`。内側の読み取り `_postseason_brackets(scope)`・`_postseason_history(scope)` も範囲を受け取り、勝ち上がりの順位は `build_league_view_service(scope)`。`PennantHomeService`・`PennantHistoryService` の `league_view` と、締める処理の `titles` も範囲で組み立てる。`build_pennant_world_view(workspace_id)` の `postseason_status_for` は `WorldScope.pennant(workspace_id, 世界)`（`standings_for` と同じ形）。
  - `test_wiring.py`: 分類に3つを足し、`LedgersAreScopedToTheWorkspaceTest` の「台帳を持つ組み立て口」に3つを足して、GM ホーム・歴代の `_postseason._worlds` が範囲のワークスペースの台帳であることを足した。main の検査の `build_*(7)`・`WorldScope.pennant(7)` を `WORLD7` に、「世界の id が正しくない」検査を「実データの範囲は `InvalidWorld`」に替えた。`build_world_league_view_service`・`build_service`・`build_admin_overview_service` が views に無いことも固定した。
- **views・テンプレート・URL**: ポストシーズンの URL は `_pennant()` 経由なので `/w/<key>/pennant/<世界>/postseason/…`・`…/settings/` に入り、`workspace_view` で包まれる。新しいビュー（`pennant_postseason`・`pennant_postseason_stats`・`pennant_settings`・`_advance_postseason`・GM ホームのポストシーズンのカードと締めるカード・編成の「戦力外」タブ）は `_world_context(request, …)`・`_world_scope(request, …)` で範囲を作り、`reverse`・`redirect("名前")` は `_url(request, …)` に替えた。テンプレートの素の `{% url 'pennant_…' %}` 18か所（`_postseason_card`・`_postseason_tabs`・`postseason_stats`・`history`・`team_history`・`home`・`_world_bar`・`_finale`・`game_detail`）を `{% workspace_url %}` に替え、`{% load scoped %}` の無かった部品2つに足した。設定の変更（POST）の権限は既存の世界の書き込みと同じ世界の作成者（`_requires_world_owner`）で、未ログインの POST は `workspace_view` がログインへ送る。
- **管理コマンド**: `pennant_advance` のポストシーズンの進行と案内は `world_scope(--world)`（世界の行からワークスペースを導く）を1回作って渡す。`pennant_create --postseason` は epic の `--workspace` とそのまま並ぶ。
- **ORM の読み書き**: main の新しい `Django*` は main の時点で `WorldScope` 必須（`championships_in`・`games_in(…, stages=…)`・`stints_in`・`teams_in`・`game_rows_in_world` を通る）だったので、実装は変えていない。世界の行の保存（`DjangoWorldRepository.save`）は epic の「台帳の行だけを `update`」の形に `postseason_format` を足し、読み戻しも足した。
- テスト:
  - `test_workspace_isolation.py`: `CHECKS` に `DjangoChampionshipRepository`・`DjangoChampionshipQuery`・`DjangoPostseasonQuery`・`DjangoPostseasonStatsQuery` を足した。下ごしらえに B の世界の `B_YEAR` の終わったポストシーズン（`_plant_postseason`。公式戦1つと、1位のホームで3連勝の CS ファイナルステージ。最初の試合に B の世界の選手の打撃つき。B の世界の範囲で保存してから区分を替える）と、同じ年の日本一の記録を置き、A の実データ・A の世界の範囲から読めない（年・試合・結果・最後の公式戦の日・個人成績・日本一）、A の世界の範囲で B の世界の球団の日本一を書けない（`InvalidChampionship`）、B の同じ年の記録が A の「1世界1年に1つ」の検査に数えられない、B の世界の範囲では読める（空振りの防止）ことを確かめる。`URL_TABLE` にポストシーズンの4本を足した（`EveryWorkspaceUrlTest` は A の世界にも `YEAR` のポストシーズンを置き、B 側の世界の年を `B_YEAR` にして、年を指す2本も A で描き、B の id では門で 404 になることを確かめる）。台帳の要約の `played_postseason` が A では偽（B に引きずられない）・B では真であることも確かめる。ドメインの `Game` が `stage` を必須にしたので、試合を作る2か所に足した。
  - `test_world_isolation.py` の分類表に `PennantChampionship` を「League までの道」の形（`"team__league"`）で足した。世界の削除の順（`_WORLD_ROWS_CHILD_FIRST`）は main が `"team__league"` で入れていて、epic の形と同じ。
  - main の新しいテスト（`test_pennant_postseason.py`・`test_pennant_championships.py`・`test_pennant_postseason_screens.py`・`test_pennant_postseason_settings.py`・`test_postseason_isolation.py`・`world_case.py` の `PostseasonWorldCase`）の `WorldScope.real()`／`WorldScope.pennant(id)`・`build_*()`／`build_*(world_id)`・`DjangoWorldRepository()`・`reverse(…)`・リーグと球場の直接作成を、`real_scope()`・`world_scope(id)`・`world_ledger()`・`ws_url(…)`・`make_league`・`make_stadium` に替えた。画面から世界を作るテストは作る人を編集者にした（W2 T4）。`test_postseason_isolation.py` の世界バーのリンクの正規表現は `/w/<key>/` を含む形に。`test_admin_workspaces.py` の管理画面の試合のフォームは、main が足した必須の `stage` を渡す（管理画面のフィールドセットには出ない）。
- **CLAUDE.md**: main の #288 が CLAUDE.md の「サービスは対象ごとに分けてある」に `AdminOverviewService` を挙げているが、この取り込みでは消えている。CLAUDE.md はこの取り込みでは変えず、T6 の CLAUDE.md の差分（ユーザーに見せてから直す）に含める。`ddd-boundary-reviewer.md` の同じ箇所は直した。
- **レビュー**（`ddd-boundary-reviewer` とコードレビューの2つ。どちらも重要な指摘なし）。軽微な指摘を直した:
  - `_plant_b_postseason` が日本一の記録を `YEAR`、ポストシーズンの試合を `B_YEAR` に置いていて、「記録があればその年のポストシーズンの試合がある」（`pennant_championships.club_results` はずれると `InvalidPostseason`）と合っていなかった。記録を `B_YEAR` にそろえ、試合も公式戦と勝ち上がりに見合う形（`_plant_postseason`）にした。`_championship_repository` の検査は、B の記録の無い年に書いて「範囲の外の球団」で止まること・B の行が変わらないこと・B と同じ年（`B_YEAR`）の A の記録が書けることに直した。
  - URL の検査で、B 側の世界の年が `YEAR` だったため、年を指すポストシーズンの2本（`pennant_postseason_by_year`・`pennant_postseason_stats`）の「A の key の下で B の id は 404」が、門が無くても「その年のポストシーズンが無い」で 404 になり、門の検査になっていなかった。また `ABSENT_IN_FIXTURE` にあり、A の画面としても描いていなかった。B 側の世界の年を `B_YEAR` に、A の世界に `YEAR` のポストシーズンを置いて `ABSENT_IN_FIXTURE` から外し、2本が A で 200・B の id で 404・B の key では同じ引数で 200（目印が出る）であることを足した。
  - 台帳の要約（`_world_summary_query`）で main が足した `played_postseason` を検査していなかった。A の台帳の A の世界は偽（`list_all`・`get` の両方）、B の台帳の B の世界は真であることを足した。
  - `ddd-boundary-reviewer.md` の `build_service(real_scope())` を `build_league_view_service(real_scope())` に。`.claude/`・`docs/wiki/` に消えた名前（`build_service`・`build_world_league_view_service`・`build_admin_overview_service`・`AdminOverviewService`・`TeamApplicationService`）が「今あるもの」として残っていないことを確かめた（残るのは「当時の」「#219 で分けた」と断った歴史の記述だけ）。Wiki「ペナントモードの仕組み」の `TeamApplicationService` の1行を過去の形に直した。
  - `test_wiring.py`: `build_pennant_world_view` の `postseason_status_for` が世界ごとの範囲（`WORLD7`・`WORLD8`）で組み立てることを、`standings_for`・`games_for` と同じ検査に足した（組み立て口を差し替えて渡る範囲を捕まえる。DB を読まない）。
  - 変異で確かめた: ポストシーズンの2画面のビューから `_world_context`・`_world_scope` の門を外す（世界の行のワークスペースで見出しと範囲を作る）と、`test_the_other_workspaces_ids_are_404_under_a`（年を指す2本と `pennant_postseason`）と新しい検査が落ちる。台帳の要約の `played_postseason` をどの世界のポストシーズンの試合でも真にすると、`DjangoWorldSummaryQuery` の検査が落ちる。どちらも戻した。

#### T5 権限を `WorkspaceAccess` で判定する（`feature/workspace-permissions`、2026-10-25）

サイトのチームの書き込みの判定を `request.workspace.access`（`WorkspaceAccess`）だけにし、**`is_staff`・superuser の特権を廃止した**。T4 のレビューの (2)（公開ワークスペースにメンバーでない運営者が書き込める。閲覧の判定と書き込みの判定の食い違い）を閉じたので、「T5 が入るまで epic を main に入れない」は解消した。マイグレーションは無い。

- views（`presentation/views.py`）: `_can_edit_teams(request, team_ids)`（`_workspace(request).access.can_edit_teams(team_ids)`。DB を読まない）を足し、`_requires_team_permission`・選手一覧と選手詳細の `can_edit_team`・試合一覧の `can_create_game`・試合詳細の `can_edit`・`game_create`・`game_edit`・`player_edit` をここへ通した。API（`api.py`）の判定も同じ。
  - `game_create`: 編集できるチームが1つも無い人（どのチームの担当でもない部外者）は、GET も POST も 403（押しても弾かれる入力画面を見せない）。チームが1つも無いワークスペースでは、持ち主・編集者でも一覧へ戻す（`can_edit_teams([])` は真になるが、対戦カードを選べない画面になる）。
  - `can_create_game` も同じ2つの条件（編集できるチームがある・チームが1つ以上ある）。レビューの指摘で `game_create` を一覧の導線の条件にそろえた。
  - **スコアブックの保存は、書き換えた後の対戦カードでも判定する**（計画に無い。前は保存前の試合の2チームだけで判定していたので、片方の担当者が対戦カードを担当外の2チームに替えられた。試合の登録と同じ規則にそろえた。403 の JSON）。
- `DjangoTeamPermissionQuery`・`build_permission_query` を消した（計画の「その Protocol」は、application に置いていなかったので消すものが無かった）。`test_wiring.py` の分類（`REAL_ONLY_BUILDERS`）と `test_workspace_isolation.py` の `CHECKS` から外し、`TeamPermissionQueryIsGoneTest` が名前（`DjangoTeamPermissionQuery`・`build_permission_query`・`TeamPermissionQuery`）がテスト以外の `myapp/` に無いことと、`presentation/` の `.py` が `.is_staff`・`.is_superuser` を読まないことを固定する。
- テンプレート: 書き込みの導線はすでにビューのフラグ（`can_edit_team`・`can_edit`・`can_create_game`・世界の `is_owner`・作成フォームの有無）で囲まれていて、`user.is_authenticated` で囲んだ書き込みの導線は無かった（`base.html` の `user.is_authenticated` はユーザー欄と役割のバッジ、`user.is_staff` は管理画面のリンクで、どちらも残した）。フラグの出どころが `WorkspaceAccess` に替わっただけで、テンプレートは変えていない。`.claude/rules/templates.md` の規則を「ビューが渡す権限のフラグで囲む」に直した。
- `orm_models.Team.managers` のコメントを今の規則に直した。`help_text`（「管理ユーザーは常に編集できます。」）は古くなったが、変えるとマイグレーションが要るので直していない（T6 以降に回す）。
- ペナントの世界の書き込み（進める・締める・編成・設定・削除）は今どおり世界の作成者（`_requires_world_owner`）。docstring の「管理ユーザーでも」を「ワークスペースの持ち主・編集者でも」に。

**計画からのずれ**

- 計画の「サイトの書き込みに staff を使っていた約10ファイル」は、T3・T4 の書き換えで担当（`login_as_manager`）・編集者（`make_editor`）に替わっていて、残っていた staff・superuser は管理画面のテストと「権限が無いこと」を確かめるテストだけだった（T5 の判定の切り替えでフルテストは1件も落ちなかった）。「権限が無いこと」を superuser で確かめていたペナントのテスト7件（`test_pennant_home.py` 2・`test_pennant_worlds.py`・`test_pennant_offseason_screens.py`・`test_pennant_screens.py` 2・`test_club_screen.py`）は、T5 の後は superuser に権限が無いので空振りになる。**ワークスペースの編集者（実データなら全チームを書ける人）でも、世界の作成者でなければ書けない**ことを確かめる形に替えた。`test_world_isolation.py` の「世界の id は 404」の検査も、superuser から編集者に替えた（「権限が無い」ではなく「見つからない」まで進める）。e2e は staff を使っていなかった（担当と編集者）ので変えていない。
- スコアブックの対戦カードの判定（上）を足した。
- 変異「`_requires_team_permission` が staff を通す」は、判定の唯一の入口 `_can_edit_teams` に当てた（`_requires_team_permission` だけに当てると、導線のフラグと API が漏れる）。

**テスト**

- `test_workspace_permissions.py`（新）: 役割8つ（持ち主・編集者・担当・ほかのチームの担当・ログインした部外者・未ログイン・staff の部外者・superuser の部外者）× 13項目（選手一覧・選手詳細・試合一覧・試合詳細の導線、ペナントの作成フォーム、試合の登録・試合の編集・選手の編集の GET、選手の登録・選手の編集・試合の登録・スコアブックの保存・ペナントの作成の POST）の真理値表を、公開のワークスペースと非公開のワークスペースで1つずつ持つ。POST は DB の変化と合わせて判定する（ok なら保存された、403・ログインへ・フォームのエラーなら何も変わらない）。非公開ではメンバーの行は公開と同じ、部外者（staff・superuser を含む）はすべて 404、未ログインはすべてログインへ（API は 403）。ほかのワークスペースの担当・編集者・持ち主は、このワークスペースでは部外者（担当チームは解決したワークスペースのものだけを数える）。
- `test_cross_workspace_ids.py`（新）: A の持ち主が A の key の下で B の id を混ぜて送る。試合の登録の `home_team`・`away_team`（片方・両方）はフォームのエラー（200）、スコアブックの B の試合は 404、A の試合の対戦カードを B のチームに・打順に B の選手・打順の行のチームが B は 400、B のチームへの選手の登録は 404、選手の編集画面の7つのボタン（編集・退団・主将の指名と解任・支配下登録・FA 宣言とその取り消し）を B のチームと B の選手・A のチームと B の選手・B のチームと A の選手で送るとどれも 404、戦力分析の `?team=<B>` は A の先頭の球団へ落ちる。どれも **アプリの全表の行が変わらない**（`snapshot_all_rows()`）。止まらない経路（500 や保存）は見つからなかった。
- `test_workspace_isolation.py`（拡張）: `EveryWorkspaceUrlTest` に、書き込みを受け付けるパターンの本文の表 `WRITE_BODIES`（実際に書ける本文。空の本文の検証落ちで空振りしない）を足し、(a) 種類が書き込み・API のパターンはどれも本文を持つ、(b) A の持ち主（公開の B では閲覧者）が B の key の下の**全パターン**へ POST すると、書き込みは 403・読むだけの画面は 200／405 で、全表の行が変わらない、(c) A の key の下で B の id を送ると、id を URL に持つ書き込みは 404（読むだけの画面は 405 も）、本文だけに持つもの（試合の登録・世界の作成）はフォームのエラーで、全表の行が変わらない、(d) 空振りの防止: 同じ本文を A の持ち主が A の id で送ると、選手の登録・試合の登録・スコアブックの保存・世界の作成は書ける。
- `test_permissions.py`: `test_staff_can_edit_any_team` を `test_staff_outside_the_workspace_cannot_edit`（staff・superuser の部外者は選手の編集・試合の編集が 403 で、登録の導線も出ない）と `test_an_editor_of_the_workspace_can_edit_any_team` に替えた。スコアブックで対戦カードを担当外の2チームに替えると 403 で試合が変わらないこと（再発防止）と、担当のチームが残る対戦カードなら保存できること（空振りの防止）を足した。チームの無いワークスペースでは、持ち主にも試合の登録の導線を出さず画面は一覧へ戻し、部外者は 403 であること（`EmptyWorkspaceGameCreateTest`）。
- `test_world_isolation.py` の `PermissionTest`: `build_permission_query` の検査を、画面の振る舞い（編集者がペナントの球団に選手を登録しようとすると 404・ペナントの球団の行の担当者は実データでは閲覧者で、ペナントの球団は 404・実データの球団は 403）に替えた。
- 件数: domain＋integration 3,814、e2e 13。

**変異の確かめ**（domain＋integration を流し、どれも戻した）

| 変異 | 落ちたテスト |
| --- | --- |
| `can_edit_teams` が常に真（担当がほかのチームも書ける） | 24件（86 の部分テスト） |
| `_can_edit_teams`（`_requires_team_permission`・導線のフラグ・API の唯一の入口）が staff・superuser を通す | 3件（5 の部分テスト。権限の真理値表・`test_staff_outside_the_workspace_cannot_edit`・`test_views_do_not_read_is_staff`） |
| 担当チームの判定が、ほかのワークスペース（とペナントの世界の球団）の担当を数える（`DjangoWorkspaceAccessQuery` の2本目を `Team.objects` に） | 6件（8 の部分テスト） |


**計測（2026-10-25）**: 「W2 の計測の基準」と同じ手順。開発 DB を `backup()` で `/tmp/w2-t5/db.sqlite3` に写し（`integrity_check` ok・試合 3,480。共有の開発 DB は main の 0058 まで当たっていて、epic の 0059〜0061 はそのまま `migrate` できた。T4 の `stage` 列の手当ては要らなかった）、T5 のコードで `migrate` → `pennant_create`（2リーグ・世界 1・球団12・選手966人）→ `pennant_advance --to week`（36試合）。写しの上だけで `s-sato`（staff でない）を球団 1 の担当にした。既定のワークスペースの key は `q5pab3hlrh`。基点 e113c47（epic の先端＝T4 と main の取り込みの後。使い捨ての worktree）と T5 を交互に3回ずつ、未ログイン・持ち主（`admin`。`--world 1` でペナントも）・担当（`s-sato`）で流した。中央値（応答 ms / SQL ms / クエリ）。写しと使い捨ての worktree は計測後に消した。

| 画面 | 基点・未ログイン | T5・未ログイン | 基点・持ち主 | T5・持ち主 | 基点・担当 | T5・担当 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ダッシュボード | 487/63/11 | 489/61/11 | 501/61/13 | 493/61/13 | 491/60/14 | 508/63/14 |
| チーム一覧 | 12/3/3 | 12/3/3 | 12/3/5 | 13/3/5 | 14/3/6 | 14/3/6 |
| 順位表 | 72/3/5 | 71/3/5 | 75/3/7 | 71/3/7 | 78/3/8 | 74/3/8 |
| 順位表(2026) | 69/3/5 | 68/3/5 | 70/3/7 | 69/3/7 | 73/3/8 | 73/3/8 |
| 選手一覧(野手) | 265/12/28 | 270/12/28 | 275/13/31 | 269/12/30 | 267/13/32 | 270/13/31 |
| 選手一覧(投手) | 262/11/28 | 265/11/28 | 270/11/31 | 253/11/30 | 263/11/32 | 260/11/31 |
| 選手詳細 | 87/4/16 | 87/4/16 | 90/4/19 | 95/4/18 | 89/4/20 | 94/4/19 |
| 選手検索 | 14/1/5 | 13/1/5 | 14/1/7 | 15/1/7 | 15/1/8 | 15/1/8 |
| リーグ詳細 | 109/6/6 | 104/6/6 | 108/6/8 | 112/6/8 | 109/6/9 | 109/6/9 |
| リーグ成績 | 147/7/9 | 143/8/9 | 142/8/11 | 151/10/11 | 143/7/12 | 143/7/12 |
| リーグタイトル | 70/9/8 | 65/8/8 | 66/9/10 | 69/9/10 | 70/9/11 | 68/9/11 |
| 試合一覧 | 102/15/7 | 100/14/7 | 104/14/10 | 101/14/9 | 100/14/11 | 102/14/10 |
| 試合詳細 | 497/36/18 | 495/38/18 | 495/37/21 | 499/38/20 | 503/37/22 | 500/38/21 |
| 戦力分析 | 35/6/9 | 36/6/9 | 38/6/11 | 37/6/11 | 38/6/12 | 40/6/12 |
| 戦力分析（起用マップ） | 26/5/9 | 27/5/9 | 28/5/11 | 28/5/11 | 28/5/12 | 31/6/12 |
| 戦力分析（FA取得） | 42/10/14 | 41/10/14 | 41/10/16 | 42/10/16 | 42/10/17 | 45/11/17 |

ペナント（持ち主 `admin`。基点 → T5）: ペナント一覧 27/3/16 → 30/3/16、世界ダッシュボード 154/4/53 → 158/4/53、世界の順位表 12/0/11 → 12/0/11、世界のリーグ詳細 15/1/12 → 14/1/12、世界のリーグ成績 115/0/17 → 116/0/17、世界のチーム一覧 8/1/9 → 9/1/9、世界の選手一覧 115/2/45 → 115/2/45、世界の試合一覧 15/1/13 → 16/1/13、世界の戦力分析 39/1/15 → 35/1/15、世界の球団ホーム 62/4/29 → 61/4/29。

- **見込みどおり**: 未ログインは全画面で基点と同じクエリ数。持ち主は判定のある4画面（選手一覧の2つ・選手詳細・試合一覧・試合詳細）で −1（`DjangoTeamPermissionQuery` の1本が消えた）、ほかの画面とペナントの10画面は同じ。担当も判定のある画面で −1（T4 の +1 と打ち消し合い、W2 の基準＝T3 の先端と同じ）、判定の無い画面は基準から +1 のまま（担当チームの id の2本目）。
- 応答時間は揺れの範囲（同じ画面で ±10% 以内。SQL 時間も同じ）。


#### T6 記録と規則（`docs/workspace-w2-records`、2026-10-25）

- CLAUDE.md（2026-10-25 にユーザーが承認した差分のとおり）: 範囲の規則の例外を3つ（`admin_real_queryset`・ワークスペースの台帳・`real_teams_of_outer_workspace()`）と、ワークスペースは URL の key から `workspace_view` が解決することを書いた。組み立て口の例外に `build_workspace_access_query()`・`build_workspace_list_query()` を足し、サービスの一覧から `AdminOverviewService` を消した。新しい項目「サイトの権限は `WorkspaceAccess` だけで判定する」（`is_staff`・`is_superuser` は管理画面に入る条件だけ。`WorkspaceAccess` はチームがこのワークスペースのものかは見ないので、新しい書き込みの経路は id を範囲で絞って引く）。UI の「書き込みの導線」と「読み書きが同じ URL に同居する画面」を権限のフラグの形に。
- CLAUDE.md が「例外3つの使ってよい場所を `test_wiring.py` が検査する」と書くので、検査の無かったワークスペースの台帳に `WorkspaceLedgerUsersTest`（`build_workspace_access_query`・`build_workspace_list_query`・`DjangoWorkspaceAccessQuery`・`DjangoWorkspaceListQuery` の名前を出すのが、テストを除く `myapp/` で `presentation/views.py` と定義の `infrastructure/queries.py` だけ）を足した。`admin_real_queryset`（`AdminOnlyScopeExceptionTest`）・`real_teams_of_outer_workspace`（`OuterWorkspaceTeamsTest`）は前からある。
- `Team.managers` の `help_text` を W2 の後の事実（担当はこのチームの選手・試合を編集できる。持ち主・編集者は全チームを編集できる。管理画面に入れることはサイトの権限に何も足さない）に直し、マイグレーション `0062_team_managers_help_text`（`AlterField` だけ）を足した。origin/main の最新は `0058_pennantworld_draft_order_rule` で、0059 以降は main に無い（振り直しは要らない）。「管理ユーザーがサイトで全権を持つ」旨の古い文言を `myapp/`・`docs/wiki/`・`.claude/`・README で探し、残っていたのはこの `help_text` と、それを断るコメント（消した）だけだった。
- `.claude/agents/ddd-boundary-reviewer.md` に「7. 権限」（判定は `WorkspaceAccess` だけ・材料は `workspace_view` が1回読む・`is_staff` をサイトの判定に使わない・新しい書き込みの経路は id を範囲で絞る・導線はフラグで囲む）を足し、6. の範囲の例外を3つに。`.claude/rules/templates.md` は T5 で直してあった。
- ROADMAP: フェーズ9 の W2 を ✅、フェーズ5 の「権限管理（担当者制）」に管理ユーザーの全権の置き換えを追記。W6 の行を「権限をワークスペースに合わせる」に（URL の移動は W2 で済んだ）。
- この設計書: 3.1 の `scoped_url`・`workspace_url` の数と組み立て口の数（22個）、6. の URL 表（ペナントを W2 で移した）、9. の W2・W6 の行、W1b のまとめの「W2 以降に残したもの」の消し込み、「epic を main に入れるときに必ず行うこと」に W2 の分を足した。
- `WorkspaceLedgerUsersTest` は、`admin.py` に台帳の名前を1つ足すと落ちることを確かめた（戻した）。
- 件数: domain＋integration 3,815（T5 より +1）。e2e は画面を変えないので流していない。

#### W2 のまとめ（T6、2026-10-25）

**何をしたか**: ワークスペースを URL（`/w/<key>/`）で選び、誰が何をできるかをワークスペースの役割（持ち主・編集者・チーム担当・閲覧）で判定するようにした。サイトの `is_staff`・superuser の特権を廃止した。ペナントの画面も `/w/<key>/pennant/` に移した。

| 段階 | PR | 内容 |
| --- | --- | --- |
| T0 | #290 | main（#219 段階4・#204 段階B0・B）の取り込み。FA 宣言の読み取りをワークスペースで絞った（`declarations_in`） |
| T1 | #289 | `WorkspaceRole`・`WorkspaceAccess`（domain）と、それを読む台帳 `DjangoWorkspaceAccessQuery`・`DjangoWorkspaceListQuery`。画面はまだ使わない |
| T2 | #291 | 管理画面のトップの概況を、全ワークスペースを数える運営向けの概況に |
| T3 | #293 | URL の組み立てを補助（`workspace_url`・`_url`・`ws_url`）に寄せた（URL も振る舞いも変えない）。`measure_pages` に `--workspace`・`--as`・`--world` |
| T4 | #299 | 中の画面43本を `/w/<key>/` の下へ（古い URL は 404）。`workspace_view` が key から解決し、公開範囲とログインを判定する。`/` の一覧。世界の作成を持ち主・編集者に。一時関数 `legacy_workspace_id` を消した |
| — | #301 | main（#288・#294・#300）の取り込み。マイグレーションを 0059〜0061 に振り直した。`AdminOverviewService` を消して運営向けの概況を残した |
| T5 | #304 | チームの書き込みの判定を `WorkspaceAccess` だけに。`DjangoTeamPermissionQuery`・`build_permission_query` を消した。スコアブックは書き換えた後の対戦カードでも判定する |
| T6 | この PR | 記録と CLAUDE.md・`ddd-boundary-reviewer.md`・ROADMAP、`Team.managers` の `help_text`（0062） |

**決定**（#78 のコメント）: 2026-10-24 の「ペナントの画面も W2 で `/w/<key>/pennant/` に移し、世界の作成を持ち主・編集者に絞る」（推奨 A。2026-10-07 の Q3 を改めた。進める・締める・編成・削除は世界の作成者のまま）と「推奨で進めるもの」5点（T3 と T4 を分ける・`measure_pages` の `--as`・`--world`・見本は `SAMPLE_WORKSPACE_KEY`・Wiki「ワークスペースと共有」を W2 で・チームごとの判定はビューに残す）、2026-10-25 の CLAUDE.md の差分（とその追加の1文）。

**計測**（W2 の基準＝T3 の先端と比べたクエリ数。応答時間はどの段階も揺れの範囲）

| 段階 | 未ログイン | 持ち主・編集者 | 担当 |
| --- | --- | --- | --- |
| T4 | 全画面で同じ | 全画面で同じ | 全画面で +1（担当チームの id の2本目） |
| T5 | 全画面で同じ | 判定のある4画面で −1、ほかは同じ | 判定のある画面は基準と同じ、ほかは +1 のまま |

ペナントの10画面（持ち主）はどの段階も同じ。`/` はワークスペースの数によらず3クエリ。

**漏れと権限を拾えることの確かめ**（変異。どれも戻した）: T1 は `resolve` が key を見ない 16件・担当を範囲で絞らない 7件・`can_edit_teams` が常に真 24件・一覧が公開ワークスペースも出す 7件。T2 は数える範囲・上位の並び・上位の相関で 5・2・2件。T3 は素の `{% url %}` に戻すと場所つきで落ちる。T4 は公開範囲を見ない 3件・`resolve` が最小の id 14件・確かめた世界を id だけで覚える 1件・`_w` を通さない 33件・世界の作成を誰でもに戻す 2件。T5 は `can_edit_teams` が常に真 24件・入口が staff を通す 3件・担当をほかのワークスペースでも数える 6件。

**W3 以降に残したもの**
- W3: リーグ・チーム・球場の登録画面（今は管理画面だけ）。名前の一意の違反を日本語の例外に訳す
- W4: ワークスペースの作成・設定・削除・上限・招待・メンバー管理（`can_manage_members`・`can_delete` は真理値表だけ固めた）。編集者と担当の割り当ては W4 まで管理画面で行う。「持ち主は編集者に入らない」「担当チームはそのワークスペースのチーム」を集約で検査する。ヘッダーのワークスペースの切り替え。管理画面の staff の役割（閲覧だけの権限のインラインの `clean()`。W1a T7 の残り）。メンバーでなくなった世界の作成者の扱い（W6 と合わせて）
- W6: 世界の数の上限をワークスペースごとに読み替える（今は1人5つを全ワークスペースで数える）・ワークスペースの持ち主による世界の削除・分岐元の扱い
- 測ってから決める: ペナントの条件に `world__workspace_id` を足す二重の防御
- 管理画面のオートコンプリートの候補は絞っていない（保存で弾く）
- 未ログインの人には、無い key（404）とある非公開の key（ログインへ）で応答が分かれる（T4 のレビュー。key は推測しにくいので、ログインへ送る便利さを取った）
- epic を main に入れるとき: 上の「epic を main に入れるときに必ず行うこと」（W1a の superuser の確認と、W2 の staff の編集者の確認・`SAMPLE_WORKSPACE_KEY`・古い URL の周知）と、main を取り込むたびのマイグレーション番号の確認

### W3（#79）

計画は #79 の計画コメント（2026-10-25。epic 1cd28f6 と照合）。決定（2026-10-25、すべて推奨）: 1. 球場はワークスペースで共有し、世界の球団が本拠地にしている球場は消せない／2. 所属リーグは試合が1つも無いときだけ変えられる／3. 管理画面は残してサイトと同じ規則で検査する（T5）／4. プロフィールの更新は `RosterService`（T4）。

#### T1 domain・永続化・サービス（`feature/league-setup-service`、2026-10-25）

画面はまだ使わない（T2・T3 の登録画面が使う）。マイグレーションは無い（`makemigrations --check` で確かめた。名前の長さの定数は今の列と同じ値）。
この節（と T4・T2・T5）のマイグレーションの番号（「0061 の制約」など）は書いた時点のままで、main の取り込み（2026-10-25 その4）の後は1つずつ後ろ（一意制約は `0062_workspace_constraints`）。

- domain:
  - 名前の長さの定数 `LEAGUE_NAME_MAX_LENGTH`（50）・`TEAM_NAME_MAX_LENGTH`（100）・`STADIUM_NAME_MAX_LENGTH`（100）を `entities.py` に置き、ORM の `max_length` はこれを import する。`clean_name()` が前後の空白を除いて空と上限超えを `InvalidName` にし、`League`・`Team`・`Stadium` の作成（`__post_init__`）と `rename()` が通す。既存の行を読むときに落ちないことは、既存のテストと開発 DB（リーグ8・チーム48・球場21。空・上限超えは0）で確かめた。
  - `domain/setup.py`: `league_deletion_blocker(team_count)`・`team_deletion_blocker(stint_count, captaincy_count, game_count)`・`stadium_deletion_blocker(real_home_team_count, world_home_team_count)`（消せるなら None、消せなければ日本語の理由）と、`league_change_blocker(game_count)`・`ensure_league_change_allowed(game_count)`（試合があれば `LeagueChangeLocked`）。
  - 例外 `InvalidName`・`DuplicateName`・`NotDeletable`・`LeagueChangeLocked`。`WorkspaceAccess.can_set_up()`（`can_edit_all()` と同じ）。
  - リポジトリの Protocol: `StadiumRepository`（新。`find_by_id`・`save`・`delete`）、`LeagueRepository.delete`・`TeamRepository.delete`。
- infrastructure:
  - `DjangoStadiumRepository(scope)`: 実データの範囲だけ（ペナントの範囲はコンストラクタで `InvalidWorld`）。`stadiums_in(scope)` で引き、範囲の外は `StadiumNotFound`。
  - 削除3つ: `transaction.atomic` の中で数を読む（`usage_counts`。クエリ1本）→ domain の規則（通らなければ `NotDeletable`）→ 範囲で絞った1行を消す。チームは担当の中間表の行も連鎖で消える。数え方は `league_usage`・`team_usage`・`stadium_usage`（相関した副問い合わせの COUNT）の1か所で、参照クエリの一覧も同じ注釈を使う。球場の世界の球団の数は、範囲のワークスペースの世界（台帳 `worlds_in` で絞る）の球団（`scoping.teams_of_worlds_in`）。
  - 名前の一意: リーグ・球場は保存の前に範囲の中の同名を確かめ（`_name_taken`）、書き込みをセーブポイントで包んで一意制約の `IntegrityError`（0061 の `unique_workspace_league_name`・`unique_workspace_stadium_name` と世界の `unique_world_league_name`）を `DuplicateName` に訳す。SQLite は違反の文言に制約名を出さないので、制約の列から組み立てた「表.列, 表.列」でも見る（出典は ORM の `Meta.constraints`）。チーム名は新しいチームと改名のときだけ `teams_in(scope)` の中の同名を確かめる。
  - 所属リーグの変更: `TeamRepository.save` が今のリーグと比べ、変えたときだけそのチームの試合（全区分）を数えて `ensure_league_change_allowed` を通す。
  - `DjangoSetupQuery(scope)`（実データ専用。コンストラクタで検査）: `leagues()`・`teams()`・`stadiums()` はそれぞれクエリ1本（行数によらない。多段の prefetch なし）、`league(id)`・`team(id)`・`stadium(id)` は範囲の外なら `*NotFound`。チームの本拠地は `home_stadium_in`（範囲の外の球場は本拠地なし）。
- application: `LeagueSetupService`（`application/league_setup.py`。更新だけ。依存はリーグ・チーム・球場のリポジトリ）。新しいリーグ・チームの表示順は末尾（今の最大＋1。チームはそのリーグの中、無ければ 0）。`SetupQuery`（Protocol）と DTO `LeagueSetupRow`・`TeamSetupRow`・`StadiumSetupRow`。消せない理由・所属リーグを変えられない理由は DTO のプロパティが件数から domain の関数で作る。
- presentation: `build_league_setup_service(scope)`・`build_setup_query(scope)`（`_require_real`。`REAL_ONLY_BUILDERS`）。`tests/helpers.py` が再輸出する。

**計画からのずれ**

- 例外を2つ足した: 名前の検査の `InvalidName` と、所属リーグの変更の `LeagueChangeLocked`（前例 `PostseasonFormatLocked` と同じく、理由の文言は domain の `league_change_blocker` が出典）。表示順（0以上の整数）と支配下選手の登録枠（空か0以上の整数）も `League`・`Team` の作成と `reorder()`・`change_rules()` で検査する（ORM の `PositiveIntegerField` の CHECK 制約に当たって 500 になるのを防ぐ）。
- **所属リーグの変更の規則はリポジトリ（`TeamRepository.save`）で通す**（サービスではなく）。理由: (1) 名前の一意・本拠地の範囲と同じく、集約が全体を見ないと決まらない検査の置き場所がリポジトリ（計画 3. の「違反はリポジトリが日本語の例外に変える」）。(2) 今のリーグはチームの存在を確かめるクエリで一緒に読めるので、変えない保存ではクエリが増えない。サービスに置くと、試合の数を読むためのメソッドをリポジトリの Protocol に足すことになり、`RosterService` など別の経路の保存には効かない。
- `scoping.py` に `teams_of_worlds_in(workspace_id)` を足した（計画は「変更なし」）。世界の球団の本拠地を数えるのに、`.objects` を直接書かずに範囲の出入口を通すため。
- チーム名・リーグ名の保存の前の確認は、ペナントの世界の範囲でも同じく範囲の中で効く（計画は「実データの中で一意」。世界の行は分岐の写しで、範囲の中では重複しない。世界の作成は新しい球団1つにつき1クエリ増える）。
- 球場の数に入れる世界の球団は、そのワークスペースの世界の球団だけ（ORM の直書きでほかのワークスペースの世界の球団が指していても数えない。消すと `SET_NULL` で外れるが、もともと範囲の外の本拠地として読まれない行）。
- `TeamSetupRow` に、削除の確認に出す担当者の数（`manager_count`）を足した（計画 6.）。
- 既存のテストの直し: `test_repositories.py` の `HomeStadiumCheckQueriesTest` は、本拠地を変えない保存の基準に改名を使っていた（改名は重複の検査で1クエリ増える）。基準を表示順の変更に替えた（意図は同じ）。改名と所属の変更だけがクエリを1本増やすことは新しい `NameCheckQueriesTest` が固定する。
- 試合を数える注釈は区分を明示する（`stage_condition("", StageFilter.ALL_STAGES)`。`test_postseason_isolation.py` の検査に合わせる）。

**テスト**: domain `test_setup_rules.py`（新。名前の検査・表示順と枠・消してよいかの規則と文言・所属リーグの変更）、`test_workspace_access.py` の真理値表に「登録」の列。integration `test_league_setup_service.py`（新。往復・表示順の末尾・名前の一意はワークスペースの中だけ（B と世界では同じ名前を作れる）・`IntegrityError` の訳し・消せない行は `NotDeletable` で全表の行が変わらない・消すのはその1行だけ（`snapshot_all_rows()` と担当の中間表）・世界の球団だけが本拠地の球場も消せない・ほかのワークスペースの世界の球団は数えない・所属リーグの変更・B の id を混ぜた書き込み・ペナントの範囲を拒む・一覧のクエリ数が行数によらず1本ずつ・改名と所属の変更だけがクエリを増やす）、`test_workspace_isolation.py` の `CHECKS` に `DjangoStadiumRepository`・`DjangoSetupQuery`（A の範囲で B の行が出ない・B の id は見つからない・A のリーグ・チーム・球場の削除が B の行を消せない）、`test_wiring.py` の分類（`REAL_ONLY_BUILDERS`）・`ProtocolConformanceTest`・依存の検査。

**変異の確かめ**（どれも戻した。`test_league_setup_service`・`test_workspace_isolation` を流した）: 削除が規則を通らない（3つの `raise NotDeletable` を外す）→ 6件、`DjangoStadiumRepository` が範囲で絞らない → 3件（うち isolation の `CHECKS` 1件）、チーム名の検査が範囲で絞らない → 16件（B と同じ名前を作れない2件と、世界の分岐が実データと同じ名前で弾かれて isolation の準備が14クラスで落ちる）。

**レビュー（ddd-boundary-reviewer）で直したもの**: (1) 世界の作成が、分岐元に同じ名前のチームがあると写す途中で `DuplicateName` になる → `create_world` がリーグ名と同じく写す前に `InvalidWorld` で弾く（実データのチーム名は管理画面・投入コマンドが検査しないため）。(2) 所属リーグの変更の試合の数え方が一覧と別だった → 注釈 `team_game_count` の1か所にまとめた。(3) サービスが `Team.league_id`・`home_stadium_id` を直接書いていた → `Team.move_to_league()`・`change_home_stadium()`。(4) 前後に空白のある古いチーム名の行が、名前を変えない保存を改名扱いにして重複の検査で落ちうる → 空白を除いて比べる。残したもの: 数えてから消すまでの間の同時の書き込み（SQLite は書き込みが直列なので実害は小さい。`PROTECT` にして `ProtectedError` を訳す案はマイグレーションと世界の削除の順序の確かめが要るので、要るならユーザーが決める）。

件数: domain＋integration 3,863（W2 の T6 より +48）。e2e は画面を変えないので流していない。

**T2〜T4 への申し送り**

- 画面は `DomainError`（`InvalidName`・`DuplicateName`・`NotDeletable`・`LeagueChangeLocked`・`*NotFound`）をフォームのエラーにする。削除のリンクは `deletion_blocker` が None の行だけに出し、消せない行の削除 URL は理由と「戻る」だけ（計画 6.）。所属リーグの選択は `league_change_blocker` があれば出さない（両立しない操作を並べない）。
- フォームの選択肢の出典: 外国人枠の既定は `ForeignPlayerRules.NPB`、支配下枠の既定は `DEFAULT_REGISTERED_PLAYER_LIMIT`、グラウンド・屋根は `StadiumProfile.SURFACES`・`ROOFS`、名前の長さは `*_NAME_MAX_LENGTH`。所在地（`city`）の長さ100は domain が検査しないので、フォームで止める。
- `update_team` は集約（ロスター込み）を読んで保存する。所属リーグを変えるとき、移す先のリーグの支配下枠・外国人枠は検査しない（試合の無いチームだけが移れるので外国人枠は効かないが、支配下枠を超えたまま移れる。T3 で扱いを決め、検査を足すなら `Team.move_to_league()` に足す）。
- T4: プロフィールは `RosterService` に置く（決定 4）。`LeagueSetupService` には足さない。
- T5: 管理画面の削除とチーム名の検査は、`domain/setup.py` の規則と `TeamRepository` の重複の検査を同じ形で通す。

#### T4 選手のプロフィールの編集（`feature/player-profile-edit`、2026-10-25）

サイトの選手の編集画面に、プロフィールの14項目（よみがな・背ネーム・生年月日・投打・身長・体重・出身地・ドラフト（年・順位・育成）・出身高校・大学・社会人・国籍・外国人選手か）の節を足した。マイグレーションは無い（`makemigrations --check`）。

- domain: `Team.change_player_profile(player_id, profile)`（`ensure_draft_fits_career` を通してから置き換える。値そのものは `Profile`・`DraftPick` が作るときに検査）。`DraftPick.from_inputs(year, round_, is_development=)`（年が空なら None、年の無い順位・育成は `InvalidDraftPick`）。文字の欄の長さの定数 `PROFILE_TEXT_MAX_LENGTH`（100）・`BACK_NAME_MAX_LENGTH`（20）を `value_objects.py` に置き、ORM の `max_length` はこれを import する（値は同じ）。
- application: DTO `ProfileInput`（素の値だけ。投打は表記）、変換 `application/player_profile.py`（`profile_from_input`・`profile_input_of`）。`RosterService.update_player(..., profile: ProfileInput | None = None)` が名前・背番号・守備位置と**同じ1回の集約の保存**で書く。None ならプロフィールに触れない。`PlayerDetail.profile`（編集画面の初期値）。
- presentation: `PlayerProfileForm`（欄の型と長さだけ。投打の選択肢は `Handedness`）、目印 `PROFILE_SECTION_MARKER`（隠し欄）。`player_edit` は**目印があるときだけ**プロフィールのフォームを読み、無い送信（基本情報だけ・退団などのボタン）でプロフィールを空で上書きしない。失敗したら送った値のまま出し直す。`player_edit.html` に「プロフィール」の節（同じフォーム・同じ「更新する」）。
- 管理画面: `PlayerForm` の「年の無い順位・育成」の検査を `DraftPick.from_inputs` に移した（文言と、年の欄のエラーにする扱いは同じ）。

**計画からのずれ**

- 計画にない `DraftPick.from_inputs` を足した。管理画面の `PlayerForm` だけが持っていた規則（年の無い順位・育成を拒む）を、サイトにも同じく効かせるため domain に移した（画面に複製しない）。
- 長さの定数を domain に足した（T1 の名前の長さと同じ形）。`Profile` 自身は長さを検査しない（入力の経路のフォームが止める。従来どおり）。

**テスト**: domain `test_profile.py` の `ChangePlayerProfileTest`（置き換え・経路と食い違うドラフトと身長の上限は例外で選手は変わらない・知らない選手）、`test_draft_pick.py` の `DraftPickFromInputsTest`。integration `test_player_profile_edit.py`（新。14項目の往復と編集画面の初期値・育成ドラフトの往復・目印つきの空欄はプロフィールを空にする・選手詳細に出る・目印の無い送信と退団・主将のボタンでプロフィールが消えない・身長の上限／年の無い順位／経路と食い違うドラフト／数値でない身長／知らない投打／長すぎる背ネームは何も保存しない・ほかのチームの担当は 403）、`test_workspace_permissions.py` の「選手の編集（POST）」の本文にプロフィール（基本情報だけが書かれたら落ちる）、`test_cross_workspace_ids.py` の「プロフィールの編集」（B の id は 404）。件数: domain＋integration 3,884（T1 より +21）、e2e 14（+1）。

**変異の確かめ**（どれも戻した）: 目印を見ずにプロフィールを読む → `test_a_post_without_the_marker_keeps_the_profile` が落ちる（1件）、`change_player_profile` が経路の整合を見ない → domain と integration の2件、`update_player` がプロフィールを捨てる → 27件（往復・権限の表の持ち主・編集者・担当の行など）、`from_inputs` が年の無い順位を許す → 4件。

**レビュー（ddd-boundary-reviewer）**: 重要な指摘なし。軽微のうち、管理画面のコメントの途切れ・`draft_of` の docstring（読む変換と入力の検査の使い分け）・長さの定数のコメント（`Profile` は検査しない）を直した。残したもの: プロフィールの欄と退団・主将・支配下登録のボタンが同じ `<form>` にあり、欄を書き換えてからボタンを押すと入力は捨てられる（名前・背番号も以前から同じ。データは失われない）。分けるかはユーザーが決める。

**Enter での送信の不具合の修正（既存の不具合。T4 で直した）**: 入力欄で Enter を押すと、HTML の仕様でフォームの文書順で最初の送信ボタンが押されたことになり、この画面では「支配下登録する」（育成選手）か「主将にする／解任する」が押されていた（プロフィールの欄が増えて踏みやすくなった）。フォームの先頭に「更新する」と同じ送信（name・value なし）を見えない形（`visually-hidden`・`tabindex="-1"`・`aria-hidden`）で置いた。再発防止: integration `DefaultSubmitButtonTest`（描いた HTML で最初の送信ボタンが「更新する」。育成・主将・主将でないの3通り）、e2e `test_player_edit_enter.py`（育成で主将でない選手のよみがなで Enter → 更新され、昇格も主将の指名も起きない）。変異: 隠しボタンを消す → integration 3件・e2e 1件が落ちる。

#### T2 リーグと球場の画面（`feature/setup-leagues-stadiums`、2026-10-25）

マイグレーションは無い（`makemigrations --check`）。

- URL 6本（`_w(..., login=True)`）: `setup/leagues/`（`setup_leagues`。一覧と作成）・`setup/leagues/<id>/`（`setup_league_edit`）・`setup/leagues/<id>/delete/`（`setup_league_delete`。GET は確認、POST は削除）、球場も同じ形（`setup_stadiums`・`setup_stadium_edit`・`setup_stadium_delete`）。
- ビュー: 門は `_requires_set_up(request)`（`can_set_up()` が偽なら 403。解決済みの権限だけを見て、参照クエリもサービスも組み立てる前に拒む）。GET も POST も同じ門。一覧・初期値・消せない理由は `build_setup_query`、書き込みは `build_league_setup_service`。範囲の外の id（ほかのワークスペース・ペナントの世界）は表示でも保存でも 404。`InvalidName`・`DuplicateName` は名前の欄、ほかの `DomainError` はフォーム全体のエラー（200）。削除は消せない行なら理由と「戻る」だけを出し、POST しても `NotDeletable` の理由を出して何も消さない。
- フォーム: `LeagueSetupForm`（作成は `with_display_order=False` で表示順の欄を外す。外国人枠3つと支配下枠は空欄で無制限。初期値は `ForeignPlayerRules.NPB`・`DEFAULT_REGISTERED_PLAYER_LIMIT`。`clean()` が `ForeignPlayerRules` を組み立てる）・`StadiumSetupForm`（選択肢は `StadiumProfile.SURFACES`・`ROOFS`、開場年は `Season` の範囲。`clean()` が `StadiumProfile` を組み立て、その例外をフォームのエラーにする）。数値の欄は DB の整数の範囲（`MAX_STORED_INTEGER`）で頭を押さえる。
- テンプレート（`myapp/setup/`）: `_head.html`（パンくず・見出し・タブ。チームのタブは T3 が足す）・`_field.html`・`_stadium_fields.html`・`leagues.html`・`league_edit.html`・`stadiums.html`・`stadium_edit.html`・`delete.html`（**削除の確認は共通**。ビューの `_deletion_page(kind, name, reason, back_url, setup_tab, notes)` が描く。T3 のチームは `notes` に外れる担当者の数を渡す）。削除のリンクは `deletion_blocker` が None の行だけ。世界の球団が本拠地の球場の編集画面は「変更は世界にも映ります」と知らせる。
- 導線: コンテキストプロセッサが `can_set_up` を渡し、ヘッダーの「登録」（行き先は `setup_leagues`）とダッシュボードの空の案内（チームが1つも無いとき）をそれで囲む。

**計画からのずれ**

- 所在地の長さの定数 `STADIUM_CITY_MAX_LENGTH`（100）を `domain/entities.py` に置き、ORM の `city` の `max_length` とフォームがそれを参照する（T1 の申し送りは「フォームで止める」。レビューの指摘で出典を1か所にした。domain は検査しない。値は同じなのでマイグレーションは出ない）。
- `test_workspace_isolation.py` の「B の id を混ぜた POST」の検査から、本文にも URL にも id を持たない作成（`setup_leagues`・`setup_stadiums`）を外した（`ID_FREE_WRITES`。混ぜる id が無い。門は部外者の POST の検査が受け持つ）。
- `tests/helpers.header_pages()` にヘッダーの「登録」を足した（ペナントの画面のリンクが世界の外へ出てよい行き先。e2e で見つかった）。
- 空のワークスペースの「id を取らない画面がどれも開く」の検査で、試合の登録だけは既存どおり試合一覧へ戻す（チームが無いと対戦カードを選べない）。

**テスト**: integration `test_setup_screens.py`（新。作成・編集・削除・表示順の末尾・空欄は無制限・NPB の初期値・名前の重複と空と長さ・枠と属性の不正はフォームのエラーで行が変わらない・ほかのワークスペースと同じ名前は作れる・削除のリンクは消せる行だけ・消せない行の削除は理由と戻るだけで POST しても消えない・世界の球団だけが本拠地の球場も消せない・ペナントのリーグの id は 404・門（担当は全 URL で 403 で何も書かない・門は参照クエリとサービスの組み立ての前に拒む・編集者は通る・未ログインはログインへ）・ヘッダーの導線・空のワークスペース（id を取らない PAGE が開く・案内は持ち主と編集者だけ・一から球場とリーグを作れる）・一覧と編集・削除の画面のクエリ数が行数によらない）、`test_workspace_isolation.py`（`URL_TABLE` に6本・`Side.stadium`・`WRITE_BODIES`・持ち主の本文が実際に書くことの確かめに作成と編集を足した）、`test_workspace_permissions.py`（真理値表に「ヘッダーの登録の導線」「登録画面（GET）」「リーグの登録（POST）」の列）、`test_cross_workspace_ids.py`（B のリーグ・球場の id で GET・POST とも 404 で行が変わらない・一覧に B の行が出ない）。

**変異の確かめ**（どれも戻した。`test_setup_screens`・`test_workspace_permissions`・`test_cross_workspace_ids`・`EveryWorkspaceUrlTest` の58件を流した）: 門を外す → 57件、削除のリンクを常に出す → 3件、ヘッダーの導線を `can_set_up` で絞らない → 15件、消せない行にも削除のボタンを出す → 4件、ドメイン例外をフォームのエラーにしない → 4件（500）。

**レビュー（ddd-boundary-reviewer）**: 重要な指摘なし。軽微で直したもの: 所在地の長さの出典（上のずれ）・`ID_FREE_WRITES` の条件に括弧・Wiki の案内の条件（「リーグが無い」→「チームが無い」。ダッシュボードはチームのあるリーグだけを並べる）。残したもの: 表示順は登録画面では数値、管理画面ではドラッグの2系統（別の画面なので並べない規則には当たらない。管理画面の扱いは T5 で決める）。

件数: domain＋integration 3,902（T1 より +39）、e2e 13。

**T3 への申し送り**

- チームの画面は `_head.html` にタブ（リーグと球場の間）を足し、削除は `_deletion_page(..., notes=[外れる担当者の数])` を使う。所属リーグの欄は `league_change_blocker` があれば出さない。
- ダッシュボードの案内の文言（「リーグと球場を登録する」）と、チーム一覧の空の文言（「管理画面から追加してください」）を、チームを作れるようになった形に直す。
- e2e `test_setup_flow.py` はヘッダーの「登録」から入る。

#### T5 管理画面の検査をそろえる（`feature/admin-setup-rules`、2026-10-25）

決定 3（管理画面は運営者の道具として残し、削除の条件とチーム名の一意をサイトと同じ規則で検査する）を入れた。マイグレーションは無い（`makemigrations --check`）。T2（#313）が epic に入る前の先端（550f8be）から始めた。

- **削除（リーグ・チーム・球場）**: `admin.py` に `BlockedDeletionAdmin`（消せない行は `has_delete_permission` が偽＝削除のボタンを出さず、削除画面を直接開いても・POST しても 403。一括削除は `_delete_selected_unless_blocked` が消せない行を1つでも見つけたら何も消さず、行ごとの理由を messages に出す）と、それを継ぐ `SetupRulesDeletionAdmin` を置いた。`PlayerStintAdmin` の同じ仕組み（`deletion_blocker`・一括削除の差し替え）もこの部品に寄せた（一括削除の文言はモデルの表示名から作る）。
  - 消せるかは**サイトの登録画面と同じ参照クエリの行**（`build_setup_query(scope).league(id)` などの `deletion_blocker`）で決める。数え方（`league_usage`・`team_usage`・`stadium_usage`。球場は世界の球団の本拠地も数える）と規則（`domain/setup.py`）を管理画面に複製しない。範囲は行のワークスペースの実データ（`_row_scope`）。1回の画面で何度も聞かれるので、行のインスタンスに覚えておく（行ごとにクエリ1本）。
  - **消すのも登録のサービス**（`build_league_setup_service(scope).delete_*`。`delete_model`・`delete_queryset` を差し替え）。リポジトリが数え直して規則を通してから範囲で絞った1行を消すので、Django の collector の連鎖（リーグ→チーム→在籍・試合）では消さない。一括削除はトランザクション1つで、途中で弾かれたら何も消えない。
- **リーグの変更画面のチームのインライン**（`TeamInlineFormSet`）: 消せないチームの行は「削除」の欄を隠す（`HiddenInput`。消せる/消せないはワークスペースのチームの行をクエリ1本で読む）。偽造して送っても `clean` が理由つきで弾く。行の削除（`delete_existing`）も登録のサービスで消す。
- **チーム名の一意**: 検査の出典を `infrastructure/repositories.py` の `ensure_team_name_free(teams, name, own_id)`（と文言の `duplicate_team_name`）の1か所にし、`DjangoTeamRepository.save` と管理画面の `TeamForm`・`TeamInlineFormSet` が同じ関数を通す（出典を2つにしない形。リポジトリの検査を関数に切り出し、管理画面はフォームの検証の段で同じ関数を呼んでエラーに訳す）。検査するのはリポジトリと同じく新しいチームと改名のときだけ。インラインは保存した後の状態で比べる（このリーグの行どうしと、このリーグの外のワークスペースのチーム。A↔B の入れ替えは通す）。
- **所属リーグの変更**: `TeamForm` がリーグを変えたときだけ、登録画面の行の `league_change_blocker`（試合を区分を問わず数える）を見て、試合があればリーグの欄のエラーにする。
- 規則を通らない経路の確かめ: リーグ・チームの削除は上のとおり。球場→チームは `SET_NULL` だが球場の削除が規則を通る。`Workspace` の削除はリーグ・球場が `PROTECT`。在籍・主将在任・試合の削除はそれぞれの管理画面の行の削除（チームは消えない）。選手の削除（在籍の連鎖）は W3 の範囲の外（計画のやらないこと）。

**計画からのずれ**

- 消せるかの判定に、件数の注釈を直接使う代わりに登録画面の参照クエリ（`build_setup_query`）の行を使った（注釈も規則も同じものが通る。数えて規則に渡す2行も管理画面に複製しない）。
- `PlayerStintAdmin` の削除の部品を共通にした（計画は新しい3つだけ）。
- 管理画面の削除を登録のサービスに通した（計画は「削除の可否を規則で決める」まで）。導線を隠すだけだと、数えてから消すまでの間に在籍や試合が入ったときに連鎖で消える。

**テスト**: `test_admin.py` に `SetupRulesDeletionTest`（消せない行（チームのあるリーグ・在籍のあるチーム・ホーム／ビジターの試合のあるチーム・実データのチームの本拠地・世界の球団だけの本拠地）は変更画面に削除のリンクが無い・削除画面は GET も POST も 403 で全表の行が変わらない・一括削除は消せない行を含めると何も消さず理由を出す・空のリーグ・チーム・球場は消せて消えるのはその1行だけ・空の行の一括削除・主将の在任のあるチーム）、`TeamInlineSetupRulesTest`（削除の欄は消せるチームだけ・偽造した削除は理由つきで弾き行が変わらない・空のチームは行から消せる・新しい行／改名がワークスペースの中の名前と重なるとエラー（同じリーグ・ほかのリーグ・新しい行どうし）・入れ替えとほかのワークスペースの名前は通る）、`TeamFormSetupRulesTest`（追加と改名の重複はフォームのエラー・ほかのワークスペースと名前を変えない保存は通る・試合のあるチームは所属リーグを変えられない・試合の無いチームは変えられる）。件数: domain＋integration 3,898（T4 より +14）、e2e 14（変わらず）。

**変異の確かめ**（どれも戻した。`test_admin` を流した）: 導線の検査（`has_delete_permission`）を外す → 7件、一括削除の差し替えを外す → 6件（行はリポジトリの検査で守られ、落ちるのは「何も消さない」と理由の表示）、`TeamForm` の名前の検査を外す → 2件、インラインの名前の検査を外す → 4件、所属リーグの検査を外す → 1件、インラインの削除の検査を外す → 1件（サービスの `NotDeletable` が 500 になる）、さらに collector で消す → 1件、インラインの削除の欄を隠さない → 1件、導線の検査と `delete_model` を外す → 7件。

**レビュー（ddd-boundary-reviewer）**: 重要な指摘なし。残した軽微な指摘: (1) 検査と削除の間に在籍などが足されると、サービスの `NotDeletable` が 500 になる（データは消えない。一括削除では Django が `delete_queryset` の前に削除の LogEntry を書くので、弾かれても記録が残る）。(2) ワークスペースの削除の確認画面で、空でないリーグ・球場が「保護されている」ではなく「削除する権限がない」の表示になる（`get_deleted_objects` が行ごとに `has_delete_permission` を呼ぶため。消せないことは変わらない）。(3) 改名の判定がリポジトリ（前後の空白を除いて比べる）と管理画面（`changed_data`）でわずかに違う（管理画面が検査する方向に多いだけ）。(4) 一括削除の検査は行ごとにクエリ1本（一覧の `list_select_related` で行の関連は読み済み）。

#### main の取り込み（2026-10-25 その4）

`refactor/merge-main-into-workspaces-5` で、epic 23189a9（W3 T5 の後）に main 9f6a8fb を取り込んだ（マージコミット 24896c4）。T3 の前。

- **main から来たもの**: #198 D2（PR #303。ドラフト会議の育成の巡。新人の契約区分 `Draftee.contract`・`SeasonClosed.development_draftee_count`）、#302（PR #306。AI の1位の入札に球団ごとの好み）、#307（PR #309。社会人は育成で指名できない。`DraftRoute.development_amateur_paths()`。`seed_virtual_players` の育成の選手の経歴もそこから引く）、#308 P1（PR #311。選手ごとの伸びしろ `Potential`・`PennantPlayerRatings.potential`。マイグレーション `0059_pennantplayerratings_potential`）、#308 P2（PR #314。スカウトが伸びしろも推定し、AI のドラフトを見込みで選ぶ）。どれもペナントのオフ・ドラフト・成長の domain が中心で、ORM に触れるのは `DjangoRatingsRepository` の `potential` の列の写しだけ。
- **振り直し**: main が 0059 を使ったので、epic のマイグレーションを `0060_workspace`（依存は `0059_pennantplayerratings_potential`）・`0061_backfill_default_workspace`・`0062_workspace_constraints`・`0063_team_managers_help_text` に移した。依存・docstring・コメント（`admin.py` の編集者の行・`repositories.py` の名前の一意制約・`test_jersey_number.py`・`test_migration_draft_year.py`・`test_workspace_constraints.py`）・`test_workspace_migration.py` の BEFORE/AFTER をそろえた。この節より前の記録の番号（T1 の「0061 の `unique_workspace_league_name`」など）は書いた時点のままで、今は1つずつ後ろ（制約は 0062）。空の DB からの `migrate` と `makemigrations --check` を確かめた。PR の直前に origin/main を fetch し直し、最新が 0059 のままであることを確かめた。
- **衝突**は5ファイルで、どれも import だけ: `repositories.py`（W3 T1 の `domain.setup` と main の `Potential` を両方）、`seed_virtual_players.py`（main の `REAL_SCOPE = WorldScope.real()` は epic で `--workspace` の範囲 `_real_scope()` に置き換え済みなので落とし、main の `ALL_PATHS`・`DEVELOPMENT_PATHS` を残した。main の `_refresh_schools` の変更は自動で `players_in(self._real_scope())` の形に乗った）、テスト3本（`test_pennant_offseason.py`・`test_pennant_ratings.py`・`test_seed_virtual_players.py`。epic で使わなくなった `WorldScope` の import を落とし、main の追加の import を残した）。
- **合わせ込み**: 要らなかった。新しい組み立て口・`Django*`・URL・管理コマンドの引数は無く（`test_wiring.py`・`CHECKS`・`URL_TABLE` は変えていない）、main の新しいテストは既に epic の形（`world_scope`・`world_ledger`・編集者で作る世界）の上に書かれた既存のテストクラスへの追加だった。W3 の登録の規則とは交わらない（チーム名の一意はチームの新規作成と改名のときだけで、新人の追加は `Team.add_player` の在籍。世界の作成（`fork`）は main で変わっていない。伸びしろは作成時の能力の行に引くだけ）。
- **変異の確かめ**: 新しい `Django*` が無いので行っていない。
- **計測**: やり直していない（#303〜#314 はペナントの締める処理・ドラフト・成長の変更で、ビューの変更は締めた後の messages の文言だけ。`measure_pages` の画面の読み取りは変えない）。
- テスト: domain＋integration 4,007、e2e 14。
- **レビュー**（`ddd-boundary-reviewer`）: 重要な指摘なし。範囲なしの形（`WorldScope.real()`・`REAL_SCOPE`）が戻っていないこと、マイグレーションが1本道であること、domain が Django を import しないことを確かめた。軽微な指摘のうち、この記録が付け直しに追いついていない点はこの節で直した（前の記録の番号は書いた時点のまま）。

#### T3 チームの画面（`feature/setup-teams`、2026-10-25）

マイグレーションは無い（`makemigrations --check`）。

- URL 3本（`_w(..., login=True)`）: `setup/teams/`（`setup_teams`。一覧と作成）・`setup/teams/<id>/`（`setup_team_edit`）・`setup/teams/<id>/delete/`（`setup_team_delete`）。門は T2 と同じ `_requires_set_up`。範囲の外のチーム（ほかのワークスペース・ペナントの世界）の id は 404。
- フォーム `TeamSetupForm`: 所属リーグと本拠地の選択肢は `SetupQuery.leagues()`・`stadiums()` の行から作る（**選択肢は導線にすぎず、止めるのはリポジトリの範囲の検査**。選択肢の外の値はその手前のフォームのエラー）。本拠地は空欄で本拠地なし。作成は `with_display_order=False`（リーグの末尾）、試合のあるチームの編集は `with_league=False`（欄を外し、POST に `league` があっても今のリーグで保存する）。ドメイン例外は欄ごとに出す（名前→名前の欄、`LeagueNotFound`・`LeagueChangeLocked`・`RegisteredPlayerLimitExceeded`→所属リーグの欄（無ければ全体）、`StadiumNotFound`→本拠地の欄）。
- **移す先のリーグの支配下枠（T1 の申し送り）**: `Team.move_to_league(league_id, *, limits: RosterLimits)` にし、今の支配下選手（育成は数えない）が移す先の `League.roster_limits` を超えるなら `RegisteredPlayerLimitExceeded`（日本語）で何も変えない。今と同じリーグなら検査しない（後から枠を下げたリーグのチームも、所属を変えない保存では咎めない）。`limits` は `add_player` と同じく必須。`LeagueSetupService.update_team` は所属を変えるときだけ範囲のリーグリポジトリから移す先を引く（範囲の外は `LeagueNotFound`）。
- テンプレート: `teams.html`（リーグの表示順・チームの表示順の一覧。本拠地・在籍の記録・試合の数。リーグが無いうちは作成フォームの代わりにリーグのタブへの案内）・`team_edit.html`（試合のあるチームは所属リーグの欄の代わりに今のリーグと `league_change_blocker`）。削除は `_deletion_page(..., notes=[外れる担当者の数])`（`TeamSetupRow.manager_count`。0人なら出さない）。
- 導線: `_head.html` にチームのタブ、ダッシュボードの空の案内を「リーグ・球場・チームを登録する」に、チーム一覧の空の文言を「管理画面から追加してください」から、持ち主・編集者への「チームを登録する」（`setup_teams`。ペナントの世界の一覧には出さない）に直した。

**計画からのずれ**

- タブの位置: T2 の申し送りは「リーグと球場の間」だったが、`_head.html` のコメントの「作る順（球場は本拠地としてチームから選ぶので、チームより先に作る）」に従い、リーグ・球場・チームの順にした。
- `test_setup_screens.py` が800行を超えたので、チームの画面のテストと #100 の接続のテストを `test_setup_teams.py`（新）に分けた（`.claude/rules/tests.md` の目安。レビューの指摘）。門・空のワークスペース・クエリ数のチームの分は `test_setup_screens.py` に置いたまま。
- `test_workspace_permissions.py` のチームの作成の列は、世界の作成（同じ表の前の列）が元のリーグの名簿を検査するので、選手のいないチームを「ほかのリーグ」に足す。

**テスト**: domain `test_setup_rules.py`（`MoveToLeagueLimitTest`: 枠に収まれば移る・超えれば移らず理由・同じリーグは咎めない）、integration `test_setup_teams.py`（新。一覧・選択肢はワークスペースの行だけ・作成はリーグの末尾・本拠地なし・名前の重複と空と長さと選択肢の外はフォームのエラー・編集・本拠地を外す・改名の重複・試合のあるチームは欄が出ず POST しても所属が変わらない・支配下枠を超える移動はフォームのエラーで行が変わらず枠に収まれば移る・削除のリンクは消せるチームだけ・消せないチームの削除は理由だけで POST しても消えない・担当者の数の案内と担当の割り当てだけが外れる・ペナントの球団の id は 404・リーグが無いと作成フォームの代わりに案内・**#100**: 画面だけで2チーム各10人のリーグを作り、世界の作成が「〜は、試合を組める名簿ではありません。」で止まり行が変わらない）、`test_setup_screens.py`（門の URL にチームの3本・チーム一覧の空の案内は持ち主だけ・一から球場とリーグとチーム2つ・チームの一覧と編集・削除のクエリ数）、`test_league_setup_service.py`（支配下枠を超える移動は何も書かない）、`test_workspace_isolation.py`（`URL_TABLE`・`WRITE_BODIES` に3本、持ち主の本文が実際に書く確かめに作成と編集）、`test_workspace_permissions.py`（「チームの登録（POST）」の列）、`test_cross_workspace_ids.py`（A のチームの作成・編集に B のリーグ・B の球場の id はフォームのエラーで行が変わらない・B のチームの id は 404・一覧に B の行が出ない）、`test_world_isolation.py`（`SetupScreensAndTheWorldTest`: 登録画面に世界の目印が出ない・サイトで球場を直しても世界の球団の本拠地は外れず収容人数は映る・実データのチームの本拠地を外しても世界の球団は変わらない・世界の球団だけが使う球場は消せない）、e2e `test_setup_flow.py`（新。staff でない空のワークスペースの持ち主が、トップ → ワークスペース → ヘッダーの「登録」→ 球場 → リーグ → チーム2つ → 選手の登録 → 試合の登録までをブラウザだけで行う）。

**変異の確かめ**（どれも戻した）: 移す先の枠の検査を外す（`count = 0`）→ domain 1件・integration 2件、試合のあるチームにも所属リーグの欄を出す → 1件、担当者の数の案内を外す → 1件、チームのタブを外す → e2e 1件、世界の作成の名簿の検査を外す → #100 のテスト1件（初めは `managed_team` を送っておらずフォームの検証で止まって空振りしていたのを、この変異で見つけて直した。フォームが通ったことも確かめる）。

**レビュー（ddd-boundary-reviewer）**: 重要な指摘なし。軽微で直したもの: チーム一覧（実データとペナントで共有のテンプレート）の登録の案内を世界では出さない・試合があって枠も超えるチームをサービスから直接動かすと理由が枠のほうになることを docstring に書いた（画面は欄を出さないので届かない）・テストのファイルの分割（上のずれ）。

件数: domain＋integration 3,953（T2 より +51。T4 の分を含む）、e2e 15。

**T6 への申し送り**

- #79 の完了条件の「空のワークスペースからブラウザだけで試合の登録まで」は e2e `test_setup_flow.py` が満たす。
- 決めてほしいこと（ユーザー）: 移す先のリーグの支配下枠を超えるチームの所属リーグの変更を拒む（推奨で実装済み）。代わりに「移せるが警告」「枠を見ない」にするなら `Team.move_to_league` だけを直す。

#### T6 記録と規則（`docs/workspace-w3-records`、2026-10-25）

- 計測（下の「W3 のまとめ」の表）。基点 1cd28f6（W2 の最後）と W3 の先端 4d50bdc で、全画面のクエリ数が同じ。
- CLAUDE.md: 「application のサービスは対象ごとに分けてある」の更新の一覧に `LeagueSetupService` を足した（ほかの文は変えていない）。
- `.claude/agents/ddd-boundary-reviewer.md`: 6. のサービスの一覧に `LeagueSetupService`。管理画面（`admin.py`）に登録の規則の別の数え方・別の削除の経路を作っていないか（消せるかは `build_setup_query(scope)` の行の `deletion_blocker`、消すのは `build_league_setup_service(scope)` の `delete_*`、チーム名は `ensure_team_name_free`）を観点に足した。
- ROADMAP: フェーズ9 の W3 を ✅（main へは epic ごと入れる）。
- この設計書: 4. の `LeagueSetupService` の責務（プロフィールは `RosterService` に置いた）と `RosterService` の行、6. の登録画面の行（見るのも全体の編集権・URL 9本）、9. の W3 の行（完了）、T1 の節のマイグレーションの番号は書いた時点のままである旨。
- 件数: domain＋integration 4,037（コードは変えていない。T3 の後の epic の先端と同じ）。e2e は画面を変えないので流していない。

#### W3 のまとめ（T6、2026-10-25）

**何をしたか**: 今は管理画面にしか無かったリーグ・チーム・球場の登録・編集・削除と、選手のプロフィールの編集をサイトの画面に作った。管理画面で作った空のワークスペースの持ち主（staff ではない）が、ブラウザだけで球場・リーグ・チームを作り、選手を登録し、試合を登録できる（e2e `test_setup_flow.py`）。管理画面は運営者の道具として残し、削除とチーム名・所属リーグの検査をサイトと同じ規則・同じ経路にそろえた。

| 段階 | PR | 内容 |
| --- | --- | --- |
| T1 | #310 | `domain/setup.py`（消してよいかの規則・所属リーグの変更の規則）、例外 `InvalidName`・`DuplicateName`・`NotDeletable`・`LeagueChangeLocked`、`WorkspaceAccess.can_set_up()`、`DjangoStadiumRepository`、削除3つ、`LeagueSetupService`・`SetupQuery`・DTO、組み立て口 `build_league_setup_service(scope)`・`build_setup_query(scope)`。名前の一意の違反を日本語の例外に訳す |
| T4 | #312 | 選手の編集画面にプロフィールの14項目（`RosterService.update_player` の同じ1回の保存。目印があるときだけ更新）。Enter で最初の送信ボタンが押される既存の不具合を直した |
| T2 | #313 | リーグと球場の登録画面（6本）。ヘッダーの「登録」・空のワークスペースの案内 |
| T5 | #315 | 管理画面の削除（リーグ・チーム・球場）を登録画面の参照クエリと登録のサービスに通す。チーム名の一意（`ensure_team_name_free`）と所属リーグの変更をサイトと同じ検査に |
| — | #316 | main（#303・#306・#309・#311・#314）の取り込み。マイグレーションを 0060〜0063 に振り直した |
| T3 | #317 | チームの登録画面（3本）。移す先のリーグの支配下枠の検査（`Team.move_to_league(..., limits=)`）。#100 との接続（一から作った小さいリーグからは世界ができない）。e2e `test_setup_flow.py` |
| T6 | この PR | 計測・記録と CLAUDE.md・`ddd-boundary-reviewer.md`・ROADMAP |

**決定**（#79 のコメント、2026-10-25。すべて推奨）: 1. 球場はワークスペースで共有する台帳とし、編集はペナントの世界にも映る。世界の球団が本拠地にしている球場は消せない（世界ごとの複製は W6）／2. チームの所属リーグは試合が1つも無いときだけ変えられる／3. 管理画面は運営者の道具として残し、削除の条件とチーム名の一意をサイトと同じ規則で検査する／4. 選手のプロフィールの更新は `RosterService`（名前・背番号・守備位置と同じ1回の `Team` 集約の保存）。

**推奨で進めたもの**（計画の 1〜10）: 登録画面は見るのも書くのも全体の編集権（持ち主・編集者）・「空」の定義は domain の関数（リーグはチーム0、チームは在籍・主将在任・試合0、球場は世界の球団を含めて本拠地0）・名前の一意は保存の前の確認と `IntegrityError` の訳しの2段・チーム名もワークスペースの中で一意（リポジトリで検査）・表示順は数値の欄で新しい行は末尾（列ソートなし）・本拠地はチームの側からだけ選ぶ・マイグレーションを足さない・ヘッダーの「登録」は `can_set_up`・空のワークスペースのダッシュボードに案内・プロフィールは同じフォームで目印があるときだけ更新。

**計測**（2026-10-25）: W2 の計測と同じ手順。開発 DB（main の 0059 まで）を `backup()` で写しを2つ作った（どちらも `integrity_check` ok・試合 3,480）。基点 1cd28f6 はマイグレーションが `0059_workspace`〜`0062` の番号で main の `0059_pennantplayerratings_potential` とぶつかるので、基点の写しは main の作業ツリーのコードで `migrate myapp 0058_pennantworld_draft_order_rule`（伸びしろの列を落とす）→ 基点の使い捨ての worktree で `migrate`。W3 の写しは W3 の先端でそのまま `migrate`（0060〜0063）。それぞれのコードで `pennant_create --league 1 --league 2 --year 2030 --seed 20261015 --owner admin --managed-team 1`（世界 1・リーグ2・球団12・選手966人）→ `pennant_advance --world 1 --to week`（6日・36試合）。写しの上だけで `s-sato`（staff でない）を球団 1 の担当にした。基点と W3 を交互に3回ずつ、未ログイン（`--workspace <key>`）・持ち主（`--as admin --world 1`）・担当（`--as s-sato`）で流した。中央値（応答 ms / SQL ms / クエリ）。写しと使い捨ての worktree は計測後に消した。

| 画面 | 基点・未ログイン | W3・未ログイン | 基点・持ち主 | W3・持ち主 | 基点・担当 | W3・担当 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ダッシュボード | 493/61/11 | 505/63/11 | 489/61/13 | 502/61/13 | 489/58/14 | 527/64/14 |
| チーム一覧 | 12/3/3 | 12/3/3 | 12/3/5 | 12/3/5 | 13/3/6 | 14/3/6 |
| 順位表 | 74/3/5 | 69/3/5 | 75/3/7 | 77/3/7 | 71/3/8 | 76/3/8 |
| 順位表(2026) | 68/3/5 | 69/3/5 | 73/3/7 | 70/3/7 | 69/3/8 | 69/3/8 |
| 選手一覧(野手) | 262/11/28 | 258/11/28 | 272/13/30 | 267/13/30 | 271/11/31 | 264/12/31 |
| 選手一覧(投手) | 259/11/28 | 257/11/28 | 253/11/30 | 258/11/30 | 251/11/31 | 269/11/31 |
| 選手詳細 | 92/4/16 | 88/4/16 | 88/4/18 | 89/4/18 | 88/4/19 | 92/4/19 |
| 選手検索 | 14/1/5 | 13/1/5 | 14/1/7 | 15/1/7 | 14/1/8 | 16/1/8 |
| リーグ詳細 | 116/6/6 | 103/6/6 | 104/6/8 | 110/6/8 | 108/6/9 | 113/6/9 |
| リーグ成績 | 149/7/9 | 138/7/9 | 143/7/11 | 146/7/11 | 140/7/12 | 148/7/12 |
| リーグタイトル | 70/9/8 | 63/8/8 | 65/8/10 | 70/9/10 | 69/8/11 | 71/9/11 |
| 試合一覧 | 107/15/7 | 100/14/7 | 101/14/9 | 102/15/9 | 100/15/10 | 109/15/10 |
| 試合詳細 | 528/41/18 | 485/37/18 | 492/37/20 | 493/37/20 | 495/39/21 | 512/40/21 |
| 戦力分析 | 39/6/9 | 36/6/9 | 37/6/11 | 39/6/11 | 40/6/12 | 38/6/12 |
| 戦力分析（起用マップ） | 28/6/9 | 30/5/9 | 28/5/11 | 30/6/11 | 29/6/12 | 28/5/12 |
| 戦力分析（FA取得） | 41/10/14 | 43/11/14 | 42/11/16 | 45/11/16 | 45/10/17 | 41/10/17 |

ペナント（持ち主 `admin`。基点 → W3）: ペナント一覧 27/3/16 → 30/3/16、世界ダッシュボード 154/3/53 → 167/4/53、世界の順位表 12/0/11 → 13/0/11、世界のリーグ詳細 15/1/12 → 15/1/12、世界のリーグ成績 116/0/17 → 122/0/17、世界のチーム一覧 9/1/9 → 9/1/9、世界の選手一覧 117/2/45 → 117/2/45、世界の試合一覧 14/1/13 → 14/1/13、世界の戦力分析 33/1/15 → 71/1/15、世界の球団ホーム 57/4/29 → 66/4/29。

- **クエリ数は全画面・3通りとも基点と同じ**（実データ16画面×3通り・ペナント10画面。3回とも同じ数）。W3 は今の画面の読み取りを変えていない。main の取り込み（#316）の分も差は無い（#303〜#314 はペナントの締める処理・ドラフト・成長の変更）。
- 応答時間は揺れの範囲（実データは同じ画面で ±10% ほど。SQL 時間も同じ）。目立つのは世界の戦力分析（33 → 71ms）だが、**`measure_pages` の1回目のアクセスの差**で、同じプロセスで同じ URL を8回描くと基点 34〜37ms・W3 33〜34ms で同じ。W3 T5 の後・取り込みの前（23189a9）でも同じく 61〜65ms に出るので取り込みの分ではない。1回目の差は、テンプレートの読み込みなどの最初の1回の手間がどの画面に乗るかの違い（測る順は同じだが、W3 で登録画面のテンプレートと URL が増えた）と見て、追っていない。

**漏れと規則を拾えることの確かめ**（変異。どれも戻した）: T1 は削除が規則を通らない 6件・`DjangoStadiumRepository` が範囲で絞らない 3件・チーム名の検査が範囲で絞らない 16件。T4 は目印を見ずにプロフィールを読む 1件・経路の整合を見ない 2件・プロフィールを捨てる 27件・年の無い順位を許す 4件・Enter の既定の送信を消す integration 3件と e2e 1件。T2 は門を外す 57件・削除のリンクを常に出す 3件・ヘッダーの導線を絞らない 15件・消せない行に削除のボタン 4件・ドメイン例外をフォームのエラーにしない 4件。T5 は削除の導線の検査を外す 7件・一括削除の差し替えを外す 6件・名前の検査（フォーム 2件・インライン 4件）・所属リーグの検査 1件・インラインの削除の検査 1件。T3 は移す先の枠の検査を外す 3件・試合のあるチームに所属リーグの欄 1件・担当者の数の案内 1件・チームのタブ（e2e）1件・世界の作成の名簿の検査 1件。

**ユーザーに確かめること**（#79 のコメント。どれも推奨で進めた／今のまま）
1. **数えてから消すまでの割り込み**（T1・T5）: 数えてから消すまでの間に別の要求が在籍や試合を書き込むと、空でないものを消しうる（管理画面では `NotDeletable` で 500。データは消えない）。SQLite は書き込みが直列なので実害は小さい。防ぐなら外部キーを `PROTECT` にして `ProtectedError` を訳す（マイグレーションと世界の削除の順序の確かめが要る）。推奨: 今のまま（PostgreSQL に移るときに見直す）。
2. **選手の編集画面のボタンの分け方**（T4）: プロフィール・基本情報の欄と、退団・主将の任命/解任・支配下登録のボタンが同じフォームにあり、欄を書き換えてからボタンを押すと入力は黙って捨てられる（データは失われない。名前・背番号は以前から同じ）。推奨: ボタンを別のフォーム（別の節）に分ける（A）／今のまま（B）。**→ 2026-10-25 ユーザーが A に決定。下の T7 で分けた。**
3. **表示順の入れ方の2通り**（T2・T5）: 管理画面はドラッグ、登録画面は数値（どちらも同じ `display_order`）。推奨: このまま（別の画面なので並べない規則には当たらない）。 **→ 2026-10-25 ユーザーが「登録画面もドラッグにし、編集画面の数値の欄は消す」に決定。下の T8 で変えた。**
4. **移す先のリーグの支配下枠**（T3）: 枠を超えるチームの所属リーグの変更は拒む形で実装した（推奨）。ほかの案: 移せるが警告／枠を見ない（`Team.move_to_league` だけを直せば済む）。

**W4 以降に残したもの**
- W4: ワークスペースの作成（空）・設定・削除・上限・招待・メンバー管理。担当者（`Team.managers`）と編集者の割り当ては W4 まで管理画面で行う。ヘッダーのワークスペースの切り替え（`base.html` のヘッダーは W3 で「登録」を足した）
- W6: ペナントの世界ごとの球場の複製の要否（決定 1。今は共有で、編集は世界にも映る）
- 要望が出たら: 選手の削除（誤って登録した選手の取り消し。在籍・試合の明細から参照されるので、消してよい条件の設計が別に要る）・過去の在籍と主将の在任歴の編集（2026-10-03 の Q6 で見送り）
- 上の「ユーザーに確かめること」1 の答えしだいの直し（2 は T7 で直した）
- epic を main に入れるとき: 上の「epic を main に入れるときに必ず行うこと」（W3 で足すものは無い）と、main を取り込むたびのマイグレーション番号の確認

#### T7 選手の編集画面のボタンを分ける（`fix/player-edit-action-forms`、2026-10-25）

上の「ユーザーに確かめること」2 の決定（A）。選手の編集画面（`player_edit.html`）で、入力欄（名前・背番号・守備位置・プロフィール）と「支配下登録する」「主将にする／主将を解任する」「退団にする」が同じ `<form>` にあり、欄を書き換えてからボタンを押すと入力が黙って捨てられていた。テンプレートだけの変更で、ビュー（`views.player_edit`。押されたボタンの name で分岐する）とサーバ側の振る舞い（何が保存されるか）は変えていない。マイグレーションは無い。

- 入力のフォームの送信ボタンは「更新する」1つだけにした。T4 で先頭に置いた見えない既定のボタン（`visually-hidden` の「更新する」）は、ほかのボタンが無くなり Enter でも「更新する」が押されるので消した。
- 操作のボタンは入力のフォームの外の「操作」の節（カード）に、それぞれ別の小さなフォーム（csrf つき・POST・同じ URL・今の name/value のまま）で並べた。送るのは押したボタンと、支配下登録の新しい背番号（`promote_number`）だけ。出し分け（育成選手だけ支配下登録・主将かどうかで指名／解任）は今の条件のまま。
- 支配下登録のフォームは別になったので `formnovalidate` を外し、新しい背番号の欄を `required` にした（ブラウザが空と表記の違いを先に止める。サーバ側の検査は今のまま domain）。欄に `label for` を付けた。
- FA 宣言の節は以前から別のフォームなので変えていない。

**テスト**: integration `test_player_profile_edit.py` の `DefaultSubmitButtonTest` を `ActionFormsTest` に置き換えた（描いた HTML で、入力のフォームの送信ボタンが name・value の無い1つだけ／操作のボタンがそれぞれ別のフォームで、送る欄が csrf と `promote_number` だけ。育成選手・主将・主将でないの3通り）。`test_the_other_buttons_keep_the_profile` は、目印なし（今の画面が送る形）と目印つき（T4 の前の画面を開いたままの人など）の両方で、支配下登録を足した4つのボタンがプロフィールを変えないことを見る（支配下の選手なので支配下登録はドメインの検査で止まる。通った経路は新しい `test_a_successful_promotion_keeps_the_profile` が育成選手で見る）。e2e `test_player_edit_enter.py`: Enter の検査はそのまま意味を保つ（入力のフォームのボタンが「更新する」だけになったことを実ブラウザで確かめる）。足したもの: 欄を書き換えてから「主将にする」を押すと、送られる欄が csrf とボタンだけで主将になり名前・よみがなは変わらない／新しい背番号の欄で Enter を押すと支配下登録になる。件数: domain＋integration 4,038（+1）、e2e 17（+2）。

**レビュー（ddd-boundary-reviewer）**: 重要な指摘なし。軽微のうち、支配下登録が通った経路のプロフィールの検査（上の新しいテスト）と、`ActionFormsTest` の送る欄を部分集合でなく一致で見ること（支配下登録のフォームから `promote_number` が消えたら落ちる）を直した。残したもの（このブランチで生じたものではない）: 退団のボタンの `onclick="return confirm('{{ player.name }} …')"` は、属性値の HTML のエスケープが JS として読む前に戻るので、選手名にアポストロフィがあると確認の文が壊れ、編集者が入れた名前が JS として動く余地がある（`escapejs` で直す。別の Issue にする）。

**変異の確かめ**（戻した）: 「主将にする」を入力のフォームの「更新する」の前に戻す → integration 2件（`ActionFormsTest` の育成・主将でない）・e2e 2件（Enter が主将の指名になりよみがなが保存されない・ボタンが入力欄を送る）が落ちる。

**計画からのずれ**: なし（`formnovalidate` を外して `required` にしたのは、フォームを分けたことで要らなくなった分の後始末）。

#### T8 登録画面の一覧のドラッグで表示順を並べ替える（`feature/setup-drag-ordering`、2026-10-25）

上の「ユーザーに確かめること」3 の決定（2026-10-25）: リーグ・チームの表示順は登録画面の一覧のドラッグで決め、編集画面の表示順の数値の欄は消す。新しい行は今どおり末尾。マイグレーションは無い（`makemigrations --check`）。

- domain: `setup.apply_order(items, ordered_ids)`（**識別子の並び**に 0, 1, 2 … を `reorder()` で振る。id の集まりがちょうど一致しない＝欠け・余り・重複・範囲の外の id・空なら `InvalidOrder` で何も変えない。黙って補わない）と文言の `ORDER_MISMATCH`。例外 `InvalidOrder`。リポジトリの Protocol に `LeagueRepository.save_display_orders`・`TeamRepository.save_display_orders`（表示順だけを書く。範囲の外の行があれば `*NotFound` で何も書かない）。
- infrastructure: `_write_display_orders`（範囲で絞った行を読む1本と、変わった行の `bulk_update` 1本。行数によらない）。集約の `save` を通さないので名前・所属・在籍の検査は走らない（表示順は集約の不変条件と無関係。値の検査は `apply_order` が `reorder()` で通す）。
- application: `LeagueSetupService.reorder_leagues(ids)`・`reorder_teams(league_id, ids)`。「今の行を読む → `apply_order` → `save_display_orders`」を1つのトランザクションにするため、`AtomicBlock`（`transaction.atomic`）を組み立て口から注入した（前例: ペナントのサービス）。`update_league`・`update_team` から表示順の引数を外した（呼ぶのはサイトのビューだけで、管理画面は別の経路＝インラインと一覧の `display_order` の欄。管理画面のドラッグは変えていない）。**所属リーグを変えたチームは移す先のリーグの末尾に入れる**（作成と同じ。古い表示順のままだと移す先の並びの途中に紛れ込む）。
- presentation: URL 2本（`_w(..., login=True)`）`setup/leagues/reorder/`（`setup_leagues_reorder`）・`setup/leagues/<league_id>/teams/reorder/`（`setup_teams_reorder`）。POST だけ（GET は一覧へ戻す）、門は `_requires_set_up`、チームの並べ替えは URL のリーグが範囲の外なら 404。本文は `order` の繰り返し（id の並び）。合わない並び・数でない値は、何も書かずに一覧を `messages.error` つきで出し直す（200。フォームのエラーと同じ扱い）。書けたら一覧へ戻す。フォームから表示順の欄と `with_display_order` を消した。
- テンプレートと JS: `leagues.html`・`teams.html`（チームはリーグごとの表に分けた。`regroup`）の行に `data-order-id`、表ごとに保存のフォーム（隠し欄 `order` に今の並び・「並び順を保存する」「元に戻す」。`hidden` で始まる）。`myapp/static/myapp/js/setup-sortable.js`（素の JS。管理画面の `admin-inline-sortable.js` と同じく HTML5 のドラッグ＆ドロップだけ。新しい依存なし。React のアイランドにしない）が行を動かし、隠し欄を識別子の並びで書き直し、開いたときと並びが違うときだけ保存のフォームを出す。ほかの表（ほかのリーグ）の行へは落とせない。JS が動かなければ案内と保存のフォームは hidden のままで一覧は読める。表示順の数値の列は消した（並びが表示順）。見た目は `theme.css` に管理画面と同じ約束（落下先は上辺の線）で足した。

**計画からのずれ**

- 保存は非同期（fetch）ではなく、管理画面と同じく「並び順を保存する」で送るフォームにした（CSRF・権限・エラーの出し方がほかの登録画面と同じ形で済み、JS が動かない環境でも壊れない）。
- 所属リーグの変更でチームを移す先の末尾に入れる（上）。表示順の欄が無くなったので、移した後の位置をサービスが決める必要が出た。
- `test_setup_screens.py` が増えるので、並べ替えの画面のテストは `test_setup_ordering.py`（新）に置いた。

**テスト**: domain `test_setup_rules.py` の `ApplyOrderTest`（id の順に振る・チームも同じ・欠け／余り／入れ替え／重複／重複で数だけ合う／空は何も変えない・行も id も無ければ何もしない）。integration `test_league_setup_service.py` の `ReorderTest`（往復・ほかのリーグのチームは変わらない・表示順のほかはどの表も変わらない・合わない並び10通りは `InvalidOrder` で全表の行が変わらない・B のリーグは `LeagueNotFound`・リポジトリも範囲の外の行を拒む・クエリ数が行数によらない）と、移したチームは移す先の末尾・移さない編集は表示順を変えない。`test_setup_ordering.py`（新。一覧の HTML（行の id の順・保存のフォームが隠れて今の並びを持つ・JS の読み込み・数値の列が無い・チームはリーグごとのフォーム）・並べ替えの往復と保存の知らせ・合わない並び9通り（数でない値・本文なしを含む）は 200 で理由を出し行が変わらない・B とペナントのリーグの URL は 404・GET は一覧へ・書き込みのクエリ数が行数によらない）、`test_setup_screens.py`（編集画面に表示順の欄が無く編集は表示順を変えない・門の検査に並べ替えの2本（担当は 403・門はサービスの前に拒む・編集者は書ける・未ログインはログインへ）、`test_setup_teams.py`（編集の欄・送られた表示順を読まない）、`test_workspace_isolation.py`（`URL_TABLE`・`WRITE_BODIES` に2本、持ち主の本文が実際に書く確かめに2本）、`test_workspace_permissions.py`（「リーグの並べ替え（POST）」「チームの並べ替え（POST）」の列）、`test_cross_workspace_ids.py`（B のリーグ・チームを混ぜた並べ替えは 200 で行が変わらず、B のリーグの URL は 404）。e2e `test_setup_ordering.py`（新。リーグの行をドラッグして保存すると表示順が変わる・チームはリーグの中で動き、ほかのリーグの行へは落ちない・元の並びに戻すと保存のボタンが消える）。

**変異の確かめ**（どれも戻した）: `apply_order` の集まりの一致の検査を「部分集合なら通す」に緩める → domain 4件・integration 14件、並べ替えの2本の門（`_requires_set_up`）を外す → `test_setup_screens` の門の検査5件・権限の表7件・isolation の部外者の POST、`save_display_orders` を範囲で絞らない → 1件、移したチームを末尾に入れない → 2件、JS が隠し欄を書き直さない → e2e 2件。

**レビュー（ddd-boundary-reviewer）**: 重要な指摘なし。残した軽微な指摘: (1) `AtomicBlock` を `application/pennant_season.py` から import している（ペナントと無関係な登録のサービスがペナントのモジュールに依存する。中立な場所へ移す余地）。(2) 所属リーグを変えたときの移す先の末尾は、範囲の全チーム（ロスターなし）を読んで Python で絞り、トランザクションの外（同時の作成で表示順が重なりうるが、名前の順で並ぶだけ）。(3) 失敗時に一覧を 200 で出し直すので、再読み込みで同じ POST がもう一度送られる（`InvalidOrder` で何も書かない）。(4) `_reorder` は `DomainError` を広く受けて文言にする（今の経路で届くのは `InvalidOrder` と、同時の削除による `*NotFound` だけ）。

件数: domain＋integration 4,059（T7 の 4,038 より +21）、e2e 20（+3）。

### W4（#80）

計画は #80 の計画（2026-10-25。epic 6e13a25 と照合）。T1 は 6e13a25 から作り、コミットの前に epic の先端 b1c027d（W3 T7）の上に載せ直した。決定（2026-10-25、すべて推奨）: 1. メンバーでなくなった人（担当に変えた人を含む）が作った世界は読むだけ（世界の書き込みに「今もこのワークスペースの全体の編集権」を足す）／2. 世界が1つでもあるワークスペースは消せない（確認画面に世界の一覧と削除への導線。持ち主がほかの人の世界も消せる）／3. サービスは `WorkspaceService`（作成・設定・削除）と `WorkspaceMemberService`（招待・受諾・役割・除名）の2つ／4. CLAUDE.md の差分は T7 で入れる。

#### T1 domain・永続化・サービス（`feature/workspace-members-service`、2026-10-25）

画面はまだ使わない（T3・T4 の画面が使う）。マイグレーションは `0064_workspace_invitation`（スキーマだけ。データの移行は無い）。作業の時点で main は 0059 まで（epic の 0063 の次でよい。main の取り込みのときに付け直す）。

- domain（`domain/workspace.py`）:
  - `Workspace` 集約に `editor_ids`・`team_assignments`（利用者 → 担当チームの id。メンバーの画面の単位が人なので「人 → チーム」の向き）。不変条件（1人の役割は1つ: 持ち主は編集者にも担当にも入らない・編集者は担当に入らない・担当は1チーム以上）は作成と各操作で検査する。操作は `rename`・`change_visibility`・`role_of`・`is_member`・`make_editor`・`assign_teams`・`change_role`（メンバーでなければ `MemberNotFound`。新しく入れるのは受諾だけ）・`remove_member`・`accept(invitation, user_id, *, now)`・`ensure_assignments_within(team_ids)`。
  - `members_from_rows(owner_id, editor_ids, manager_rows)`: 表の行から集約のメンバーを組み立てる。管理画面で付けた重なった行は、役割の優先（持ち主 > 編集者 > 担当）で上の役割だけを残す（読むときに落とさない）。リポジトリとメンバーの一覧の参照クエリが同じ関数を使う。
  - `InvitedRole`（`EDIT_ALL`・`EDIT_TEAMS`。表示名と `granted_role`。ORM の choices の出典）、`Invitation`（`issue`・`is_expired`・`ensure_teams_within`。ダイジェストは16進64文字、`EDIT_TEAMS` はチーム1つ以上・`EDIT_ALL` はチームなし）、`INVITATION_TTL`（7日）、`invitation_digest`（SHA-256）、`INVITATION_UNAVAILABLE`（無い・期限切れ・使用済みの同じ文言）。
  - `WorkspaceQuota(max_owned_workspaces)`（`can_create_workspace`・`ensure_can_create_workspace`・`limit_message`）。`WorkspaceAccess.can_change_settings()`（持ち主）。例外 `WorkspaceLimitReached`・`InvalidInvitation`・`MemberNotFound`。
  - リポジトリの Protocol `WorkspaceRepository`（`find_by_id`・`add`・`save`・`count_owned_by`）・`InvitationRepository`（`add`・`find_by_digest`・`delete(workspace_id, invitation_id)`・`delete_expired(workspace_id, now)`）。**削除は T2 で足す。**
- infrastructure:
  - `WorkspaceInvitation`（`workspace` CASCADE・`token_digest` unique・`role`・`teams` M2M・`expires_at`・`created_at`。作った人の列は持たない）。分類表は「メンバー」（担当チームの M2M は「受け持つ球団」と同じ例外）。
  - `DjangoWorkspaceRepository`（範囲を取らない台帳）: 読むのは行・編集者の行・**そのワークスペースの実データのチーム（`teams_in(WorldScope.real(ws))`）の担当の中間表の行だけ**。`save` は検査（行がある・`ensure_assignments_within`）をすべて済ませてから、名前と公開範囲・編集者の行・範囲のチームの担当の行を、今の行と比べた差分だけ書く（ほかのワークスペースのチーム・世界の球団の担当には触れない）。
  - `DjangoInvitationRepository`（範囲を取らない台帳）: `add` は担当チームを招待のワークスペースの実データのチームに限る（`Invitation.ensure_teams_within`）。`find_by_digest` は担当チームを同じ範囲で読み、`EDIT_TEAMS` で残ったチームが無ければ使えない招待。取消と期限切れの掃除はワークスペースの id で絞る。
  - 参照クエリ `DjangoWorkspaceManagementQuery(workspace_id)`（`members()` は3本・`invitations(now)` は2本。人数・招待の数によらない）と `DjangoInvitationPreviewQuery()`（トークンから引く台帳。使えるかは domain の `Invitation` で判定。開いた人が既にメンバーか）。
  - `infrastructure/workspaces.py` に `new_workspace_key()`（管理画面の `_new_workspace_key` から移した。管理画面も使う）と `new_invitation_token()`（`secrets.token_urlsafe(32)`）。
- application（`application/workspaces.py`。更新だけ）: `WorkspaceService`（`can_create`・`limit_message`・`create`（数える → 上限 → 保存を1つのトランザクション）・`change_settings`）、`WorkspaceMemberService`（`issue_invitation`（期限切れの掃除と発行を1つのトランザクション。トークンを返すのはここだけ）・`revoke_invitation`・`accept_invitation`（引く → 集約の `accept` → 保存 → 招待を消す、を1つのトランザクション。消せなければ保存も戻る。key を返す）・`change_role`・`remove_member`）。権限はサービスでは見ない（画面の門）。参照の Protocol `WorkspaceManagementQuery`・`InvitationPreviewQuery`、DTO `MemberRow`・`InvitationRow`・`IssuedInvitation`・`InvitationPreview`。
- presentation: `build_workspace_service()`・`build_workspace_member_service()`・`build_invitation_preview_query()`（範囲を取らない。`WORKSPACE_LEDGER_BUILDERS`）・`build_workspace_management_query(workspace_id)`（新しい分類 `MEMBER_BUILDERS`）。`tests/helpers.py` が再輸出する。`settings.WORKSPACE_MAX_OWNED`（環境変数、既定3。`.env.example`・Wiki「本番公開」）。

**計画からのずれ**

- 削除の確認の件数（DTO `WorkspaceContents`）と `workspace_deletion_blocker` は T2・T5 に回した（削除と一緒に作る）。
- `InvitationRepository.delete_expired` はワークスペースの id を取る（計画は引数を書いていない）。全ワークスペースの招待を消す口を範囲を取らない台帳に作らないため。期限切れの招待は、そのワークスペースで発行するまで残る（使えないので害は無い。一覧にも出ない）。
- 既にメンバーの人（持ち主を含む）の受諾は `InvalidInvitation`（「すでにこのワークスペースのメンバーです。」）で、招待は使わずに残る。役割を変えるのは持ち主の役割の変更だけ。
- `WorkspaceService.can_create` を足した（`/` の作成フォームの代わりに上限の案内を出すかの判定。T3 が使う）。
- 担当チームがすべて消えた（またはワークスペースの外へ出た）`EDIT_TEAMS` の招待は、受諾でも中身の表示でも「無い招待」と同じ扱い。
- 重なった行は保存のときに消える（持ち主の担当の行・編集者の担当の行。権限に何も足さない行）。設定の変更（名前だけの保存）でも消える。

**テスト**: domain `test_workspace.py`（設定・不変条件・操作・`members_from_rows`）、新 `test_invitation.py`・`test_workspace_quota.py`、`test_workspace_access.py` の真理値表に「設定の変更」の列。integration 新 `test_workspace_service.py`（作成・上限3と4つ目・settings からの上限・設定・集約の往復・重なった行・範囲の中だけの書き込み・範囲の外の拒否・招待の発行と受諾と1回だけ・期限切れ・既にメンバー・取消・役割の変更・除名・参照のクエリ数・招待の中身）、`test_workspace_isolation.py` の `CHECKS` に4つ、`test_wiring.py` の分類（`WORKSPACE_LEDGER_BUILDERS`・`MEMBER_BUILDERS`・`WORKSPACE_LEDGERS`・`MEMBER_QUERIES`・`TAKES_NOTHING`・`ProtocolConformanceTest`・依存の検査・`WorkspaceLedgerUsersTest` の名前と使ってよい場所に `infrastructure/repositories.py`）、`test_world_isolation.py` の分類表に `WorkspaceInvitation`。件数: domain＋integration 4,120（epic の先端 b1c027d の 4,038 より +82）、e2e 17（画面は変えていないが流した。変わらず）。

**変異の確かめ**（どれも戻した。`test_workspace_service`・isolation の `CHECKS` を流した）: 保存が担当の行を範囲で絞らない → 5件、保存が範囲の検査をしない → 4件、受諾で招待を消さない → 2件、上限を検査しない → 2件、読みが担当を範囲で絞らない → 12件、招待の担当チームを検査しない → 5件、期限切れの掃除が他所の招待も消す → 2件、取消がワークスペースで絞らない → 2件、メンバーの一覧が範囲で絞らない → 3件、招待の中身がチームを範囲で絞らない → 1件（isolation）。domain: 持ち主を編集者・担当に入れられる → 2件、期限切れを受諾できる → 2件。

**レビュー（ddd-boundary-reviewer）**: 重要な指摘なし。軽微を直した: (1) `find_by_digest` が担当チームを範囲で絞らず、中身の表示（絞る）と食い違っていた → 同じ範囲で読む（isolation に再発防止）。(2) 招待の範囲の検査がリポジトリに直書き → `Invitation.ensure_teams_within`。(3) `MemberRow.team_ids` のコメント（名前順 → 表示順）。残したもの: `AtomicBlock` を `pennant_season` から import している（共通の置き場所が無い。今のまま）、管理画面の編集者・担当のインラインは集約の検査を通らない（読むときに落とさず保存で消える設計。T6 で管理画面の書き込みをどうするか決める）。

**T2 以降への申し送り**

- T2: `WorkspaceRepository.delete` と `WorkspaceService.delete`。招待の表と担当チームの M2M（`WorkspaceInvitation.teams` の中間表）も削除の順に入れる。削除の確認の件数の DTO。
- T3: `/` の作成フォームは `build_workspace_service().can_create(user_id)` と `limit_message`。設定の門は `can_change_settings()`。ドメイン例外（`InvalidWorkspace`・`WorkspaceLimitReached`）をフォームのエラーに。
- T4: 発行の直後だけ `IssuedInvitation.token` からリンクを作る。招待の画面は `build_invitation_preview_query().preview(token, user_id, timezone.now())`（`InvalidInvitation` は 404。`is_member` なら受諾のボタンを出さない）。受諾は `accept_invitation(token, user_id)` が key を返す。役割の変更と除名は `MemberNotFound`・`InvalidWorkspace` を画面のエラーに。担当チームの選択肢はこのワークスペースの実データのチーム。
- T6: 管理画面からワークスペースの台帳の組み立て口を使うときは、`test_wiring.py` の `WorkspaceLedgerUsersTest.ALLOWED` に `myapp/admin.py` を足す。

#### T2 ワークスペースの削除（`feature/workspace-delete`、2026-10-25）

画面（`workspace_delete`）は T5。マイグレーションは無い。

- domain: `workspace_deletion_blocker(world_count)`（`domain/workspace.py`。世界が1つでもあれば日本語の理由、無ければ None。決定 2）。リポジトリの削除と削除の確認の DTO が同じ関数を通す。
- `WorkspaceRepository.delete(workspace_id)`（Protocol と `DjangoWorkspaceRepository`）: 1つのトランザクションで、検査（行がある → 世界が無い（`NotDeletable`）→ ほかの範囲にも在籍がある選手がいない（`InvalidWorkspace`。選手の id を出す）→ ほかのワークスペース（とその世界）の行が、消す行を空にできない外部キーで指していない（`InvalidWorkspace`。下の「迷子の参照」））をすべて済ませてから、
  1. 消す行を指す SET_NULL の参照を外す（`_WORKSPACE_DETACHED_REFERENCES`: `Team.home_stadium`・`PennantWorld.managed_team`・`PennantClubPlan.closer`。`_raw_delete` は on_delete を働かせないため。管理画面の直書きで、ほかのワークスペースのチームがこの球場を本拠地にしていることがある。`WorkspaceCase` の `_plant_stray_stadiums` がその形）、
  2. `_SCOPE_ROWS_CHILD_FIRST`（`_WORLD_ROWS_CHILD_FIRST` から改名。世界の削除と共有）を `WorldScope.real(ws)` で流す、
  3. 範囲の選手を500人ずつ、
  4. `_WORKSPACE_ROWS_CHILD_FIRST`（担当の中間表 → 招待の担当チームの中間表 → チーム → リーグ → 球場 → 招待 → 編集者 → ワークスペース）を、ワークスペースの id への道で絞って消す。どれも `_raw_delete`（collector を通さない）。
- application: `WorkspaceService.delete`（リポジトリに渡すだけ。検査と削除はリポジトリの1トランザクション）。参照 `WorkspaceManagementQuery.contents()` と DTO `WorkspaceContents`（リーグ・チーム・選手・試合の数と `WorkspaceWorldRow` の世界の一覧、`deletion_blocker`）。5本で読む。
- 管理コマンド `workspace_delete --workspace <key> [--yes]`: 件数を出して `yes` の入力を求める（入力が無い実行は取りやめ）。世界が残っていれば世界の一覧と `pennant_delete` の案内を出して止まる。Wiki「本番公開」の「ワークスペースを消す」。
- 在籍の無い選手はどの範囲にも属さないので消さない（試合の明細に出ていても。明細は消える）。管理画面の「選手」から消せる。
- **迷子の参照**（ほかのワークスペースの行がこのワークスペースの行を指す。管理画面の直書きでしか起きない。2026-10-25 ユーザー決定、#80）: 本拠地・世界の受け持つ球団・編成の抑えは空にして消す（上の 1.）。**試合の相手など空にできない参照は、消す前の検査で拒む**（`InvalidWorkspace`。「ほかのワークスペースの試合（id=…）がこのワークスペースのチームを指しているため削除できません。運営者に連絡してください。」）。見る参照は手で並べず、全アプリのモデル（中間表を含む）のメタ情報から「消すモデル（削除の順）を指す外部キーのうち、空にして外す3つ以外」を導く（`_workspace_incoming_references`）。参照ごとに「消す行を指していて、自分は消す行でない行」を1本で引く（47本。行数によらない）。消す行の条件は消すときと同じ（`_workspace_deleted_rows`）。
- 範囲を取らない台帳の組み立て口（`build_workspace_service`）を管理コマンドが使うので、`test_wiring.py` の `WorkspaceLedgerUsersTest.ALLOWED` に `workspace_delete.py` を足した（`--workspace` の key で指したワークスペースを消すのが仕事）。

**計画からのずれ**

- 計画の順（選手 → チーム → … → 担当の中間表 → ワークスペース）を、中間表を指す先より先に消す順に変えた（担当・招待の担当チームの中間表をチームより前に）。
- SET_NULL の参照を外す処理を足した（計画に無い。外さないと、迷子の参照がコミットのときに外部キーの検査で落ちる）。外すのはほかのワークスペースの行でも本拠地・受け持つ球団・抑えの列だけで、Django の `on_delete=SET_NULL` と同じ結果。
- 計画の `NotDeletable` 相当には既存の `NotDeletable` を使った（リーグ・チーム・球場の削除と同じ）。
- 空にできない迷子の参照を消す前に拒む検査を足した（レビューの指摘を受けたユーザー決定。計画に無い）。

**計測**（開発 DB を `sqlite3` の `backup()` で写し、0060〜0064 を当てて既定のワークスペースを消した。写しは消した）: 試合 3,480・選手 3,975・アプリの全表で 871,044 行 → 0 行。迷子の参照の検査を足す前は **1.6〜2.5秒**（2回。もう1つのエージェントのテストと並行）、クエリ 45本（選手の 500人ずつが8本）。検査を足した後は **2.8秒**（うち検査 1.05秒）、クエリ 92本（検査が47本）。時間のほぼすべてが SQL。タイムアウト（60秒）に十分な余裕があるので、T5 の画面からも同期で消してよい。

**テスト**: 新 `test_workspace_delete.py`（`WorkspaceCase` の B から世界を消し、メンバー・招待と、まだ行の無い表（打席まわり・主将・編成の行・表彰・日本一・イニングスコア）の行を足して B を消す）: B の行（**分類表から**数える。削除の順は使わない）が全表で0行になり、ほかの行は1行も変わらない（多対多の中間表を含む全表・利用者）／B に行の無い表が無いこと（消えたことを確かめられない表を作らない）／分類表のモデル（世界を除く）と多対多の中間表がすべて削除の順に入っている／消す表を指す参照は、消す表から（指す側が先）か SET_NULL として外す一覧にある／世界が残る A は `NotDeletable`・B の選手が A や A の世界にも在籍があれば `InvalidWorkspace`・無い id は `WorkspaceNotFound`・A の試合の相手が B のチーム／A の打撃成績が B にしか在籍の無い選手／A の招待の担当チームに B のチーム（id は招待の id）／A の世界の日程の相手が B のチーム（ペナントのリーグは workspace が空）なら `InvalidWorkspace`（どれも全表が変わらない）／全アプリの外部キーのうち消すモデルを指すものは、空にして外すか消す前の検査の対象のどちらか（漏れると落ちる）／迷子の本拠地・A の世界の受け持つ球団と編成の抑えが B を指す迷子は外れる／在籍の無い選手は残る／クエリ数が小さいワークスペースと同じ／`contents()` の件数と5本／管理コマンド（`--yes`・`yes`・ほかの答え・入力なし・世界の一覧・`--workspace` 必須）。domain `test_workspace.py` に `DeletionBlockerTest`。

**変異の確かめ**（どれも戻した。`test_workspace_delete` を流した）: 削除の順から `Captaincy` を外す → 5 FAIL・6 ERROR（分類表・参照の順・行の比較。teardown の外部キーの検査も落ちる）、招待の担当チームの中間表を外す → 5 FAIL・6 ERROR、在籍がまたがる選手の検査を外す → 2 FAIL・2 ERROR、世界の検査を外す → 1 FAIL・1 ERROR、SET_NULL の外しを止める → 1 FAIL・1 ERROR、抑えを外す道を書き間違える（`closer__stints__team__league__world__workspace_id`）→ 1 FAIL・1 ERROR。迷子の参照の検査を外す → 4 FAIL・4 ERROR、試合の相手を検査の対象から外す → 3 FAIL・2 ERROR（外部キーの網羅の検査を含む）、検査で消す行を除かない（自分の行も迷子とみなす）→ 5 FAIL・7 ERROR。

**レビュー（ddd-boundary-reviewer）**: 重要な指摘なし。軽微を直した: SET_NULL を外す3本のうち行で確かめていたのが本拠地だけだった → 受け持つ球団と編成の抑え（在籍をたどる道）のテストを足した。(1) 在籍以外の参照がワークスペースをまたぐ迷子（A の試合の相手が B のチームなど）が事前の検査で拾えず、コミットの外部キーの検査で `IntegrityError` になる → ユーザー決定（2026-10-25）で、消す前の検査で `InvalidWorkspace` にした（上の「迷子の参照」）。検査を足した差分のレビューも重要な指摘なし。軽微を直した: 中間表の行で出す id を持ち主の行（招待）の id に、世界の行（null の道）が B を指す形のテスト、網羅のテストに PROTECT の参照。残したもの: 理由の「運営者に連絡してください」は T5 の画面（持ち主）向けで、管理コマンドでは運営者本人が読む（T5 と合わせて決める）。(2) 迷子の本拠地を黙って外す（Wiki に書いた。コマンドに外した件数を出すのは任意）。(3) 削除のトランザクションはリポジトリの `@transaction.atomic`（チームの削除と同じ）。

**T5 への申し送り**

- 削除の確認の画面は `build_workspace_management_query(ws).contents()`。`deletion_blocker` があれば削除のボタンを出さず、理由と世界の一覧（世界の削除の画面へのリンク）を出す。削除は `build_workspace_service().delete(ws)`（`NotDeletable`・`InvalidWorkspace` は画面のエラーに）。
- 持ち主がほかの人の世界も消せるようにする（決定 2）のは T5 の世界の書き込みの判定と一緒に。

#### T3 作成と設定の画面・ヘッダー（`feature/workspace-create-settings`、2026-10-25）

epic の 3e954a4（W4 T1）から作り、コミットの前に epic の先端 2f68b4f（W3 T8）の上に載せ直した（衝突なし）。マイグレーションは無い。

- 作成: `/` のログイン時の一覧の下に作成フォーム（`WorkspaceForm`。名前・公開範囲。選択肢は `Visibility.choices()`、既定は非公開）。
  上限に達していれば（`build_workspace_service().can_create(user_id)` が偽）フォームの代わりに `limit_message` の案内だけ。
  送り先は `/workspaces/new/`（`workspace_create`。ワークスペースの外、`require_POST`）。成功したら新しいワークスペースの
  ダッシュボード（W3 の空の案内）へ。入力の誤り（`InvalidWorkspace` は名前の欄）と上限（`WorkspaceLimitReached` はフォーム全体）は
  `/` を 200 で描き直す。未ログインの POST はログインへ（戻り先は `/`）。
- 設定: `/w/<key>/settings/`（`workspace_settings`。`login=True`）。門は `_requires_owner(request, WorkspaceAccess.can_change_settings)`
  （解決済みの権限だけで DB を読まない。持ち主以外は GET も POST も 403。T4・T5 のメンバー・削除の画面も同じ門に別の判定を渡す）。
  初期値は `WorkspaceContext` の名前と公開範囲（読み直さない）。保存したら同じ画面へ。見出しとタブは `myapp/workspace/_tabs.html`
  （今は「設定」だけ。メンバー・削除は画面を作るときに足す。押しても開けないタブを出さない）。
- ヘッダー: ログインしていれば、ワークスペースの中で名前のところが Bootstrap のドロップダウン（ダッシュボード・持ち主には
  「ワークスペースの設定」・自分のワークスペースへの切り替え・`/`）。未ログインは今どおり名前のリンク。コンテキストプロセッサが
  `can_manage_workspace`（`can_change_settings()`。DB を読まない）と `my_workspaces`（`views.my_workspaces`。`list_for` を
  描くときにだけ評価する `SimpleLazyObject`）を渡す。台帳の組み立て口の名前は `views.py` の外に出さない（`WorkspaceLedgerUsersTest`）。

**計画からのずれ**

- `/` のクエリは3本から4本（セッション・利用者・一覧・持っている数）。計画の見込み（`/` は今と同じ3クエリ）からずれた。
  申し送りのとおり `can_create(user_id)` で数える（一覧の行から持ち主の行を数えて済ませると、上限の判定の数え方が作成の検査
  （`count_owned_by`）と2か所になる）。ワークスペースの数にはよらない。
- `NoBareReverseTest.ALLOWED` に `workspace_create` を足した（ワークスペースの外の画面で、行き先は作ったばかりの別のワークスペース。
  今の要求の key が無いので `_url` では組めない）。
- `test_the_workspace_is_read_once_per_request` は、ヘッダーの切り替えの一覧（コンテキストプロセッサの `my_workspaces` を空に差し替える）を「今の要求の
  ワークスペースの読み」から除いた。
- 上限を超える作成の送信（別のタブで先に作ったなど）は、フォームのエラーではなく、`/` をフォームの代わりに上限の案内で描き直す（200。
  何も作らない）。上限に達した画面にはフォームを出さない、に揃えた（指示の「4つ目の POST は 200 でフォームのエラー」からの小さなずれ）。
- 権限の表は、W3 T8 と衝突しないよう既存の `CHECKS` に列を足さず、同じ役割で別の表（`WORKSPACE_CHECKS`。設定の導線・GET・POST・作成）にした。

**テスト**: integration 新 `test_workspace_screens.py`（作成フォームと既定の非公開・作成後のダッシュボード・入力の誤り・上限で
フォームが消え4つ目は 200 のエラー・settings からの上限・編集者として入っている分は数えない・未ログインの POST・`/` のクエリ数・
設定の往復・空の名前・持ち主以外は 403・未ログインはログインへ・ヘッダーの切り替えと設定のリンク（持ち主だけ）・一覧は1回だけ読み
未ログインと API では読まない・クエリ数がワークスペースの数によらない）、`test_workspace_permissions.py` に `WorkspaceSettingsPermissionTest`
（公開・非公開 × 8つの役割）、`test_workspace_isolation.py` の `URL_TABLE`・`WRITE_BODIES`・`ID_FREE_WRITES`・持ち主の書き込みの空振りの検査に
`workspace_settings`、`header_pages()` に設定。
件数: domain＋integration 4,162（epic の先端 2f68b4f の上。T1 の 4,120 に W3 T8 の分と、この段階の +21）、e2e 20（変わらず）。

**変異の確かめ**（どれも戻した。`test_workspace_screens`・`test_workspace_permissions`・`test_workspace_isolation` を流した）: 設定の門を外す → 69件、
設定のリンクを全員に出す → 10件、門を `can_set_up`（編集者も通す）にする → 3件、上限でもフォームを出す → 2件、作成の既定を公開にする → 1件、
一覧を遅延させずに読む → 1件（最初は API の要求で確かめるテストにしたが、API はテンプレートを描かずコンテキストプロセッサも呼ばれないので
生き残った。コンテキストプロセッサを直接呼んで、渡すときは0本・回したときに1本のテストに替えた）。

**レビュー（ddd-boundary-reviewer）**: 重要な指摘なし。軽微を直した: (1) `WorkspaceLedgerUsersTest` の docstring に、コンテキストプロセッサは
views の `my_workspaces` を通して読むことを書いた。(2) 1要求1回の読みの検査が SQL の注釈名で一覧を除いていた → `my_workspaces` を差し替える。
(3) `header_pages()` の docstring に、ログインした人のヘッダーにはほかの自分のワークスペースの入口も出ること（検査の利用者はワークスペースを
1つだけ持つ人にする）を書いた。(5) 上限の例外を描かれないフォームのエラーに積んでいた → 積まない。残したもの: (4) 作成の成功の文言は
フォームの名前を写す（ドメインの正規化は前後の空白を除くだけで一致する）。

**T4 以降への申し送り**

- T4・T5: メンバー・削除の画面は `_requires_owner(request, WorkspaceAccess.can_manage_members)`・`(…, WorkspaceAccess.can_delete)` を通し、
  `myapp/workspace/_tabs.html` にタブを足し、`workspace_tab` を渡す。ヘッダーのメニューに出すなら `can_manage_workspace` と同じく
  コンテキストプロセッサから（DB を読まない）。
- 計測（T7）: ログインした人はワークスペースの中の全画面でヘッダーの一覧の +1。`/` はログインした人で4本（一覧・持っている数）。
