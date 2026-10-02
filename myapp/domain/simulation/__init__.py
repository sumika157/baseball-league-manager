"""試合シミュレーション。Django にも numpy にも依存しない。

| モジュール | 内容 |
| --- | --- |
| `ratings` | 能力値（野手5項目・投手4項目）、成長型、能力の区分（S〜G） |
| `baseline` | リーグの基準値。**NPB の水準を決める定数の唯一の出典** |
| `odds` | 能力 → 個人の率 → odds ratio 法による対戦の確率 |
| `baserunning` | 打席の結果と走者の走力から進塁（`RunnerAdvance`）を組み立てる |
| `fielding` | 打球の処理経路（`fielded_by`）と失策を犯す守備者 |
| `manager` | AI 監督（1軍登録・スタメン・ローテーション・継投・代打・外国人の出場枠） |
| `engine` | 1試合を打席単位で進め、`assemble_game()` で `Game` を組み立てる |
| `levels` | リーグの水準・タイトル争いの水準を数える |
| `randomness` | 乱数源の Protocol と、試合ごとのシード |
| `samples` | 調整・確認・テスト用の球団 |
"""
