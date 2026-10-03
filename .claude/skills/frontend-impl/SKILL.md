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

- 着手前に実装計画を GitHub Issue に書く（`issue-plan` スキル）。既に Issue があればその計画に従い、計画が変わったら Issue を直す。
- `docs/design/` に該当する設計ドキュメントがあればそれが仕様。従って実装し、末尾の進捗チェックリストを更新する（完了後は `docs/wiki/` へ吸収して削除する運用。React 導入の設計は既に Wiki の「画面の歩き方」「試合の記録」「フロントエンド」に吸収済み）。
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

- React を触ったら `frontend-check`（tsc --noEmit）と `frontend-build` を通す。どちらもコンテナを新しく作る操作で、`make` は WSL 側にしか無いので、
  Claude Code からは `wsl -e bash -c "cd /home/sumika/work/develop/my_django_project && make frontend-check"` のように WSL 経由で呼ぶ（worktree なら末尾に `WT=<名前>`）。
- テンプレートの描画・API の動作は `tests/integration/`、JS・CSS が絡む実ブラウザ確認だけ `tests/e2e/`（ビルド済みアセットが前提）。実行方法は `run-tests` スキル参照。
- Python 側（views・api・forms）も触ったら lint とフルスイートを通す（コマンドは CLAUDE.md。worktree なら `worktree` スキルの手順2）。

## 5. 仕上げ

- `docs/wiki/画面の歩き方.md` など該当ページを更新する。docs/design/ のドキュメントが完了したら `docs/wiki/` へ吸収して削除する。
- 機能ごとに1コミット・日本語メッセージで、Issue に紐づけたブランチにコミットする（main に直接コミットしない）。
  終わったら `open-pr` スキルで3周セルフレビューを済ませて PR を出す。
