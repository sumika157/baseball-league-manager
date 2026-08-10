# 開発用の短縮コマンド集（WSL のターミナルから `make <ターゲット>` で実行する）
# コマンドの実体はすべて Docker コンテナ経由（CLAUDE.md の実行環境規則に従う）

.DEFAULT_GOAL := help

EXEC := docker compose exec web

.PHONY: help
help: ## ターゲット一覧を表示する
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---- テスト ----

.PHONY: test
test: ## フルスイート（domain + integration + e2e。t=myapp.tests.xxx で個別指定）
	$(EXEC) python manage.py test $(t)

.PHONY: test-domain
test-domain: ## domain 層のみ（Django 設定なし・DB 不要・最速）
	docker compose exec -e DJANGO_SETTINGS_MODULE= web python -m unittest discover -s myapp/tests/domain -t .

.PHONY: test-integration
test-integration: ## integration 層のみ（リポジトリ往復・画面動作・テンプレート検査）
	$(EXEC) python manage.py test myapp.tests.integration

.PHONY: test-e2e
test-e2e: ## E2E のみ（Playwright 実ブラウザ・遅い）
	$(EXEC) python manage.py test myapp.tests.e2e

# ---- 品質チェック ----

.PHONY: lint
lint: ## ruff check + mypy（コミット前に必須）
	$(EXEC) ruff check .
	$(EXEC) mypy .

.PHONY: format
format: ## ruff format で整形する
	$(EXEC) ruff format .

# ---- Docker 運用 ----

.PHONY: up
up: ## コンテナをバックグラウンドで起動する（未適用マイグレーションは自動適用）
	docker compose up -d

.PHONY: down
down: ## コンテナを停止する
	docker compose down

.PHONY: ps
ps: ## コンテナの状態を確認する
	docker compose ps

.PHONY: logs
logs: ## web コンテナのログを追尾する
	docker compose logs -f web

.PHONY: build
build: ## イメージを再ビルドする（requirements*.txt を変更したとき）
	docker compose build --no-cache

# ---- Django 操作 ----

.PHONY: migrate
migrate: ## マイグレーションを適用する
	$(EXEC) python manage.py migrate

.PHONY: makemigrations
makemigrations: ## マイグレーションを作成する
	$(EXEC) python manage.py makemigrations

.PHONY: showmigrations
showmigrations: ## マイグレーションの適用状況を確認する
	$(EXEC) python manage.py showmigrations

.PHONY: check
check: ## Django の設定ミスを検査する
	$(EXEC) python manage.py check

.PHONY: shell
shell: ## Django シェルを開く
	$(EXEC) python manage.py shell

.PHONY: bash
bash: ## web コンテナの bash を開く
	$(EXEC) bash

.PHONY: superuser
superuser: ## 管理ユーザーを作成する
	$(EXEC) python manage.py createsuperuser
