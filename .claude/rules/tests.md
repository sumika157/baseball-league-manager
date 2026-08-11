---
paths:
  - "myapp/tests/**/*.py"
---

# テストの置き場所とファイル分割

- `tests/domain/` は **DB 不要・Django 非依存**。Django 設定を読み込まずに通ること（`python -m unittest discover -s myapp/tests/domain -t .` で確認できる）。業務ルールはここ。
- `tests/integration/` は画面の動作・リポジトリの往復・フォーム検証・テンプレート検査・保存 API。
- `tests/e2e/`（Playwright + `StaticLiveServerTestCase`）は**実ブラウザでしか確認できないものだけ**。主要導線のスモークと、JS・CSS が絡んで integration では検証できないもの。業務ルールや画面のロジックを E2E に寄せない（遅く壊れやすい）。
- **ディレクトリの中は対象ごとのファイルに分ける。** 既存の大きいファイルに足し続けない（`test_integration.py` が 3,975行・47クラスまで膨らんで16ファイルに分割した前例がある）。目安として1ファイル600行を超えたら分ける。
- 結合テストの共通の土台は `tests/integration/base.py` の `BaseCase`（リーグ・球場・チーム2つ・組み立て済みのサービス）。テスト専用のサービス組み立てを作らず、`tests/helpers.py` が再輸出する `build_service()` を使う。
- 成績を持たせたい場合は `tests/helpers.py` の `play_game` / `give_batting` / `give_pitching` で試合を作る（成績は試合から集計されるため、選手に直接持たせられない）。
- ソースの整合性を機械的に見るテストは `SimpleTestCase`（DB 不要）で書く。前例: `test_templates.py`（コメント記法）・`test_wiring.py`（依存の組み立て）・`test_stat_fields.py`（成績項目の列挙）。
