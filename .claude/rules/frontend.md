---
paths:
  - "frontend/**/*.ts"
  - "frontend/**/*.tsx"
  - "frontend/package.json"
  - "myapp/static/myapp/css/*.css"
---

# React アイランドとスタイル

設計の全体像と理由は README の「リッチな編集画面（React アイランド）」「デザイン」を参照。
ここには**書くときに守ること**だけを置く。

## ビルドと配線

- dev server（HMR）は使わない。`vite build --watch` が `myapp/static/myapp/dist/` に**ハッシュ無しの固定名**で出力し、テンプレートは `{% static 'myapp/dist/<エントリ名>.js' %}` で参照する。dev/prod の分岐や manifest 解析を持ち込まない。
- 新しいエントリを作ったら `frontend/vite.config.ts` の `rollupOptions.input` に追加する。
- npm 操作は `docker compose run --rm frontend npm ...`（ホストに Node 環境を作らない）。**`run` / `up` は WSL 側から実行する**（Windows 側から作るとマウントが壊れ `npm install` が `ENOSPC` で死ぬ）。
- 依存は `package.json` で**完全固定**（`--save-exact`）。

## サーバーとのやりとり

- 初期データはビューが組み立てた payload を `json_script` で埋め込む。**GET 用の API は作らない**（画面の URL・権限・404 は既存ビューのまま）。
- 保存だけ JSON API（`myapp/presentation/api.py`）。CSRF は payload の `csrf_token` を `X-CSRFToken` ヘッダで送る。
- **JSON のキーは snake_case のまま**。camelCase への変換層を作らない。キー名はフォームのフィールド名と 1:1。
- クライアントは全行を送る。「全欄空 ＝ 出場していない」の間引きはサーバー（フォームの `is_blank`）が判断する。
- **検証の出典を増やさない。** 型変換・必須チェックは `presentation/forms.py` のフォーム、業務ルールはドメイン層。React 側の検証・自動計算は保存前の入力補助に留め、確定はサーバーの応答を表示する。
- 部分更新でキーが欠けたときに「空リスト」と同じ扱いをしない（既存データが全消去される）。行は位置ではなく識別子で組み立てる。

## スタイル

- Bootstrap 5 の上に `theme.css` を薄く重ねる。グリッドとユーティリティは Bootstrap のまま使い、**配色・余白・タイポグラフィ・角丸だけ**を上書きする。
- 既存クラス（`entry-table` など）に揃える。新しい配色やフォントを増やさない。
- 成績表は `tabular-nums` で桁を揃える。
