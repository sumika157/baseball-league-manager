---
paths:
  - "myapp/tests/**/*.py"
---

# テストの置き場所とファイル分割

- `tests/domain/` は **DB 不要・Django 非依存**。Django 設定を読み込まずに通ること（`python -m unittest discover -s myapp/tests/domain -t .` で確認できる）。業務ルールはここ。
- `tests/integration/` は画面の動作・リポジトリの往復・フォーム検証・テンプレート検査・保存 API。
- `tests/e2e/`（Playwright + `StaticLiveServerTestCase`）は**実ブラウザでしか確認できないものだけ**。主要導線のスモークと、JS・CSS が絡んで integration では検証できないもの。業務ルールや画面のロジックを E2E に寄せない（遅く壊れやすい）。
- **ディレクトリの中は対象ごとのファイルに分ける。** 既存の大きいファイルに足し続けない（`test_integration.py` が 3,975行・47クラスまで膨らんで16ファイルに分割した前例がある）。目安として1ファイル600行を超えたら分ける。
- 結合テストの共通の土台は `tests/integration/base.py` の `BaseCase`（リーグ・球場・チーム2つ・組み立て済みのサービス）。テスト専用のサービス組み立てを作らず、`tests/helpers.py` が再輸出する `build_*()`（画面と同じ組み立て口）を使う。`BaseCase` は `self.league_view`（`build_league_view_service()`。ダッシュボード・順位・リーグ・タイトル・成績・チーム一覧）・`self.team_view`（`build_team_view_service()`。選手の一覧・個人ページ）・`self.game_view`・`self.roster` を持つ。
- 成績を持たせたい場合は `tests/helpers.py` の `play_game` / `give_batting` / `give_pitching` で試合を作る（成績は試合から集計されるため、選手に直接持たせられない）。
- ソースの整合性を機械的に見るテストは `SimpleTestCase`（DB 不要）で書く。前例: `test_templates.py`（コメント記法）・`test_wiring.py`（依存の組み立て）・`test_stat_fields.py`（成績項目の列挙）。
- **重い準備（選手の登録・世界の分岐・シーズンを回す）はクラスで1回（`setUpTestData`）にする。** テストごとの変更は `TestCase` が巻き戻す。
  同じシーズンを回すテストは同じクラスに集める（前例: `FinishedSeasonCase`・`FinishedSeasonTest`）。選手を何人も登録するときは
  `helpers.register_players` でまとめる（1人ずつのサービス呼び出しは人数の2乗で遅い。登録そのものを確かめるテストは除く）。
  遅くなったら `manage.py test --slowest-classes 20` で所要時間の長いクラスを見る（#184）。

# 期待値（何を正しいとするか）

- **期待値は仕様から書く。実装を動かして出た値を写さない。** 実装の後にテストを書くと、今の挙動（バグ込み）を正解として
  固定しやすい。期待値の根拠（NPB の規則・Issue の決定・Wiki の仕様）をテスト名か docstring に書く。
  例外は、意図して今の挙動を固定する特性テスト（`tests/integration/golden/` と `test_game_edit_payload.py`）。
- **落ちたテストの期待値を、今の実装の出力に合わせて書き換えない。** テストが実装の追認になり、バグを正解として固定する。
  期待値を直してよいのは、今回の変更で仕様を意図して変えたとき（Issue・ユーザーの決定がある）だけで、理由をコミットメッセージに書く。
  ゴールデンファイルの更新も同じ。仕様が変わったのか実装が壊れたのかの決め方は `run-tests` スキルの §6。
- 書き換えてよいのは、何を正しいとするかに触れない部分だけ（E2E のセレクタ、テストデータの組み立て方、画面の文言を変えたときの文字列の照合など）。
