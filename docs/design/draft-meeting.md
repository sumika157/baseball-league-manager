# 設計: ドラフト会議の保存と1巡ずつの進行（D3・D4）

> 状態: D3 着手（2026-11-12）。親 Issue [#198](https://github.com/sumika157/baseball-league-manager/issues/198)、
> 段階は [#297](https://github.com/sumika157/baseball-league-manager/issues/297)（D3）・[#298](https://github.com/sumika157/baseball-league-manager/issues/298)（D4）。
> 統合ブランチ `epic/draft-meeting`（D3・D4 だけを積む。D1・D2 は main に直接入った。2026-10-24 ユーザー決定）。
> 段階ごとの決定と実測はこのファイルの「段階ごとの記録」に追記し、epic を main に入れるとき（D4 の後）に Wiki
> 「ペナントモード」「ペナントモードの仕組み」「画面の歩き方」へ吸収して削除する。

## 1. なぜ

D1・D2 で会議のエンジン（行動の並びから導く `DraftState`・入札と抽選・AI・育成の巡）は入ったが、会議は締める処理の中で
全球団を AI として一度に進めるので、GM はドラフトで何も選べない。会議の**行動だけを保存**し、GM が1巡ずつ指名し、
抽選の結果を見て次へ進め、途中で離れても再開できるようにする。

足すのは保存・進行・画面・ロックだけ。会議の間、`Team` 集約・能力・日程には何も書かない（新人を `Team` に入れるのは
今と同じく締める処理の1つのトランザクション）。会議を始めなかった年は、締める処理が今と同じく全自動で進める。

## 2. ユーザーの決定（2026-10-25。#297 のコメント）

| | 決定 |
| --- | --- |
| Q1 会議を開ける時点 | **ポストシーズンの後**（締められる条件と同じ）。戦力外の指名は今どおりポストシーズン中も選べ、会議を始めたら固定 |
| Q2 候補の指紋 | **候補の身元だけ**（番号・名前・位置・経歴・生年月日）。隠し値（伸びしろなど）や能力の式を後から変えても、途中の会議は壊れない |
| Q3 見立ての連続性 | **引き継がない**。ドラフトのときの見立て（種は「世界・年・候補・球団」）と入団後の見立て（種は「世界・球団・選手」）は別の乱数。画面の注記と Wiki で説明する |
| Q4 球団の好み | **画面に出さない** |
| Q5 会議のやり直し | **GM が自分でやり直す操作は作らない**。`DraftChanged`（コードが変わって候補か人数の幅が変わった）のときだけ「会議をやり直す」を出す |

## 3. #198 本文から変える点

1. 行動の行に区分・巡・入札の回・抽選の当たり外れを持たせない（行動の並びから導ける。抽選は種で再生できる）。
2. 「すべてお任せにする」ボタンを作らない（会議を始めずに締めれば全自動＝同じ結果の操作が2つ並ぶ）。締めるカードに
   「ドラフト: お任せ（会議を開かない）・ドラフト会議で指名する」の1行。
3. GM ホーム: 未開始と終了は締めるカード＋ドラフトの行、途中は会議のカード（「N巡目・あなたの番」「会議へ」）だけ。
4. 会議の画面に盤を置かない（D4 でオフの結果と共有する部分テンプレート）。D3 は「この巡の指名順と結果」「自軍の指名」「直近の動き」。
5. GM の球団の育成の最多は `MAX_DEVELOPMENT_PICKS`。AI は計画の人数（`development_planned`）に達したら選択終了する
   （会議を始めなかった年と始めた年で AI の部分が同じになる）。

## 4. 設計

### domain

- `meeting_slots(teams, plans, *, managed_team_id)`: 会議の人数の幅。**会議を始めるときと締めるときの唯一の関数**。
  GM の球団だけ育成の最多を `MAX_DEVELOPMENT_PICKS` にする。
- `DraftState.ai_action` の育成の選択終了: 計画の人数に達したら PASS。
- `ProspectPool.signature`: 身元だけ（Q2）。
- 新しい集約 `DraftRecord`（`domain/pennant/draft_record.py`）: 年・候補の指紋・人数の幅・行動の並び。範囲は世界×年。
  `state(strategy, pool)`（指紋が違う・当てられない行動があれば `DraftChanged`）・`last_seq`・状態。
- `DraftMeetingStatus`（未開始・途中・終了）。導くだけで保存しない。
- 変えられない理由の関数: `world.draft_order_rule_change_blocker(*, world_over, meeting_started)`、
  `pre_draft_input_blocker(*, meeting_started)`（戦力外の指名に使う。#169 F2・#170 P2 も同じ関数）。
- 例外（日本語）: `DraftInProgress`・`DraftConflict`・`DraftChanged`・`DraftStarted`。
- 画面用: `DraftState.resolved_moves(after_seq)`（保留中の入札は出さない）・`selectable(team_id, prospect)`
  （`apply` の検査と食い違わない）。候補の一覧の並べ替えのキーと既定（見込みの区分の降順）は `domain/services/sorting.py`（T3）。

### application（T2）

- 新しい更新サービス `PennantDraftService`（`application/pennant_draft.py`）: `view`・`start`・`nominate`（入札か指名かは番が決める）・
  `pass_turn`・`delegate_turn`（指名できる候補が無いときだけ）・`auto_complete`・`reset`（`DraftChanged` のときだけ。Q5）。
- 書き込みは「外で読む → 候補を作り直す → 再生 → GM の行動 → `advance_ai(stop_at=GM)`」、中で `last_seq` と
  未締めを読み直して追記する。局面の検査（`_check`・`_season_over_year`）と締める処理の入力の読み口
  （`OffseasonInputsReader`）を切り出して共有する。
- 締める処理: 会議が途中なら `DraftInProgress`、終わっていれば保存した行動を `plan_offseason(draft_actions=…)` に渡し、
  `meeting_slots` と指紋を写したものと比べる。始めていなければ今と同じ。`_state()` に会議の目印、`_commit` で会議の表を消す。
- 戦力外の指名は、中で「会議が無い」を読み直してから書く。`PennantSettingsService` は会議の有無を見る。DTO（`DraftMeetingView` ほか）。

### infrastructure

- ORM: `PennantDraft`（世界×年で一意）・`PennantDraftSlot`（会議を始めたときに写した人数の幅）・
  `PennantDraftAction`（会議×`seq` で一意＝二重送信の最後の砦）。
- `DraftRepository`（`WorldScope` 必須。`append(..., expected_seq)`）。名前だけを読む軽い参照（候補の名前が世界の選手と
  重ならないようにするため。`Team` 集約を組み立てない）。
- 世界の削除・`test_world_isolation` の分類表。

### presentation（T3）

- URL: GET `draft/<年>/`（誰でも）、POST `start/`・`nominate/`・`pass/`・`delegate/`・`auto/`・`reset/`（オーナー。
  `expected_year`・`expected_seq`）。締めた年はオフの結果へ。`build_pennant_draft_service`（`test_wiring`）。
- テンプレート `draft.html`（状態ごとに操作を分ける）・`_draft_card.html`・`_season_close.html` の行。
  戦力外タブと設定の「指名順」は会議の後は読むだけで理由を出す。

### データ

CreateModel 3つ（backfill なし）。1会議あたり行動 約600行・人数の幅 48行（8リーグ）。締めると消える。
候補は保存しない（決定的に作り直し、指紋で照合）。

## 5. 段階（タスク。base は `epic/draft-meeting`）

| タスク | 内容 | PR |
| --- | --- | --- |
| T0 | epic とこの設計書 | |
| T1 | domain と保存（上の domain・infrastructure） | |
| T2 | application（`PennantDraftService`・締める処理・ロック） | |
| T3 | 画面 | |

D4（#298）: 抽選の記録（`PennantDraftLottery`）と「N球団競合」・巡×球団の盤・世界の一覧の「会議の途中」・
並べ替えの追加キー・Wiki への吸収。epic → main は D4 の後の1本で、自動マージにしない。

各タスクで `ddd-boundary-reviewer` を通す。

## 6. やらないこと（D4 か作らない）

抽選の記録と「N球団競合」・巡×球団の盤・世界の一覧の「会議の途中」・並べ替えの追加キー（D4）、誰が決めたかの列、
1手の取り消し、外国人・育成の昇格の指名の固定（#169 F2・#170 P2 の側で）、管理コマンドからの GM の指名。

## 7. 申し送り（epic の外へ）

- #169 F2・#170 P2 は、書く前に `pre_draft_input_blocker` を通し、`OffseasonInputsReader` に指名を足す。
- D4 の抽選の記録は、締めるときに再生した `state.lotteries` から写す。

## 8. 実測の目安（T2・T3 で測る）

`measure_pages`（8リーグ）で: 会議の GET 300ms・1手の POST 500ms・お任せ 2秒・始める 3秒（トランザクションの外）・
締める処理のロックは変わらない・GM ホームの増え方。超えたら会議の間だけ候補を保存する退路。

## 9. 段階ごとの記録

### 着手時の確認（2026-11-12）

- 前提の #169 F1（#324）・F1b（#328）・#308 P3b（#329）は main に入った（c8b5f70）。epic はそこから切った。
- **候補（`draw_pool`）に渡す `used_names` は、外国人の獲得の結果に依存しない。** 獲得した外国人の名前は
  `generator.foreign_name` の「名・姓」（中黒を含むカタカナ）で、候補の名前（`generator.japanese_name` の漢字の氏名）と
  重ならないので、候補を引く乱数の流れは変わらない（`tests/domain/test_foreign_market.py` の
  `test_signing_first_does_not_change_the_meeting` が固定している）。名前の衝突が続いたときの連番の逃げ道
  （`japanese_name` の末尾）だけは名前の数に依るが、氏名の組み合わせの数からみて起きない。
  ただし**候補の位置ごとの人数**（`planned`）と人数の幅は、獲得した外国人の位置（`signed`）で決まる。会議を始めるときも
  締める処理と同じく、引退・戦力外（`plan_departures`）→ 外国人の獲得（`offseason_signings`）を計算してから
  `meeting_slots` と `draw_pool` を呼ぶ（獲得は決定的なので、会議を始めた後に戦力外の指名を固定すれば締めるときも同じになる）。
- epic の構成: 統合ブランチ `epic/draft-meeting` は親 #198 に紐づけ（前例 #39 と `epic/postseason`。epic の親に紐づけ、
  epic を main に入れる PR は `Closes #198`）、タスクのブランチは D3 の #297 に紐づける。タスクは sub-issue にせず、
  #297 の本文のチェックリストで追う。
