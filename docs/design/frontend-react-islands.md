# フロントエンド導入設計: React アイランド + 試合編集画面

> **運用メモ**: このファイルはセッションをまたぐ引き継ぎ用の設計ドキュメント。
> 実装がすべて完了したら、必要な内容を README（一次ドキュメント）へ吸収し、このファイルは削除する。
> 進捗は末尾のチェックリストを更新すること。

## 要件

- リッチな編集画面を作り込みたい。参照系（一覧・詳細・順位表）は現行の Django テンプレートのまま。
- 第一弾は**試合編集画面**（`/games/<id>/edit/`）。入力の構造が最も深く（イニングスコア × 打撃 × 投球 × 継投）、リッチ化の恩恵が最大のため。
- DDD の層規則・テスト戦略・日本語文言の規則（CLAUDE.md）はすべて維持する。

## 方式の決定

### 全体構成: React アイランド

全面 SPA 化はしない。Django がページを描画し、リッチな編集画面だけ
テンプレート内の `<div id="...-root">` に React コンポーネントをマウントする。

- フレームワーク: **React 18 + TypeScript + Vite**（`frontend/` に配置）
- 参照系画面には手を入れない。編集画面を React 化するときも、URL・権限・ビューの骨格は変えない。

### ビルド方式: `vite build --watch`（dev server は使わない）

| 決定 | 理由 |
|---|---|
| Vite dev server（HMR）は使わず、`vite build --watch` で `myapp/static/myapp/dist/` に常時出力 | dev/prod の分岐・CORS・react-refresh preamble・manifest 解析が全部不要になる。E2E（`StaticLiveServerTestCase`）も同じ成果物をそのまま配信できる |
| 出力ファイル名は**ハッシュなしの固定名**（例: `game_edit.js`） | manifest 解析が不要になり、テンプレートは `{% static %}` で参照するだけ。**django-vite 等の Python 依存を追加しない** → `requirements*.txt` 変更なし、イメージ再ビルド不要 |
| ビルド成果物（`myapp/static/myapp/dist/`）と `frontend/node_modules/` は gitignore | 生成物はコミットしない。`package-lock.json` はコミットする |

将来キャッシュバスティングが必要になったら django-vite + manifest 方式へ移行する（この決定はその時に見直す）。

### 実行環境: node コンテナ

- docker-compose に `frontend` サービス（`node:22-bookworm-slim`）を追加。
  `npm install && npm run watch` を実行し続ける。web イメージには Node を入れない。
- ホストに Node 環境は作らない（Python と同じ方針）。npm 操作はすべて
  `docker compose run --rm frontend npm ...` で行う。
- **注意**: `docker compose up` / `run` などコンテナを**新規作成**する操作は必ず WSL 側から
  実行する（CLAUDE.md 実行環境の規則参照）。Windows 側から作るとバインドマウントが
  実体と切り離された 127MB の幽霊ディスクになり、npm install が ENOSPC で死ぬ
  （2026-08-11 に実際に踏んだ。書き込みも実リポジトリに届かない）。

### Django との接続

- **初期データはテンプレート埋め込み**: ビューが payload dict を組み立て、
  `{{ payload|json_script:"game-edit-data" }}` で埋め込む。GET 用 API は作らない
  （画面の URL・権限チェック・404 処理を既存ビューのまま使えるため）。
- **保存だけ JSON API**: `POST /api/games/<id>/`（`presentation/api.py`）。
  成功 → `{"ok": true, "redirect_url": "<試合詳細>"}`、
  失敗 → `{"ok": false, "error": "<日本語メッセージ>"}`（HTTP 400）。
  権限なし → 403、未ログイン → 403（画面自体が login_required なので通常は起きない）。
- **CSRF**: payload に `csrf_token` を含め、fetch の `X-CSRFToken` ヘッダで送る。
- **JSON のキーは snake_case のまま**（Python と 1:1。camelCase への変換層を作らない）。

### 検証の出典を増やさない

- 型変換・必須チェックは既存の `presentation/forms.py`（`GameForm` / `BattingEntryForm` /
  `PitchingEntryForm` / `InningScoreForm`）を**行単位でそのまま再利用**する
  （フォームは dict を受け取れるので formset の management form は不要）。
- 「全欄空 = 出場していない」の判定（`is_blank`）もフォーム側の実装が唯一の出典。
  クライアントは全行を送り、サーバー側で間引く。
- 業務ルール（被本塁打 ≤ 被安打、勝敗・S・H の導出など）はドメイン層のまま。
  React 側の警告表示はあくまで入力補助で、確定判断はドメイン例外を表示する。

### React 画面の UX 方針（既存フォームからの改善点）

- **スコアはイニングスコアから自動計算**（導出できるものは入力させない）。
  イニングが1マスでも入力されたら合計を表示・送信し、手入力欄は読み取り専用にする。
  イニング全空欄の場合だけ従来どおり手入力できる。
- 被本塁打 > 被安打 の行は保存前にその場で警告（サーバーに行く前に気づける）。
- 入力のある行（= 出場扱いになる行）を視覚的にハイライトする。
- 保存失敗時はドメイン例外の日本語メッセージを画面上部の alert に表示（ページ遷移なし・入力は保持）。
- 見た目は既存の Bootstrap + `theme.css`（`entry-table` / `linescore-table` 等）をそのまま使い、他画面と揃える。

### 既存フォーム POST 経路の扱い

React 化後、`game_edit` ビューの POST 処理と formset 描画は**削除**する
（保存経路を2つ残すと検証・文言の二重管理になる。「両立しない操作を並べない」）。
ビューは GET 専用（payload 組み立て + 描画）になる。

## payload / API の形

```jsonc
// テンプレート埋め込み payload（json_script）
{
  "game": {"id": 1, "year": 2026, "played_on": "2026-04-01",
            "home_team": 1, "away_team": 2, "home_score": 3, "away_score": 2},
  "innings": [{"inning": 1, "away": 0, "home": 1}, ...],   // 12個。未実施の回は null
  "rosters": [
    {"team_id": 1, "team_name": "...", "is_home": true,
     "batters": [{"player_id": 5, "name": "...", "number": 1,
                   "batting_order": 1, "slot_sequence": 0, "fielding_position": "遊",
                   "at_bats": 4, "singles": 1, ...}],       // 未入力は null
     "pitchers": [{"player_id": 9, "name": "...", "number": 18,
                    "entered_inning": 1, "innings_pitched": "6.1",
                    "earned_runs": 2, ...}]}
  ],
  "fielding_positions": ["投", "捕", ...],
  "max_innings": 12,
  "urls": {"save": "/api/games/1/", "detail": "/games/1/", "list": "/games/"},
  "csrf_token": "..."
}
```

```jsonc
// POST /api/games/<id>/ のリクエスト（キーはフォームのフィールド名と同じ）
{
  "year": 2026, "played_on": "2026-04-01",
  "home_team": 1, "away_team": 2, "home_score": 3, "away_score": 2,
  "innings": [{"inning": 1, "away": 0, "home": 1}, ...],
  "batting":  [{"player_id": 5, "batting_order": 1, ...}],   // 全行送る。空行はサーバーが間引く
  "pitching": [{"player_id": 9, "entered_inning": 1, "innings_pitched": "6.1", ...}]
}
```

## ファイル配置

```
frontend/
  package.json / package-lock.json / tsconfig.json / vite.config.ts
  src/
    lib/api.ts            # fetch ラッパー（CSRF ヘッダ付与・エラー整形）
    game_edit/
      main.tsx            # json_script を読んで #game-edit-root にマウント
      types.ts            # payload と 1:1 の型定義
      App.tsx ほかコンポーネント
myapp/static/myapp/dist/  # ビルド成果物（gitignore）
myapp/presentation/api.py # 保存 API（JSON を forms で検証 → application 呼び出し）
```

- URL 追加: `myapp/urls.py` に `path("api/games/<int:game_id>/", api.game_update, name="api_game_update")`
- Makefile 追加: `frontend-build`（本番ビルド）、`frontend-check`（`tsc --noEmit`）
- docker-compose: `frontend` サービス追加

## テスト計画

| 層 | 内容 |
|---|---|
| domain | 変更なし（ドメインは触らない） |
| integration | `test_game_edit_api.py`: 保存 API の正常系／ドメインエラーが日本語メッセージで 400 になる／未ログイン・権限なしで拒否／不正 JSON で 400。`game_edit` テンプレートに root 要素と json_script が描画されること |
| e2e | `test_game_edit_react.py`: ログイン → 編集画面 → イニングスコアと打撃1行を入力 → 保存 → 試合詳細に反映を確認。**ビルド済みアセットが前提**（無ければ `make frontend-build` を促すメッセージで失敗させる。skip にはしない） |

## コミット計画

1. **ビルド基盤**: 設計ドキュメント + `frontend/` 足場 + compose サービス + Makefile + .gitignore + README（環境・構成の節）
2. **試合編集画面の React 化**: `api.py` + React 実装 + テンプレート差し替え + テスト + README（画面・アーキテクチャの節）+ 本ドキュメントの吸収・削除

## 進捗チェックリスト

- [ ] コミット1: ビルド基盤（frontend/ 足場、frontend サービス、Makefile、.gitignore、README）
- [ ] コミット2: 保存 API（presentation/api.py + urls）
- [ ] コミット2: React 画面（game_edit 一式）とテンプレート差し替え・旧 POST 経路の削除
- [ ] コミット2: integration テスト（API・テンプレート）
- [ ] コミット2: e2e テスト（Playwright）
- [ ] コミット2: ruff / mypy / フルテスト通過
- [ ] コミット2: README へ吸収・このファイルを削除・ROADMAP 更新
