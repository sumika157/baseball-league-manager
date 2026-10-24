# 設計: ポストシーズン（CS と日本シリーズ）

> 状態: 実装中（統合ブランチ `epic/postseason`）。親 Issue [#39](https://github.com/sumika157/baseball-league-manager/issues/39)。
> 設計の出典は #39 の実装計画のコメント（2026-10-12）と、同じ日のユーザーの決定のコメント。段階ごとの実装計画は sub-issue（S1 は [#238](https://github.com/sumika157/baseball-league-manager/issues/238)）。
> このファイルは epic の間の**決定と実測の記録**で、`epic/postseason` を main に入れるとき（S5）に Wiki（「ペナントモード」「ペナントモードの仕組み」「試合がすべての出典」「成績と順位の見方」「画面の歩き方」「アーキテクチャ」）へ吸収して削除する。

## 1. 要点

- ペナントの世界のシーズン終了の後に、上位の球団が勝ち抜くポストシーズン（CS と日本シリーズ）を足す。試合は公式戦と同じエンジン・同じ `Game` で作る。
- **ポストシーズンの試合は公式戦の成績・順位・タイトル・通算・FA の日数に数えない**（NPB と同じ）。
- 保存するのは「試合の区分（`Game.stage`）」と「世界の方式（`World.postseason_format`、S3）」だけ。どのシリーズの第何戦か・いまのステージ・次の対戦は保存せず、結果から毎回導く（`season.py` の「進行の状態は持たない」と同じ）。

## 2. ユーザーの決定（2026-10-12）

| 項目 | 決定 |
| --- | --- |
| 3リーグ以上の日本シリーズ | 各リーグの CS の勝者全員で、勝率の順に種を付けた勝ち抜きのトーナメント（2の累乗に満たない分は上位がシード） |
| 方式 | 世界ごとに3つから選ぶ: NPB 方式（新しい世界の既定）／優勝球団で日本シリーズ／なし。**既存の世界は「なし」** |
| ポストシーズンの個人成績 | **表を出す**（S4）。公式戦と同じ参照クエリに区分の条件を渡して集計し、保存しない |

## 3. 段階

| 段階 | 内容 | 状態 |
| --- | --- | --- |
| S1 | 試合の区分（`GameStage`・`Game.stage`）と、公式戦の集計からポストシーズンを外す | 実装済み（#238） |
| S2 | 勝ち上がりの規則（domain の純粋な関数）・`final_order`・リーグの優先順・エンジンの延長の上限 | 実装済み（#239） |
| S3 | 進行（`PennantPostseasonService`）・`World.postseason_format`・締める処理の検査・管理コマンド | 実装済み（#240） |
| S4a | 画面から進めて締められる最小（勝ち上がりの表・GM ホームのカード・進めるボタン・作成フォームの方式・世界の終了の直し）と実測 | 実装済み（#241） |
| S4b | 方式の設定の画面・ポストシーズンの個人成績の表 | 未着手（#280） |
| S5 | 歴代・球団史への表示（#105 の後）・Wiki への吸収とこのファイルの削除 | 未着手 |

## 4. 段階ごとの記録

### S1 試合の区分と集計の分離（2026-10-15）

**決めたこと**

- `GameStage`（`domain/value_objects.py`）: `REGULAR = "公式戦"`・`POSTSEASON = "ポストシーズン"`。`Game.stage` と `GameHeader.stage` は**既定値を持たない必須の引数**（渡し忘れを型検査と例外で捕まえる。範囲の `WorldScope` と同じ考え）。呼ぶ側（スコアブック・試合の登録・シミュレーション・仮想データの投入）はすべて区分を明示する。ORM の列は `CharField`（choices は Enum から）で、マイグレーション `0055_game_stage`（main の取り込みで番号を付け直した。#168 段階B の `0048_pennant_release_plan` とぶつかって 0048 → 0049、#204 段階A・#248 の 0049〜0051 とぶつかって 0049 → 0052、#108 の 0052〜0054 とぶつかって 0052 → 0055） は AddField だけ（既存の試合は既定値で全部公式戦。backfill なし）。
- 試合を読む入口（`infrastructure/scoping.py`）は区分を**必須のキーワード引数**で受け取る: `games_in(scope, *, stages)`・`games_in_worlds(ids, *, stages)`。明細（打撃・投球・守備）の行から見た条件は `game_rows_condition("game", scope, *, stages)`、範囲で絞らない集計（選手・球団の id で絞る）は `stage_condition("game", stages)`。区分は `StageFilter`（`REGULAR_ONLY` / `POSTSEASON_ONLY` / `ALL_STAGES`）で、「None なら全部」の形は作らない（`WorldScope` と同じ）。
- 成績の集計の関数（`repositories.py` の `batting_totals`・`pitching_totals`・`game_counts`）は区分 `stages` を**必須の引数**にし、関数の中で区分の条件を合成する。読む明細の持ち主も必須の引数 `within` で、範囲（`WorldScope`。その世界の全選手）か選手の id の並び（選手の行は世界ごとに別）のどちらか（`LineOwners`）。どちらも無い形（全世界を読む）は型の上で書けない。`games` は年・期間・球団などの追加の条件だけ。`games: Q` を必須にするだけでは `Q()` を渡すと黙って全区分になり、型で保証できない。チーム集約の選手の通算（`_RosterData`）は範囲を持たずに選手の id で絞っていて、区分を入れ忘れると黙って混ざる形だったため。
- 分類（一覧は #238）: 公式戦の集計（成績・順位・タイトル・規定の試合数・試合一覧・年と月の選択肢・選手の試合・期間の集計・オフの出場機会・戦力分析の起用・FA の日数・選手の通算・`GameRepository` の一覧）は**公式戦だけ**。試合そのもの（`find_by_id`・保存）・世界の今日・直近の登板（疲労）・試合の見どころ（id で引く）・世界の削除・管理画面と道具は**全区分**。ポストシーズンだけを読む箇所は S1 には無い（S3 の `PostseasonQuery`、S4 の成績表）。
- application の参照の Protocol は変えない（区分はインフラが決める）。S4 のポストシーズンの成績表は、参照クエリに区分を受け取る口を足して作る（ユーザーの決定3）。
- スコアブックの上書き（`record_scorebook`）は今の試合の区分を引き継ぐ（`GameHeader.stage`）。実データの試合は常に公式戦で、スコアブック・管理画面・`seed_virtual_games` は区分を出さない。
- 検査は `tests/integration/test_postseason_isolation.py`: 極端な成績（20-7、全部本塁打）のポストシーズンの試合を世界に直接足して、世界の参照画面（`world_urls()` の全部と GM ホームの期間の集計）の HTML と、公式戦の参照クエリ・リポジトリの答えが**変わらない**ことを見る。区分を1か所外す変異（通算・戦力分析の起用・オフの出場機会を `ALL_STAGES` にする）で、そのテストが落ちることを確かめた。同じファイルの静的な検査が、`world_condition("game…")` の直接の記述、試合と明細のモデルを `.objects` で読むのに区分を指定しない関数（書き込みと1試合に結び付く読みは理由つきの許可リスト）（許可リストはクラス名つきの関数名）、範囲だけの条件 `game_rows_in_world` を成績の集計の入口（`_line_rows`）以外で使うことを落とす。限界は、関数の単位で判定すること・`_default_manager` 経由と逆向きの関連（`player.game_batting` など）を見ないことで、そこは画面と参照クエリの比較に任せる。

**実測**（開発 DB のコピー。実データ 3,480 試合＋2リーグ12球団の世界で 2026 年の公式戦を消化（858 試合）。合計 4,338 試合）

- マイグレーション `0055_game_stage`（main の取り込みで番号を付け直した。#168 段階B の `0048_pennant_release_plan` とぶつかって 0048 → 0049、#204 段階A・#248 の 0049〜0051 とぶつかって 0049 → 0052、#108 の 0052〜0054 とぶつかって 0052 → 0055）: 2.4 秒（SQLite は既定値つきの AddField で表を作り直す）
- 実データの主要画面（`measure_pages`）と世界の主要画面（GM ホーム・順位表・リーグ・タイトル・成績一覧・試合一覧・歴代・選手一覧・選手ページ・試合詳細・戦力分析の4タブ）: **クエリ数はすべて同じ**。応答と SQL 時間は前後で揺れの範囲（同じ DB を変更前のコードで2回測った値の幅に収まる。例: 世界の選手一覧 342〜413ms → 333ms、実データのリーグ成績 241〜268ms → 227ms）
- シーズンを締める処理: 0.55〜0.75 秒（変更前）→ 0.51 秒（変更後）、1週間を進める（36試合）: 1.06〜1.34 秒 → 1.02 秒。結果（引退・戦力外・新人の人数、作った日程）は前後で同じ
- → **索引は足さない**。区分の条件は `myapp_game` の行に対する `stage = '公式戦'` の1つで、明細からの JOIN は既に試合の行を引いている。ポストシーズンの試合が増える S3 の後に、8リーグの世界で測り直す

### S2 勝ち上がりの規則とエンジン（2026-10-15）

**置き場所**

- `domain/pennant/postseason.py`（新規）: `PostseasonFormat`（方式の選択肢の唯一の出典。`NPB`「CS と日本シリーズ（NPB 方式）」・`CHAMPIONS`「優勝球団で日本シリーズ」・`NONE`「なし」）・`PostseasonStatus`・`SeriesStage`・`SeriesRules` と定数（`FIRST_STAGE`・`FINAL_STAGE`・`FINAL_STAGE_TWO_WIN_ADVANTAGE`・`JAPAN_SERIES`）・`Series`（1シリーズの状態。勝利数・決着・次の試合）・`PostseasonBracket.derive(方式, リーグの最終順位, リーグの優先順, 最後の公式戦の日, 結果)`。設計の `SeriesState` は、シリーズの名前と開始日も持つ `Series` 1つにまとめた。
- 入力の最終順位（`LeagueFinish`）は**全順序**で渡す。全順序は `race.py` の `final_order`（同率を `season_champion` の規定で上から1球団ずつ決める。決めた球団を除いた残りで対戦勝率を数え直す）。首位は `league_champion` と必ず同じになることをテストで突き合わせた。
- リーグの優先順は `world.py` の `league_priority(league_ids, *, year, start_year)`（#198 のドラフトでも使う予定）。
- `simulate_game(..., max_innings=MAX_INNINGS)`。`None` は延長の制限なし。安全装置として 50 回で `InvalidGame`（点の入らない乱数源で無限に回らないため）。9 未満と 50 を超える値は入口で `ValueError`。ポストシーズンの延長の上限は `postseason.py` の `POSTSEASON_MAX_INNINGS = 12`（公式戦の `MAX_INNINGS` とは別の規定として持つ）。
- 例外 `InvalidPostseason`（`domain/exceptions.py`）。

**決めた値（日付の定数。`postseason.py`）**

| 定数 | 値 | 意味 |
| --- | --- | --- |
| `DAYS_BEFORE_POSTSEASON` | 3 | 最後の公式戦の日から最初の回（FS。無ければファイナル、`CHAMPIONS` なら日本シリーズ）の第1戦まで |
| `DAYS_BETWEEN_CS_STAGES` | 2 | FS の**予定の**最終日（3日目）からファイナルの第1戦まで（1日休み）。FS が2試合で終わっても動かない |
| `DAYS_BEFORE_SERIES_ROUND` | 4 | 前の回の予定の最終日（CS は試合数の上限、日本シリーズは第7戦）と実際の最後の試合の遅い方から、日本シリーズの各回の第1戦まで |

- 日本シリーズの日割りは第1戦から 0・1・3・4・5・7・8・9 日目（第8戦は第7戦の翌日）、第9戦からは 11・13・15… 日目（移動日を1日置く）。本拠地は第1・2・6・7・8戦が上位（リーグの優先順で先）、第3〜5戦が下位、第9戦から下位・上位…と交互。
- 年をまたがないことの確かめ: 8リーグ・最後の公式戦が 9/30 で、CS を全部最後まで・日本シリーズの全7シリーズを最長（7引分のあと第14戦）にしても、最後の試合は 12/26。実際の公式戦の最終日（9/8〜9/15）ならさらに2週間早い。それでも年をまたぐ日付を導いたら `InvalidPostseason`（`next_day()` と `played()` が投げる。S3 は進める前にこれで止まる）。
- **S4 への注意**: 年をまたぐ日付の検査は次の試合を導くとき（`next_day()`・`played()`）だけに効く。`Series.date_of`・`scheduled_last_day` は検査しないので、勝ち上がりの表に予定日を出すときは年をまたいだ日付を返しうることに気をつける。また、スコアブックの入力フォーム（`presentation/forms.py`）は回の上限として `MAX_INNINGS = 12` を自分で持っている（エンジンの `MAX_INNINGS` の複製）。延長の制限の無い試合（日本シリーズの第8戦以降）を画面で扱うときに、出典を一本にするか上限を外すかを S4 で決める。

**設計から決めた・広げたこと**

- **打ち切りを下位の側にも広げた**: 残り試合を全部上位が勝っても下位の勝利数を上回れない時点で下位の勝ち（例: ファイナルで0勝3敗2分＝アドバンテージ込み1勝3敗・残り1試合）。残りの試合で結果が変わらないため。NPB の条文は上位の側だけを書いている。
- **2勝のアドバンテージの条件は、2球団のリーグのファイナル（1位 vs 2位）にも当てる**（相手が FS の勝者でなく2位になるだけ。**一般化**）。
- 2球団以下のリーグしか無い世界では FS の回が無く、ファイナルが最初の回になる（最後の公式戦の3日後）。FS のあるリーグと無いリーグが混ざる世界では、ファイナルは全リーグ同じ日（FS の日程の後）に始まる。
- 種の付け方は勝率の高い順（並べばリーグの優先順）で、標準の組み合わせ（8枠なら 1-8・4-5・2-7・3-6）に置く。空き枠の相手（シード）は上位の種。日本シリーズの「上位」（第1戦の本拠地）は種ではなくリーグの優先順で決める（設計どおり）。
- 方式 `NPB` で球団が1つしか無い世界・`CHAMPIONS` で1リーグの世界は、状況が `NONE`（対戦が組めない）。
- 結果の検査: 結果を日付の順に再生し、日ごとに「導いた次の日」と一致すること・その日の対戦（ホームとビジターの向きまで）にあることを確かめる。同じ日の一部の対戦だけ結果がある状態は許す（`next_day()` はその日の残りを返す）が、その後に別の日の結果があれば食い違い。延長の制限が無い試合（日本シリーズの第8戦以降）に引分の結果が来たら拒む。導出の入力は、リーグの優先順に同じリーグが2回ある・同じリーグ id の最終順位が2つある・同じ球団が2つのリーグにある、のどれでも拒む。

**エンジンの再現性**: `max_innings` を足す前のエンジンで、6球団・28日（84試合・引分4）の打席列のハッシュを取り、既定値の経路でビット単位で一致することを `tests/domain/test_simulation_max_innings.py` が見張る。`None` では12回で引分だった試合の12回までの打席列が同じまま、延長で決着する。

### S3 進行と締める処理（2026-10-15）

**置き場所**

- 方式: `World.postseason_format: PostseasonFormat`（domain。既定は `NONE`。選択肢の外の値は `InvalidWorld`）と `PennantWorld.postseason_format`（CharField・choices は `PostseasonFormat` から・既定「なし」）。マイグレーション `0056_pennantworld_postseason_format` は AddField だけ（**既存の世界は「なし」**。main の取り込みで 0050 → 0053 → 0056 と付け直した）。`World` の docstring は「作ったときに決まる事実と、オーナーが変えられる設定」に直した（#198 と共有の直し）。
- 世界の作成: `PennantWorldService.create_world(_with_schedule)` に `postseason_format`（省くと「なし」）。`pennant_create --postseason {npb,champions,none}`（既定 `npb`＝新しい世界の既定）。**画面の作成フォームはまだ方式を渡さない（「なし」の世界ができる）**。フォームの選択（既定 NPB）は S4。
- シミュレーションの共通部分: `application/pennant_simulation.py` の `DaySimulator`（1回の「進める」の間の登録候補・`resolve_club`・直近の登板を持ち、`play(day, home, away, *, stage, max_innings)` で1試合を作る）。`pool_of`・`pitching_history` もここへ移した。乱数の識別は `game_key(stage, day, home, away)`: 公式戦は切り出す前と同じ `<日付>:<ホーム>:<ビジター>`、ポストシーズンは `post:` を前に付ける。
- `simulate_game` は区分 `stage` を**必須のキーワード引数**で受け取る（エンジンの中の `REGULAR` の決め打ちを消した。S1 の「呼ぶ側はすべて区分を明示」）。
- 勝ち上がりの材料: `PostseasonQuery`（`results(year)`・`last_regular_date(year)`。`values_list` で読み、集約は組み立てない）と、それを使って `PostseasonBracket` を導く `PostseasonBracketReader`（`application/pennant_postseason.py`）。最終順位は世界の範囲の `TeamApplicationService.get_league_standings(year)`（公式戦だけ）、同率のあるリーグだけ `tiebreak_for` の材料で `final_order`、リーグの優先順は世界の全リーグで `league_priority`。**方式が「なし」なら何も読まずに状況 `NONE`**（既存の世界の締める処理・GM ホームにクエリを足さない）。
- 進行: `PennantPostseasonService.advance(target, *, expected_today)`。範囲は `POSTSEASON_SCREEN_TARGETS`（`season.py`。`DAY`・`NEXT_MANAGED_GAME`・新しい `AdvanceTarget.POSTSEASON_END`）だけを受け付ける。公式戦の日程に `POSTSEASON_END` を当てると `dates_to_play` が `InvalidSchedule`（黙って「残り全部」にならない）。組み立て口 `build_pennant_postseason_service(world_id)`、勝ち上がりの読み取りは `views._postseason_brackets(world_id)`（進行と締める処理の両方が使う。`test_wiring.py` が依存と範囲を見る）。
- 締める処理: `PennantOffseasonService._check` の最後（最終シーズンの検査の後）に「その年のポストシーズンが `NOT_STARTED` / `IN_PROGRESS` なら `PostseasonNotFinished`（「Y年のポストシーズンがまだ終わっていません。」）」を1つ足しただけ。`close_option()` は理由を返す。`_state()` には触れていない。
- **戦力外の指名はポストシーズン中もできる**（ユーザーの決定 2026-10-15）: 指名できる条件（公式戦を終え、まだ締めていない・最終シーズンでない）を `_season_over_year` に分け、`_check`（締められる条件）はそれにポストシーズンの検査を足したものにした。「戦力外」タブ・`release_view`・`start_manual_releases`・`set_releases`・`reset_releases` は `_season_over_year` を、タブを出すかの判定は新しい `can_choose_releases()` を使う（それまでの `close_option().can_close` はポストシーズン中に偽になるため）。指名を使うのは締めるときだけで、ポストシーズン中に保存した指名は終わってから締めると使われる。
- 管理コマンド: `pennant_advance --to postseason-end`。`day`・`next-game` は公式戦の日程が尽きていればポストシーズンを進める（S4 の「進めるの URL を共有し、局面がシーズン終了ならポストシーズン」と同じ振る舞い）。`pennant_close_season` はポストシーズンが終わっていなければ `PostseasonNotFinished` のメッセージで止まる。

**進め方の決定**

- 1日ずつ `next_day()` で対戦を導いてシミュレーションし、`played()` で結果をメモリに足して次の日を導く。保存は最後に1回。どの範囲も、試合数が `MAX_GAMES_PER_ADVANCE`（150）を超える手前の日で止める（少なくとも1日は進める）。8リーグの最悪でも 1回で終わる見込み（実測は 109試合）。
- `NEXT_MANAGED_GAME` は自軍が試合をした日まで。自軍が出ない（敗退・不出場）なら終わりまで（公式戦の「自軍の未消化が無ければ残り全部」と同じ考え）。
- 局面がシーズン中なら `SeasonNotFinished`、締めた後（翌年の開幕前）なら `AlreadyClosed`。方式「なし」・終わったポストシーズンは何もせず空の結果（`PostseasonAdvanceReport`）。
- **ロック（#100）**: シミュレーションと `prepare_add_all`（検査と行の組み立て）はトランザクションの外。中では「世界の最後の試合日（全区分）と次の対戦の日」を読み直し、読み込みの前と違えば書かずに `AlreadyAdvanced`。公式戦の「対戦を先に消す」に当たる、ポストシーズンの二重の試合の防ぎ方。並行して進めたときに二重の試合を作らないことを、トランザクションに入る直前に別の「進める」を割り込ませるテストで確かめた。
- `expected_today` を渡すと、世界の今日が違えば何も作らずに `AlreadyAdvanced`（画面の二重送信。S4 の進めるボタンが使う）。

**設計から外した・広げたこと**

- 「公式戦を終えた年」の判定は domain の `season.regular_season_over_year` に1つ置き、ポストシーズンの進行・戦力外の指名（`_season_over_year`。最終シーズンと画面の年の検査を足す）・締める処理（さらにポストシーズンの検査を足す）が共有する。「締めてよいポストシーズン」は `PostseasonStatus.is_settled`（終わった・無い。S4 の `WorldContext.is_over` も使う）。勝ち上がりの読み取りは順位表の2メソッドだけの Protocol（`pennant_race.StandingsSource`）で受ける（#219 の分割で触る範囲を狭める）。`pennant_advance` は公式戦の後に week・month・season-end を打つと、ポストシーズンが残っていれば `--to postseason-end` を案内する。
- **`multi_season_check.py` は変えていない。** 設計は「締める前にポストシーズンを終わりまで進める」だったが、この道具は DB の世界を進めず、メモリの上で公式戦とオフの規則だけを回す（`plan_offseason` に渡す出場機会も能力も公式戦だけから決まる）。ポストシーズンを足しても結果が変わらないので、手を入れる理由が無い。DB の世界を締める道具（`pennant_close_season`）は上のとおり検査で止まる。
- `PennantPostseasonService` の `view(year)`・`option()`（勝ち上がりの表と GM ホームのカードの DTO）は画面と一緒に S4 で作る（S3 は進める処理と検査まで）。`WorldContext.is_over`（最終シーズンのポストシーズンが終わるまで世界の終了を出さない）も S4。
- 世界の一覧の DTO `PennantWorldRow` に `postseason_format` を足した（コマンドの出力と S4 の設定の画面が使う）。
- Wiki は S5 で吸収する方針のまま、**コマンドの例だけ** S3 で直した（「仮想データの投入」の「1シーズン進める → 締める」に `--to postseason-end` を足し、「アーキテクチャ」の管理コマンドの例と組み立て口の表に `postseason-end`・`--postseason`・`build_pennant_postseason_service` を足した）。`pennant_create` の既定が NPB 方式になり、例のままでは締められなくなるため。

**切り出しの前後の一致**: 切り出す前のコードで、2球団の世界の公式戦1シーズン（143日・11,038打席）の打席列・投手成績・時刻と観客数のハッシュを取り、`DaySimulator` に切り出した後のコードで同じハッシュになることを確かめた（`d882b21f…`）。以後は `test_pennant_advance.py` の再現性のテストと、`game_key` の形のテスト（`test_pennant_postseason.py`）が見張る。

**テスト**: `tests/integration/test_pennant_postseason.py`（2リーグ×3球団の世界。公式戦を1週間だけ進めて残りの対戦を消し、公式戦を終えた世界にする）。1日ずつ・自軍の試合ごと・終わりまで一度に進めて打席列まで同じ、FS → ファイナル → 日本シリーズで優勝が決まる、FS の第1戦は2位の本拠地、年と能力の年がずれない、終わったポストシーズン・方式「なし」は何もしない、範囲の検査、`expected_today`、並行して進めても二重の試合を作らない、ポストシーズンの前後で公式戦の集計（順位表・試合一覧・オフの出場機会・最後の公式戦の日）が変わらない、シーズン中は進められない、終わるまで締められない（何も書かない・`close_option()` の理由）→ 終われば締められる、ポストシーズン中も戦力外のタブが出て、保存した指名が締めるときに使われる（公式戦の途中は出ない・方式「なし」は締めるまで出る） → 締めた後は進められない、方式「なし」はすぐ締められる、管理コマンド。

**実測**（開発 DB のコピー。実データ 8リーグから分岐した 8リーグ48球団の世界と、2リーグ12球団の世界。どちらも NPB 方式で 2026 年の公式戦を消化した後）

| 項目 | 8リーグ | 2リーグ |
| --- | --- | --- |
| 公式戦の最終日 → ポストシーズン | 9/13 → 9/16〜10/31（30日） | 9/10 → 9/13〜10/4（14日） |
| 終わりまで進める（1回） | 3.52 秒・109 試合・264 クエリ | 0.73 秒・20 試合・68 クエリ |
| うち書き込みロック | 1.46 秒 | 0.27 秒 |
| 締める処理に足した勝ち上がりの導出 | 96〜110 ms・6 クエリ | — |
| シーズンを締める処理 | 1.96 秒（方式「なし」にした同じ世界は 1.78 秒） | 0.52 秒（S1 の「なし」で 0.51 秒） |

- 比べる基準: 同じコピーで作った 8リーグの世界の公式戦を `LIMIT`（144 試合）で進めると 3.37〜3.55 秒・ロック 1.23〜1.35 秒。ポストシーズンの1試合あたりのロックは公式戦と同じ水準（書くのは同じ `prepare_add_all` の行）。
- マイグレーション `0056`（当時の 0050）: 世界の行に AddField するだけで、Django の起動を含めて 2.7 秒（`PennantWorld` は数行）。
- 世界の主要画面（GM ホーム・順位表・リーグ・タイトル・成績一覧・試合一覧・歴代・選手一覧・選手ページ・戦力分析の FA・試合詳細）を、ポストシーズンの前後で測った: **クエリ数はすべて同じ**（GM ホーム 37〜38・順位表 8・成績一覧 14・試合一覧 10・選手ページ 27・試合詳細 21）。応答も揺れの範囲（例: 順位表 123 → 95ms、成績一覧 210 → 204ms）。ポストシーズンの試合の試合詳細も同じクエリ数（21）で開ける。
- 方式 NPB の世界のシーズン終了の GM ホーム（オーナー）は、締められるかの判定（`close_option`）に勝ち上がりの導出が加わる（約 100ms・6 クエリ）。方式「なし」の世界は 0 クエリ。
- → **`Game` の (year, stage) の索引は足さない**。ポストシーズンの材料の読み（結果・最後の公式戦の日）は2クエリで、導出の時間の大半はその年の順位表（公式戦の試合の読み込み）。画面のクエリ数も変わらない。

**S4 への申し送り**

- **画面からポストシーズンを進める手段は S4 で入れる（epic を main に入れる前に必須）。** S3 の時点では管理コマンド（`pennant_advance`）でしか進められず、NPB 方式の世界は画面だけではシーズンを締められない（画面から作る世界は S4 まで「なし」なので、いまは起きない）。
- 進めるビューは今の URL を共有し、局面がシーズン終了なら `build_pennant_postseason_service(world_id).advance(target, expected_today=...)` を呼ぶ。範囲は `POSTSEASON_SCREEN_TARGETS` から（ボタンの文言は `pennant_home._LABELS` に「ポストシーズンの終わりまで」を足す）。
- 作成フォームに方式の選択（既定 NPB）を足し、`create_world_with_schedule(postseason_format=...)` に渡す（いまは省いて「なし」）。設定の画面で方式を変えるときは、書き込みのトランザクションの中で「その年のポストシーズンの試合が無い」ことを `PostseasonQuery.results(year)` で読み直す。
- 方式を変えられるようにしたら、**進める側**のトランザクションの中の読み直し（`PennantPostseasonService._save`）にも方式を足す（計算中に方式が「なし」に変わっても、ポストシーズンの試合を書かない。DDD レビューの指摘）。
- 勝ち上がりの表・GM ホームのカードは `PostseasonBracketReader.bracket(world, year)` から DTO を作る（`PennantPostseasonService.view(year)`・`option()`）。GM ホームが `close_option()` と勝ち上がりの両方を呼ぶと導出が2回になるので、カードの出し分けは1回の導出で済ませる。

### S4a 画面から進めて締められる最小（2026-10-24。#241）

S4 は S4a（この段階）と S4b（設定の画面と個人成績の表。#280）に分けた（ユーザー決定 2026-10-24）。

**置き場所**

- 見るためのサービス `PennantPostseasonViewService`（`application/pennant_postseason_view.py`）を進めるサービスと分けた（`OffseasonViewService` の前例）。組み立て口 `build_pennant_postseason_view_service(world_id)`。`view(world, year)`（勝ち上がりの表）・`card(world, *, include_advance)`（GM ホームのカード）・`status(year)`（世界の終了の判定）・`years(world)`。S3 の申し送りの `PennantPostseasonService.view()`・`option()` は作らなかった（進める側に読むための依存を足さない）。
- その年の試合は `PostseasonQuery.games(year)`（id・日付・2球団・得点。1クエリ）で1回だけ読み、表の行と導出の両方に使う（`PostseasonBracketReader.bracket(world, year, results=...)`）。`results(year)` は `games` から写す（勝者の導き方は `PostseasonGameRow.result` の1か所）。表を開ける年は `PostseasonQuery.years()` と、公式戦を終えたいまの年。
- domain: `PostseasonFormat.description`（作成フォームの説明）・`PostseasonBracket.outcome_of(team_id)`（`PostseasonOutcome`。「日本一」・「◯◯敗退」・「出場なし」。勝ち残っていれば None。S5 でも使う）・`champion_label`（最後の回が日本シリーズなら「日本一」、1リーグは「CS 優勝」）・`pending_stage_names()`（まだ組めていない先の回の名前）・`_Plan.stage_name(index)`・`_Plan.is_participant`。
- 進めるボタンの文言は `pennant_home._LABELS` に `POSTSEASON_END`、選択肢は `postseason_advance_options(own_alive=)`（範囲と順序は `POSTSEASON_SCREEN_TARGETS`。「次の自軍の試合まで」は自軍が勝ち残っているときだけ。終わる日付と試合数は出さない）。
- 試合詳細は `GameDetail.stage`・`is_postseason`。

**決めたこと**

- **GM ホームでは勝ち上がりを1回だけ導く**: `card()` で導き、その状況を `close_option(postseason_status=)` と世界の終了の判定（`settled_context`）に渡す。`close_season()` は必ず自分で導き直す（表示にだけ使う）。
- **世界の終了（`WorldContext.is_over`）は、最終シーズンのポストシーズンが終わるまで偽。** 見出し（`get_context`）では導かない: 世界の全画面が通るので、終了した世界のどの画面でも勝ち上がりを導くことになるため（最初の実装でそうなっていた。DDD レビューの指摘）。終了を出す画面だけが状況を入れる（GM ホームはカードの状況・世界の一覧は `PennantWorldViewService.settle`）。`postseason_settled` が None のまま `is_over` を聞くと例外（`RuntimeError`。黙ってどちらかにしない）。一覧は、壊れた結果（`InvalidPostseason`）の世界を「終わっていない」として出し、一覧ごと落とさない。
- **両立しない操作は並べない**: ポストシーズン中（開始前・進行中）は締めるカードを出さない。終わったらカードは優勝の1行になり、締めるカードを続けて出す。進めるボタンと「戦力外はポストシーズン中も編成の『戦力外』で選べます」はオーナーにだけ（最終シーズンは代わりに「ポストシーズンが終わると、この世界は終了します」）。
- 進めるビューは URL を共有し、局面がシーズン終了なら `POSTSEASON_SCREEN_TARGETS` で検査してポストシーズンを進める（公式戦の途中の `postseason_end`、ポストシーズン中の `week` などは「その進め方はできません」）。進めた後は `?since=` を付けずに `#postseason` へ戻る。期間に公式戦が1つも無い `?since=` では「進めた結果」を出さない（まとめは公式戦だけを読むため）。
- 予定日は `next_day()` からだけ出す（`Series.date_of`・`scheduled_last_day` は画面に出さない。年をまたぐ日付を導いたら次の試合は出さない）。
- スコアブックのフォームの `MAX_INNINGS = 12` は一本化しない（実データの入力の上限。世界の試合は画面で直さない）。13回の試合詳細が開けることをテストで固定した。
- 作成フォームの方式は `RadioSelect`（送る値は `PostseasonFormat` の名前、既定 NPB）。説明は `PostseasonFormat.description`。

**実測**（開発 DB のコピー。実データ 8リーグから分岐した 2リーグ12球団・8リーグ48球団の NPB 方式の世界で公式戦を消化。中央値・3回）

| 画面 | 2リーグ | 8リーグ |
| --- | --- | --- |
| 勝ち上がりの表（開始前 → 進行中 → 終了） | 65 → 66 → 75ms・18 クエリ（3つとも同じ） | 127 → 141 → 145ms・13 クエリ（3つとも同じ） |
| GM ホーム（オーナー。終了） | 375ms・64q → 438ms・66q | 533ms・59q → 558ms・61q |
| GM ホーム（未ログイン。終了） | 274ms・37q → 386ms・50q | 355ms・37q → 443ms・45q |
| 最終シーズン・終了の GM ホーム（オーナー） | 360ms・52q → 384ms・65q | 554ms・52q → 694ms・60q |
| 最終シーズン・終了の順位表（ほかの画面の代表） | 36ms・8q → 30ms・8q | 127ms・8q → 132ms・8q |
| 世界の一覧（最終シーズンを終えた世界が1つ） | 140ms・17q → 286ms・29q | 143ms・17q → 340ms・24q |
| ポストシーズンの試合詳細（最も長い試合） | 176ms・21q | 589ms・21q |

- 「→」の左は変更前のコード（32b5422）、右は変更後。同じ DB のコピーで測った。
- 表のページのクエリ数はシリーズと試合の数によらず一定（N+1 でない。テストでも開始前と終了後のクエリ数が同じことを固定）。
- GM ホームはカード（勝ち上がりの導出 約 100ms・8〜13 クエリ）が足される。オーナーは締めるカードの導出（S3 で足した分）がカードの導出に置き換わるので、増えるのは 2 クエリ。
- 世界の一覧は、最終シーズンを終えた NPB 方式の世界ごとに導出が1回足される（約 150〜200ms）。1人の世界は5つまでで、終えた世界だけなので見送り。世界の数が増えて遅くなったら、終わったかの判定を軽い経路にする（例: 最後の試合が優勝を決めた試合か）を検討する。
- 実データの主要画面（`measure_pages`）: クエリ数はすべて同じ、応答は揺れの範囲（変更前後を交互に2回ずつ。例: 選手一覧（投手）374〜389ms → 331〜538ms、試合詳細 673〜732ms → 634〜851ms）。

**S4b への申し送り**

- 方式の設定の画面を足すと、過去の年の方式が今と違いうる。いまの表は**世界のいまの方式で導く**（`PostseasonBracketReader.bracket` が `world.postseason_format` を使う）ので、S4b の決定どおり「その年の試合から方式を導く」に替える。`years()` は試合のある年を返すので、方式を「なし」にした世界の過去の年も開けるようになる（いまは方式「なし」なら表の導出が状況 `NONE` で 404）。
- 世界バーの「ポストシーズン」のリンク・試合詳細の表へのリンクは `WorldContext.has_postseason`（いまの方式）で出し分けている。方式を変えられるようにしたら、過去の年の表へのリンクの出し方を見直す。
- 方式を変えるときの読み直し（S3 の申し送り: 進める側の `_save` の中でも方式を読み直す）はまだ入っていない。
- 個人成績の表は、公式戦と同じ参照クエリに区分の条件を渡して集計する（S1 の決定）。表のページ（`pennant/postseason.html`）にタブか下の区画として足す想定。
