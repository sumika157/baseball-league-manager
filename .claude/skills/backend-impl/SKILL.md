---
name: backend-impl
description: バックエンド（myapp/ の domain・application・infrastructure・presentation 層、マイグレーション）の実装を行う。「この設計を実装して」「API を追加して」「集約・業務ルールを変更して」のように Python 側のコードを書く作業で使う。実装は Sonnet で行い、設計・要件定義が未確定なら feature-designer エージェント（Opus）に先に委譲する。
model: sonnet
---

バックエンド実装のワークフロー。規約の本体は `CLAUDE.md`（アーキテクチャ規則・出典の一元化・不変条件・マイグレーション・文言）。ここには進め方だけを書く。

## 0. モデルの使い分け

- このスキルが有効なターンは Sonnet で動く（frontmatter `model: sonnet`）。ターンをまたいで実装を続けるときは、次のターンでもこのスキルを呼び直す。
- **設計・要件定義は Opus の担当**。実装中に設計判断（新しい集約が要るか、フィールド追加が出典の二重化にならないか、スコープの変更）が必要になったら、自分で決めずに `feature-designer` エージェント（`model: opus`）に委譲するかユーザーに確認する。

## 1. 設計を確認してから書く

- `docs/design/` に該当する設計ドキュメントがあればそれが仕様。従って実装し、末尾の進捗チェックリストを更新する。
- 設計が無い新機能・仕様変更は、先に `feature-designer` エージェントで設計書を作る。
- バグ修正・文言修正・既存パターンの踏襲だけの変更は、設計なしでそのまま実装してよい。

## 2. 内側から外側へ実装する

`domain → application → infrastructure → presentation` の順に書く（依存規則・各層の責務は CLAUDE.md「アーキテクチャ規則」）。

- 更新は集約（`Team` / `Game`）+ リポジトリ経由、参照は `infrastructure/queries.py` で DTO 直行。どちらの経路かを最初に決める。
- domain を書いたら、その時点で `make test-domain`（Django 設定なし・DB 不要・数秒）を回して業務ルールを固めてから外側へ進む。
- マイグレーションはスキーマ変更と `RunPython`（backfill）を別ファイルに分ける。適用済み（コミット済み）のマイグレーションは編集しない。

## 3. テストと検証

- 実装と同時にテストを書く: 業務ルールは `tests/domain/`（Django 非依存）、画面・永続化・フォームは `tests/integration/`。実行方法は `run-tests` スキル参照。
- 最後にフルスイート（`make test`）と `make lint`（コミット前の必須ゲート。中身は Makefile が出典）を通す。

## 4. 仕上げ

- 集約・層境界・不変条件に触れた変更は `ddd-boundary-reviewer` エージェントでレビューし、指摘があれば `fix-review` スキルで直す。
- README の該当節を更新する。docs/design/ のドキュメントが完了したら README へ吸収して削除する。
- コミットはユーザーに求められたときだけ。機能ごとに1コミット・日本語メッセージ・main に直接。
