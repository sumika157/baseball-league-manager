---
name: frontend-impl
description: フロントエンド（Django テンプレート・theme.css・frontend/ の React アイランド）の実装を行う。「画面を作って」「テンプレートを直して」「React 画面を実装して」「CSS を調整して」のように画面側のコードを書く作業で使う。実装は Sonnet で行い、設計・要件定義が未確定なら feature-designer エージェント（Opus）に先に委譲する。
model: sonnet
---

フロントエンド実装のワークフロー。規約の本体は `CLAUDE.md`（UI・設計方針、文言、既知の罠）。ここには進め方と画面側固有の規則だけを書く。

## 0. モデルの使い分け

- このスキルが有効なターンは Sonnet で動く（frontmatter `model: sonnet`）。ターンをまたいで実装を続けるときは、次のターンでもこのスキルを呼び直す。
- **設計・要件定義は Opus の担当**。実装中に設計判断（新しい画面が要るか、操作の共存・削除、API の形の変更）が必要になったら、自分で決めずに `feature-designer` エージェント（`model: opus`）に委譲するかユーザーに確認する。

## 1. 設計を確認してから書く

- `docs/design/` に該当する設計ドキュメントがあればそれが仕様。従って実装し、末尾の進捗チェックリストを更新する（現在: `frontend-react-islands.md` が React 導入の設計。README へ吸収後は README「画面構成」が出典）。
- 新しい画面・大きな UI 変更は、先に `feature-designer` エージェントで設計書を作る。文言修正・スタイル調整・既存画面の小さな改善は設計なしでそのまま実装してよい。

## 2. 方式を選ぶ

| 対象 | 方式 |
| --- | --- |
| 参照系（一覧・詳細・順位表など） | Django テンプレート + Bootstrap + `theme.css`。React 化しない |
| リッチな編集画面 | React アイランド。`frontend/src/<エントリ名>/` を作り、テンプレートの root div にマウントする |

## 3. React アイランドの規則

- dev server（HMR）は使わない。`vite build --watch` が `myapp/static/myapp/dist/` にハッシュ無しの固定名で出力し、テンプレートは `{% static 'myapp/dist/<エントリ名>.js' %}` で参照する。新しいエントリは `frontend/vite.config.ts` の `rollupOptions.input` に追加する。
- 初期データはビューが payload を組み立てて `json_script` で埋め込む（GET 用 API は作らない）。保存だけ JSON API（`presentation/api.py`、CSRF は payload の `csrf_token` を `X-CSRFToken` ヘッダで送る）。JSON のキーは snake_case のまま（camelCase 変換層を作らない）。
- **検証の出典を増やさない**: 型変換・必須チェックは `presentation/forms.py` のフォームを再利用し、業務ルールはドメイン層のまま。React 側の検証は保存前の入力補助（警告表示）に留め、確定判断はサーバーのエラーメッセージを表示する。
- npm 操作はすべて `docker compose run --rm frontend npm ...`（ホストに Node 環境を作らない）。**コンテナを新規作成する操作（up / run）は必ず WSL 側から実行する**（Windows 側から作るとバインドマウントが壊れ npm install が ENOSPC で死ぬ）。

## 4. UI の規則（CLAUDE.md の要点）

- 両立しない操作を同じ画面に並べない。書き込み導線は未ログインの人に見せない。GET は公開・POST はログイン必須。
- 導出できる値は入力させない（自動計算して読み取り専用にする）。
- ユーザーの目に触れる文言はすべて日本語。見た目は既存の Bootstrap + `theme.css` のクラス（`entry-table` 等）に揃える。
- テンプレートのコメント `{# #}` は単一行専用。複数行は `{% comment %}` を `{% extends %}` より後に置く。

## 5. 検証

- React を触ったら `make frontend-check`（tsc --noEmit）と `make frontend-build` を通す。
- テンプレートの描画・API の動作は `tests/integration/`、JS・CSS が絡む実ブラウザ確認だけ `tests/e2e/`（ビルド済みアセットが前提）。実行方法は `run-tests` スキル参照。
- Python 側（views・api・forms）も触ったら `make lint` とフルスイートを通す。

## 6. 仕上げ

- README の画面構成の節を更新する。docs/design/ のドキュメントが完了したら README へ吸収して削除する。
- コミットはユーザーに求められたときだけ。機能ごとに1コミット・日本語メッセージ・main に直接。
