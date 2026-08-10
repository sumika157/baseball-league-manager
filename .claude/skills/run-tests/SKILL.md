---
name: run-tests
description: このプロジェクト（Docker上のDjangoアプリ）でテストを実行する。「テスト実行して」「テスト通して」「domainだけテストして」のように頼まれたときに使う。DBが不要なdomain層テストの高速実行と、DBが必要な統合テストを含むフルスイート実行を切り替える。
---

このプロジェクトのテストは Docker コンテナ内で実行する。ホスト側に Python 環境は無い。

## 1. コンテナが動いているか確認する

```bash
docker compose ps
```

`web` サービスが `Up` でなければ先に起動する（確認なしで実行してよい読み取り系コマンド。[[permission-prompts-only-for-writes]] 参照）。

```bash
docker compose up -d
```

## 2. どちらのテストを実行するか判断する

| 頼まれ方 | 実行内容 |
| --- | --- |
| 「テスト実行して」「全部テストして」など指定なし | フルスイート（下記） |
| 「domainだけ」「ドメイン層のテストだけ」「速いテストだけ」 | domain層のみ・DB不要（下記） |
| 「E2Eだけ」「ブラウザテストだけ」 | E2Eのみ（下記） |
| 特定ファイル・テストケース名の指定あり | `manage.py test` にそのパスを渡す |

## 3. フルスイート（DB必要、既定）

```bash
docker compose exec web python manage.py test
```

対象: `tests/domain/`（業務ルール。DB不要な内容も含む）+ `tests/integration/`（リポジトリ往復・画面動作・テンプレート検査）+ `tests/e2e/`（Playwright実ブラウザ。**フルスイートに含まれ、そのぶん遅い**）。

ディレクトリ・ファイル単位で実行する場合:

```bash
docker compose exec web python manage.py test myapp.tests.integration
docker compose exec web python manage.py test myapp.tests.domain.test_entities
```

## 4. domain層のみ（Django設定を読み込まない、最速）

```bash
docker compose exec -e DJANGO_SETTINGS_MODULE= web \
  python -m unittest discover -s myapp/tests/domain -t .
```

`tests/domain/` 配下の全ファイルが対象になる。個別に実行する場合は
`python -m unittest myapp.tests.domain.test_value_objects` のようにモジュール名で指定する。

## 5. E2Eのみ（Playwright実ブラウザ、DB必要）

```bash
docker compose exec web python manage.py test myapp.tests.e2e
```

- コンテナ内の Chromium（headless）で実行される。`docker-compose.yml` に `shm_size: '1gb'` が設定済み（既定の64MBだとChromiumがクラッシュしやすい）。
- Playwright まわりでブラウザが見つからないエラーが出たら、イメージが古い。`docker compose build --no-cache` で再ビルドする（`playwright install` は Dockerfile 内で実行される）。

## 6. Lint・型チェック

「lintも」「型チェックも」と言われたら、あわせて実行する（コミット前は必須。CLAUDE.md 参照）。

```bash
docker compose exec web ruff check .
docker compose exec web mypy .
```

## 7. 失敗したときの一次診断

- `ImproperlyConfigured: ... DJANGO_SECRET_KEY ...` → `.env` が無い、または読み込まれていない。コンテナ再起動を確認する。
- domain層テストのはずが Django のエラーが出る → 対象ファイルが誤って Django に依存するコードを import していないか確認する（[[ddd-boundary-reviewer]] agentでレビューできる）。
- 統合テストのみ失敗し domain層は通る → `myapp/infrastructure/` か `myapp/presentation/` 側の変更を疑う。
- E2Eのみ失敗し integration は通る → 静的ファイル・JS・テンプレートの配線を疑う（E2Eだけが実ブラウザでCSS/JSまで読む）。
