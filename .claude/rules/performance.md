---
paths:
  - "myapp/application/*.py"
  - "myapp/infrastructure/*.py"
  - "myapp/presentation/views.py"
---

# 読む範囲を絞る（性能）

3,480試合・1,629選手のデータで主要画面が4〜5秒かかっていた。原因はすべて
**必要のない範囲まで読んでいたこと**で、SQL の書き方や索引の問題ではなかった。
`docker compose exec web python manage.py measure_pages` で実測できる。

## 遅さの見つけ方

- **SQL 時間だけを見ても気づけない。** 応答4.8秒のうち SQL は 38ms で、残りは
  ORM のモデル生成と値オブジェクトの組み立て（Python 側）だった。
  応答時間・SQL 時間・クエリ数の3つを並べて見る。
- **クエリ数が件数に比例して増えていないかを見る。** 48チームで290クエリのように、
  「対象の数 × 数クエリ」になっていたら N+1。
- どこが重いかは `measure_pages --profile <URL>`（cProfile）で見る。

## 参照のときにやってはいけないこと

- **参照の画面で集約（`Game` / `Team`）を全件組み立てない。** 順位・対戦成績・
  試合数は得点と対戦カードだけで決まる。`GameRepository.find_all()` は1試合ごとに
  打撃・投球・イニングスコアの明細まで読むため、順位表のために呼ぶと3,480試合で
  9.6万行を無駄に組み立てる。`application/queries.py` の参照クエリを使う。
- **数えるだけなら読まない。** 試合数は `GameListQuery.count_by_team()`（SQL の集計）。
  全件を読んで Python で数えない。
- **絞り込みは SQL 側で行う。** 全部読んでから内包表記で捨てるのは、捨てる分の
  明細まで組み立ててから捨てているのと同じ。用途に合った絞り込み付きのメソッドを
  リポジトリ／参照クエリに足す（`find_between_teams` `find_by_league_with_roster` が前例）。
- **ロスターが要らない画面で `find_all_with_roster()` を呼ばない。** 通算成績の集計が
  付いてくる。名前と id だけなら `find_all()`、一覧表示なら `TeamListQuery`。

## リポジテリ実装のときにやってはいけないこと

- **対象ごとにクエリを投げない。** マッピングの中で関連を引き直すと、対象の数ぶんの
  クエリになる。`_RosterData` のように、まとめて読んだ結果を渡して組み立てる。
- `prefetch_related` を書いたら、**マッピングが本当にそれを使っているか**を確かめる。
  `row.stints.all()` ではなく `PlayerStint.objects.filter(team=row)` と書くと、
  prefetch は無駄になり N+1 に戻る（実際にそうなっていた）。
- SQLite の多段 prefetch は関連1000件超で `Expression tree is too large` になる。
  `Prefetch(..., queryset=...select_related(...))` で JOIN にまとめる。

## 覚える（キャッシュ）を足すときの条件

- **集約（`Team` / `Game`）を要求をまたいで覚えない。** 可変なので、覚えた集約を
  書き換えると別の呼び出しに漏れる。サービス外（ORM 直書き・別プロセス）の更新にも
  気づけない。実際に「同じサービスで2回読むと古い値が返る」形になり、
  `test_admin.py` が検出した。
- 覚えてよいのは**不変な値**だけ。前例: リーグの基準値（`_LeagueContext`）、
  投球回の変換結果（`InningsPitched` は frozen）。
- 速くする前に、まず**読む範囲を絞れないか**を考える。リーグ平均のために全48チームを
  読んでいたのをリーグ単位にしたら、キャッシュより速くなった（519ms → 257ms）。
