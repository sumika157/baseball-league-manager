# 設計: 支配下／育成の契約区分と、FA・獲得経路

> 状態: 段階 E1（#126）実装済み。epic [#125](https://github.com/sumika157/baseball-league-manager/issues/125)、全体の親 [#114](https://github.com/sumika157/baseball-league-manager/issues/114)。
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
- 国内 FA は8シーズン（2007年以降にドラフトで入団した大卒・社会人は7）、海外は9、再取得は4。
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
