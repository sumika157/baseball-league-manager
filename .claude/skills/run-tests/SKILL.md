---
name: run-tests
description: このプロジェクト（Docker上のDjangoアプリ）でテストを実行する。「テスト実行して」「テスト通して」「domainだけテストして」のように頼まれたときに使う。DBが不要なdomain層テストの高速実行と、DBが必要な統合テストを含むフルスイート実行を切り替える。
---

テスト実行の手順。**基本コマンド（フルスイート・domain のみ）と lint / 型チェックのゲートは
`CLAUDE.md`（実行環境・テスト）が出典**で、ここには「どれを実行するか」の判断と、
CLAUDE.md に無い個別の実行方法・診断だけを書く。

## 1. コンテナが動いているか確認する

```bash
docker compose ps
```

`web` が `Up` でなければ先に起動する。**起動は WSL 側から**（`docker-ops` スキル参照。Windows 側から
`up` するとマウントが壊れる）。

## 2. どれを実行するか判断する

| 頼まれ方 | 実行内容 |
| --- | --- |
| 「テスト実行して」「全部テストして」など指定なし | フルスイート（CLAUDE.md のコマンド） |
| 「domainだけ」「ドメイン層のテストだけ」「速いテストだけ」 | domain層のみ・DB不要（CLAUDE.md のコマンド） |
| 「E2Eだけ」「ブラウザテストだけ」 | 下記 §4 |
| ファイル・テストケース名の指定あり | 下記 §3 |

フルスイートには `tests/e2e/`（Playwright 実ブラウザ）も含まれ、そのぶん遅い。

## 3. 一部だけ実行する

```bash
docker compose exec web python manage.py test myapp.tests.integration
docker compose exec web python manage.py test myapp.tests.integration.test_games
docker compose exec web python manage.py test myapp.tests.domain.test_entities.PlayerTest
```

`tests/integration/` は対象ごとにファイルが分かれている（`test_players.py`・`test_games.py`・
`test_admin_validation.py` など）。変更した範囲のファイルだけ回すと速い。
domain 層を個別に、Django 設定なしで回す場合は `python -m unittest myapp.tests.domain.test_value_objects`。

## 4. E2Eのみ（Playwright実ブラウザ、DB必要）

```bash
docker compose exec web python manage.py test myapp.tests.e2e
```

- 実行前にフロントエンドのビルド成果物が必要（`make frontend-build`。`up` 済みなら watch で最新）。
- コンテナ内の Chromium（headless）で動く。`docker-compose.yml` に `shm_size: '1gb'` 設定済み
  （既定の64MBだと Chromium がクラッシュしやすい）。
- ブラウザが見つからないエラーが出たらイメージが古い。再ビルドする（`docker-ops` 参照）。

## 5. Lint・型チェック

「lintも」「型チェックも」と言われたら、あわせて実行する（コミット前は必須。対象コマンドは CLAUDE.md）。

## 5.5 性能の確認

「遅い」「重い」「速くして」と言われたら、まず実測する。テストでは出ない（テストのデータ量が
小さいため）。判断基準は `.claude/rules/performance.md`。

```bash
docker compose exec web python manage.py measure_pages
MSYS_NO_PATHCONV=1 docker compose exec web python manage.py measure_pages --profile /games/
```

## 6. 失敗したときの一次診断

- `ImproperlyConfigured: ... DJANGO_SECRET_KEY ...` → `.env` が無い、または読み込まれていない。コンテナ再起動を確認する。
- domain層テストのはずが Django のエラーが出る → 対象ファイルが誤って Django に依存するコードを import していないか確認する（`ddd-boundary-reviewer` agent でレビューできる）。
- `No module named 'myapp.tests...'` → 移動・分割したテストファイルへの古い import が残っている。
- 統合テストのみ失敗し domain層は通る → `myapp/infrastructure/` か `myapp/presentation/` 側の変更を疑う。
- E2Eのみ失敗し integration は通る → 静的ファイル・JS・テンプレートの配線を疑う（E2Eだけが実ブラウザでCSS/JSまで読む）。
- 型注釈を足した直後に mypy の指摘が増えた → 想定どおり（注釈が無い関数は検査されていなかった）。CLAUDE.md の「型と静的検査」参照。
