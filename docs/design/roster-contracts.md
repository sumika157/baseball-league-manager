# 設計: 支配下／育成の契約区分と、FA・獲得経路

> 状態: 段階 E1（#126）・E2（#127）実装済み。epic [#125](https://github.com/sumika157/baseball-league-manager/issues/125)、全体の親 [#114](https://github.com/sumika157/baseball-league-manager/issues/114)。
> 統合ブランチは `epic/roster-contracts`。段階ごとの実測値と決定は末尾の「段階ごとの記録」に追記し、epic を main に入れるときに Wiki（`docs/wiki/`）へ吸収して削除する。

## 1. なぜ

- 戦力分析のデプス表に「総数・支配下・育成」を出したい。
- NPB の運用（支配下70人・育成は3桁の背番号・育成から支配下への昇格）に合わせたい。
- 後段で FA（国内／海外）の権利を扱うため、選手がどう入団・移籍したか（獲得経路）を持ちたい。

契約の区分も獲得経路も**試合から導けない入力の事実**なので、保存しても「同じ事実の出典を2つ作らない」規則には反しない
（勝敗・通算成績・順位のように集計で出せる値は、これまでどおり保存しない）。

## 2. 段階

| 段階 | Issue | 内容 |
| --- | --- | --- |
| E1 | [#126](https://github.com/sumika157/baseball-league-manager/issues/126) | 契約区分の値と検査（domain）、永続化、登録フォーム・昇格・バッジ・管理画面 |
| E2 | [#127](https://github.com/sumika157/baseball-league-manager/issues/127) | デプス表に総数・支配下・育成の列。`GameRecordingService` で育成選手の出場を拒否 |
| F1 | [#128](https://github.com/sumika157/baseball-league-manager/issues/128) | 獲得経路（`Stint.acquired_via`）とFA宣言（`FreeAgentDeclaration`） |
| F2 | [#129](https://github.com/sumika157/baseball-league-manager/issues/129) | 戦力分析の FA タブ |

## 3. E: 契約区分

### domain

- 値オブジェクト `ContractStatus(Enum)`: `REGISTERED="支配下"` / `DEVELOPMENTAL="育成"`。`labels()` / `from_label()` は `Position` と同じ流儀。**選択肢の唯一の出典**で、フォームも ORM の choices もここから作る。
- `Stint` に `signed_as`（加入時の区分。既定は支配下）、`promoted_year`（育成から支配下に上がった年）、`number_before_promotion`（昇格前の背番号）。
  - `contract_in(year)`: 支配下加入なら支配下。育成加入で `promoted_year <= year` なら支配下、それ以外は育成。
  - `contract_now`: 今の区分（昇格していれば支配下）。
  - 検査: `promoted_year` は育成加入のときだけ。加入年以上、退団年があれば以下。違反は `InvalidContract`。
- **背番号と区分（双方向）**: 今の区分が育成なら100以上、支配下なら99以下（`ContractStatus.ensure_number_fits`）。`Team` 集約が `add_player`・`change_player_number`・`promote_player` で検査し、移籍の受け入れ（`transfer_player`）と管理画面の在籍フォームも同じ関数を呼ぶ。
  - **`Stint.__post_init__` では検査しない。** 検査すると、検査を素通りして書かれた既存の行（`seed_virtual_players` の3桁の支配下）が、読んだだけで例外になるため。
  - 在籍の `number` は常に**最新の区分に合う番号**。昇格で背番号が変わるので、育成だった間の番号は `number_before_promotion` に移して残す。`number_before_promotion` は `promoted_year` があるときだけ持て、`promoted_year` があれば**必須**（過去の昇格を管理画面で手入力しても、育成だった年の番号が分かるように）。
  - 年ごとの番号は `Stint.number_in(year)`（昇格前の年は昇格前の番号）。**E2 の年度別のデプス表では `number_in(year)` を使う**（`number` は最新の番号なので、過去の年に使うと昇格後の番号が出る）。
  - 背番号の期間重複は、`Stint.number_periods()`（昇格した在籍は「昇格前の期間×昇格前の番号」と「昇格後の期間×今の番号」の2区間）と `shared_number()` で見る。管理画面の他人の在籍との照合がこれを使うので、「2015〜2023 に 50 を着けた人がいて、2025 に 50 で昇格した選手」を保存し直せる。
  - 昇格前の番号を持たせたのは、昇格で `number` を上書きすると、育成だった年の番号が消え、管理画面の期間重複検査と食い違うため。
- **支配下の上限**: `League.registered_player_limit`（既定は `DEFAULT_REGISTERED_PLAYER_LIMIT` = 70、None は無制限）。`Team.ensure_room_for_registered(limit)` が「支配下が1人増えても上限を超えないか」を、増える**前**に検査する（判定とメッセージの出典はここだけ。管理画面も呼ぶ）。数えるのは在籍中で今の区分が支配下の選手。呼ぶのは、支配下での選手追加・支配下としての移籍の受け入れ・昇格（昇格は常に）。育成の追加・育成の移籍受け入れは支配下が増えないので呼ばない。超えたときの例外は `RegisteredPlayerLimitExceeded`（`ensure_quota_not_exceeded` に例外の型を渡せるようにして、外国人枠と判定を共有）。
- `Team.add_player(..., contract=REGISTERED)` は既定値つき。ペナント epic の `fork_roster` とドラフトが既定値のまま呼んでも壊れない。
- `Team.promote_player(player_id, number, year)`: 在籍中の育成選手だけを支配下に上げ、同時に背番号を支配下の番号に変える。

### application

- 昇格は新しい `RosterService`（`application/roster.py`）。`TeamApplicationService` が約1,600行あるので足さない。組み立て口は `build_roster_service()`。
- 選手登録（`register_player`）と移籍（`transfer_player`）には区分の引数を足しただけ。移籍は指定が無ければ**移籍元の今の区分を引き継ぐ**。
- 参照: `BatterRow` / `PitcherRow` / `PlayerDetail` に `is_developmental`（今の区分が育成か）。

### infrastructure

- `PlayerStint.signed_as` / `promoted_year` / `number_before_promotion`、`League.registered_player_limit`（マイグレーション 0038）。
- backfill（0039）: 背番号が100以上の在籍を育成にする。スキーマと別ファイル。（`number_before_promotion` は昇格の記録があるときだけなので backfill では触らない）

### presentation

- 選手登録フォームに「契約区分」、選手の編集画面に「支配下登録する」（新しい背番号つき。担当者だけ）、選手一覧・個人ページに「育成」バッジ。
- 管理画面の `PlayerStintInline` / `PlayerStintForm` が区分・昇格年・上限をドメインの規則で検査し、`LeagueAdmin` に「支配下枠」。

## 4. 後段の概要

### E2

- 戦力分析のデプス表に総数・支配下・育成の列。
- `GameRecordingService` が、育成選手の試合出場（打席・登板）を拒否する。

### F1

- `Stint.acquired_via`: ドラフト／育成ドラフト／FA／トレード／自由契約からの獲得／新外国人／その他。None は不明。
- `FreeAgentDeclaration(year, kind)`: kind は国内／海外。**残留か移籍かは在籍から導く**（保存しない）。

### F2（FA タブ）

- 登録日数は**試合の初出場〜最終出場で推定し、保存しない**。
- 国内 FA は8シーズン（2007年以降のドラフトで入団した大卒・社会人は7。入団年では2008年以降）、海外は9、再取得は4。
- 1シーズン＝145日。145日未満のシーズンは合算する。

## 5. 宿題

### epic（`epic/roster-contracts`）を main に入れるとき

- `seed_virtual_players` は背番号1〜99を使い切ると**100〜999を支配下として作る**（`signed_as` の既定が支配下のため）。ペナント epic の大改修が終わってから、100以上は育成として作るか、背番号の割り当てを区分に合わせて直す。それまでは、seed で作った3桁の支配下は読めるが、区分と背番号が食い違うので、別の3桁の番号への変更は拒否される（管理画面で在籍を保存し直すときも同じ）。
- 開発 DB の既存データは 0039 で「3桁は育成」に直る。その後に seed を流し直すと、また3桁の支配下ができる。
- 既存のリーグは `registered_player_limit` が既定の70になる。70人を超えるロスターのチームは、選手の追加・移籍の受け入れ・昇格ができなくなる（既存の人数は直らない）。必要ならリーグごとに上限を変える。

### ペナント epic（#26）を main に入れるとき

- `fork_roster` が区分（`signed_as` / `promoted_year`）と獲得経路を写す。
- 1軍登録は支配下だけ。
- AI（編成）が育成選手を選ばない。
- ドラフトで入団した選手の区分（ドラフトは支配下、育成ドラフトは育成）。
- 仮想データで育成選手を生成するか。
- seed の背番号100以上の扱い（上と同じ）。

## 6. 段階ごとの記録

### E1（#126）

実測値と決定を追記する。

- 決定: 移籍は区分を引き継ぐ（指定が無ければ移籍元の今の区分）。引き継いだ先の背番号が区分に合わなければ拒否する。
- 決定: 管理画面の在籍の「加入時の契約区分」は空欄なら支配下として扱う（`from_year` と同様、入力を必須にしない）。
- 決定: 管理画面の上限検査は、いま登録しようとしている行だけを数える（同じ送信に複数の新規行があっても合算しない）。
- 実測: `measure_pages` は、マイグレーション未適用の開発 DB では新しい列が無く落ちるため E1 では実行していない。epic の統合前に、適用後の DB で実測する。
- 見送り（M3）: 外国人の登録枠は**育成も含めて数える**（Issue #114 の決定 E5）。E2 で育成選手の出場を拒否すれば出場枠側とは自然に合う。登録枠から育成を外すかは別 Issue で決める。
- 宿題（L1、[#145](https://github.com/sumika157/baseball-league-manager/issues/145)）: 移籍の受け入れで「区分と背番号の照合」をしているのが application（`TeamApplicationService.transfer_player`）になっている。`Team.accept_transfer(player, number, year, contract)` に寄せ、受け入れの検査（背番号の重複・区分・支配下の余地）を集約に集める案。ペナント epic の `fork_roster` と合わせて整理する。
- 宿題: 支配下の上限を守るのが呼び出し側任せになっている（`ensure_room_for_registered` を呼び忘れた経路は黙って超える）。上限を引数で受けて集約の中で検査する形に、#145 と一緒に寄せる。
- 宿題: `promote_player` の year 引数で過去の年を渡すと、ドメインは今在籍中の番号しか見ないので、管理画面の期間照合と食い違いうる。今は画面から年を渡す経路が無い。

### E2（#127）

- 決定: **その年の区分・背番号の出典を1つにした。** `Stint.contract_in` / `Stint.number_in` の中身を、在籍の値だけで判定できる関数
  （`ContractStatus.in_year(signed_as, promoted_year, year)`・`entities.jersey_number_in(...)`）に切り出し、`Stint` もそれを呼ぶ。
  参照クエリは在籍の値（`ContractFacts`: 最新の背番号・加入時の区分・昇格年・昇格前の番号）をそのまま DTO に載せ、年ごとの導出は
  application がこの2つの関数で行う（`Stint` を組み立て直さない・application に別の判定を書かない）。
- 決定: デプス表・入退団・起用マップの背番号は `number_in(年)`。入退団の表には育成バッジを足した（起用マップは育成が出場できないので足さない）。
- 決定: 人数の数え方は `DepthPlayer.is_developmental` から DTO の property（行・表・チーム全体）が数える。テンプレートでは数えない。
  リーグの支配下の上限は、在籍を読む既存のクエリに `select_related("team__league")` を足して取る（**クエリ数は増えない**。
  在籍が1人もいない年は上限を読まず None で、見出しは「/ 70」を出さない）。
- 決定: 育成選手の出場拒否は `GameRecordingService._ensure_no_developmental_players`（外国人の出場枠と同じ場所・同じ流儀）。
  出場した選手（ラインアップ・打席の打者と投手）を `team_of_players` でチームに振り、`Team.ensure_not_developmental_in(選手, 年)`
  （試合の年に育成なら `InvalidContract`、選手名つき）。その年にそのチームに在籍していない選手は対象にしない。
  同じ年に在籍が複数あるときは、1つでも支配下なら支配下として通す。
- 決定: 編集画面の候補（`get_game_edit_data`）は、その試合の年に育成の選手を外す。**ただし、すでにその試合のラインアップにいる選手は残す**
  （区分を持つ前に出場した過去の試合で、外すと打順・打席の表示が壊れるため）。
- 宿題（`seed_virtual_games`）: 在籍している選手をそのまま出場させ、**区分を見ない**（`bulk_create` で書くので集約の検査も素通りする）。
  今の仮想データは全員が支配下だが、0039 の backfill は「背番号100以上は育成」に直すので、`seed_virtual_players` が作った3桁の選手
  （上の宿題）は、開発 DB では育成扱いになり、試合に出た記録を持つ育成選手になりうる。epic を main に入れるとき、seed 側で育成の選手を
  出場させない（または検査を通す）ようにする。ペナントの epic が seed を大きく変えているので、E2 では触っていない。
- 見送り: 管理画面や過去データで、すでに試合に出た選手を後から「育成」に直す操作は止めていない（試合の記録は保存し直さない限り検査されない）。
- 実測: 戦力分析のクエリ数は E1 と同じ（選手を20人足しても増えない）ことをテストで確認した。`measure_pages` は E1 と同じ理由
  （マイグレーション未適用の開発 DB）で流していない。

### F2（#129）

- 決定: 規則は新規 `domain/services/free_agency.py`（定数・`EducationPath`・`service_seasons`・`fa_outlook`・`fa_order`）。**宣言の年の一覧は引数 `declared_years` で受ける**純粋関数。
  宣言は種別（国内／海外）を問わず、最後の宣言の翌年から数え直して国内・海外とも4シーズンとした（F1 の `FreeAgentDeclaration.kind` を区別するかは配線時に決める）。
  根拠: NPB 原文は「一度FAの権利を行使し、その後NPB組織のいずれかの球団と契約した選手は、出場選手登録が4シーズンに達したときに『海外FA』となる資格を取得する」。
  海外FAの資格は国内の移籍もできるので、国内・海外とも4シーズンで取得として表示する。
- 決定: 大卒・社会人の7シーズンは原文が「2007年以降の**ドラフトで**入団」。ドラフトは秋・入団は翌年なので、定数は `COLLEGE_RULE_FROM_DRAFT_YEAR = 2007`（ドラフトの年）で持ち、
  入団年（`debut_year`）－1 と比べる（入団年＝ドラフトの翌年とみなす。入団年が2008年以降なら7）。
- 決定: 記録より前が欠けて取得済みと判定したときは `FaStatus.acquired_year_is_latest` を立て、「取得済み（遅くとも2018年）」と出す（数えた分で足りた年は上限）。
- 決定: 育成だった年の除外はドメイン（`fa_outlook(developmental_years=...)`）。application は在籍の契約から「その年の在籍がすべて育成か」を `ContractStatus.in_year` で判定して年の集合を渡すだけ。
- 決定: 学歴が不明のときの国内FAは、数えたシーズンが8以上なら取得済み（7か8か決まらないが8なら確実）、入団が2007年以前なら学歴によらず8シーズン、それ以外は不明。
- 決定: 説明文の「145日」は `TeamAnalysis.service_days_per_season`（ドメインの `SERVICE_DAYS_PER_SEASON`）から出す。
- 限界（前提）: 「記録のある最初の年」は DB 全体の最小年で、全リーグ・全球団が同じ年から記録されている前提（今の開発データでは成り立つ）。
- 決定: 145日未満の年は合算し、145日に達するごとに1シーズン。**残りは次へ持ち越す**。145日以上の年は余りを持ち越さない。
- 決定: 登録日数の推定は「年ごとの初出場〜最終出場（両端含む）」で上限は丸めない（145日以上は1シーズンなので丸めても結果は同じ）。キャリア通算なのでチームは問わない。
- 決定: 「記録が無い年」は、**入団年が試合の記録のある最初の年（DB 全体）より前**のこととした。選手個人の試合の無い年は0日で、記録の欠落とは見ない。
  記録より前が欠けていても、数えた分だけで必要シーズンに達していれば「取得済み（遅くとも n 年）」。達していなければ「不明（あと最大 n シーズン）」。
  最短ではなく**最大**なのは、数えられない年が足されるほど残りが減るため。
- 決定: 学歴が不明なら国内FAだけ不明。海外FAは学歴に依らない（9シーズン）ので数える（外国人選手は学歴が空のことが多く、海外まで不明にすると情報が失われる。セルフレビューで変更）。入団年が不明なら数えられない。
- 決定: 育成だった年は、その年に在籍があって**そのすべてが育成**のとき育成の年とし、数えない（`ContractStatus.in_year` を呼ぶ。他球団の在籍も見る）。
- 決定: 材料は `TeamAnalysisQuery.load_service_history`（FA タブのときだけ呼ぶ。打撃・投球の明細の選手×年の min/max の2本・在籍1本・記録の最初の年1本の計4本で、選手数に比例しない）。
  ほかのタブのクエリ数は増えない。並びは国内FAで、取得済み → 残りが少ない順 → 上限つきの不明 → 不明。
- **宿題（G #130）**: ペナントの世界の中にシーズンを置く段階で、記録の最初の年を世界ごとに分ける（`ServiceHistory.records_from_year` の取り方）。
- **宿題（F1 が入った後）**: `application/team_analysis.py` の `_fa_rows` が `declared_years=[]` を渡している。`Player.fa_declarations` の年を渡す配線と、
  入団の経路（FA で入団した選手の扱い）を足す。宣言を渡すと、取得済みだった選手が「あとn シーズン」に戻る。種別を区別するならドメインの引数を `(年, 種別)` に変える。
