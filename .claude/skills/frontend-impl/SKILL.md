---
name: frontend-impl
description: フロントエンド（Django テンプレート・theme.css・frontend/ の React アイランド）の実装を行う。「画面を作って」「テンプレートを直して」「React 画面を実装して」「CSS を調整して」のように画面側のコードを書く作業で使う。実装は Sonnet で行い、設計・要件定義が未確定なら feature-designer エージェント（Opus）に先に委譲する。
model: sonnet
---

フロントエンド実装のワークフロー。**ここには進め方だけを書く。** 規約の本体は
`CLAUDE.md`（UI・設計方針、文言）と `.claude/rules/`（触ったファイルに応じて自動で読み込まれる）:

- `rules/frontend.md` — React アイランドの配線・API のやりとり・`theme.css`
- `rules/templates.md` — Django テンプレートの書き方

## 0. モデルの使い分け

- このスキルが有効なターンは Sonnet で動く（frontmatter `model: sonnet`）。ターンをまたいで実装を続けるときは、次のターンでもこのスキルを呼び直す。
- **設計・要件定義は Opus の担当**。実装中に設計判断（新しい画面が要るか、操作の共存・削除、API の形の変更）が必要になったら、自分で決めずに `feature-designer` エージェント（`model: opus`）に委譲するかユーザーに確認する。

## 1. 設計を確認してから書く

- `docs/design/` に該当する設計ドキュメントがあればそれが仕様。従って実装し、末尾の進捗チェックリストを更新する（完了後は README へ吸収して削除する運用。React 導入の設計は既に README「画面構成」「リッチな編集画面」に吸収済み）。
- 新しい画面・大きな UI 変更は、先に `feature-designer` エージェントで設計書を作る。文言修正・スタイル調整・既存画面の小さな改善は設計なしでそのまま実装してよい。

## 2. 方式を選ぶ

| 対象 | 方式 |
| --- | --- |
| 参照系（一覧・詳細・順位表など） | Django テンプレート + Bootstrap + `theme.css`。React 化しない |
| リッチな編集画面 | React アイランド。`frontend/src/<エントリ名>/` を作り、テンプレートの root div にマウントする |

## 3. 画面の設計判断（ファイルを開く前に決めること）

`.claude/rules/` はファイルを触ったときに読み込まれるため、**作るものを決める段階では効かない。**
次の3点はここで確認する（詳細は CLAUDE.md の「UI・設計方針」）。

- 両立しない操作を同じ画面に並べない。片方を無効化して案内文で繕う前に、片方を消せないか考える。
- 書き込みの導線は未ログインの人に見せない。GET は公開・POST はログイン必須。
- 導出できる値は入力させない（自動計算して読み取り専用にする）。

## 4. 検証

- React を触ったら `make frontend-check`（tsc --noEmit）と `make frontend-build` を通す。
- テンプレートの描画・API の動作は `tests/integration/`、JS・CSS が絡む実ブラウザ確認だけ `tests/e2e/`（ビルド済みアセットが前提）。実行方法は `run-tests` スキル参照。
- Python 側（views・api・forms）も触ったら `make lint` とフルスイートを通す。

## 5. 仕上げ

- README の画面構成の節を更新する。docs/design/ のドキュメントが完了したら README へ吸収して削除する。
- コミットはユーザーに求められたときだけ。機能ごとに1コミット・日本語メッセージ・main に直接。
