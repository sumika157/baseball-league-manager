# 開発用の短縮コマンド集（WSL のターミナルから `make <ターゲット>` で実行する）
# コマンドの実体はすべて Docker コンテナ経由（CLAUDE.md の実行環境規則に従う）

.DEFAULT_GOAL := help

# worktree で作業しているときは WT=<worktree名> を付ける（例: make test WT=player-nationality）。
# バインドマウントは main の作業ツリー全体（`.:/app`）なので、.claude/worktrees/<名前> も
# 同じコンテナから /app/.claude/worktrees/<名前> として見える。-w でそこを指すだけでよい。
#
# make と docker コマンドは必ず main の作業ツリーから実行する。worktree の中から実行すると
# compose が worktree 側の docker-compose.yml と（gitignore で存在しない）.env を読み、
# 別プロジェクト＝別コンテナを作りにいって失敗する。
WT ?=
WORKDIR := $(if $(WT),/app/.claude/worktrees/$(WT),/app)

# worktree の中から WT 無しで実行したら止める。そのまま通すと main のコードを検査・テストして
# 「通ったのに直っていない」形になり、エラーにならないぶん気づけない。
ifneq (,$(findstring /.claude/worktrees/,$(CURDIR)))
ifeq (,$(WT))
$(error worktree の中にいます。main の作業ツリーへ移り、WT=<worktree名> を付けて実行してください)
endif
endif

# コンテナは root で動くため、作業ツリーに生成される __pycache__ や各種キャッシュも root 所有になる。
# worktree の中にそれが残ると git worktree remove が「Directory not empty」で失敗し、削除に sudo が
# 必要になるので、生成物はコンテナの /tmp に逃がす（キャッシュはコンテナの寿命だけ持てば足りる）。
CACHE_ENV := -e PYTHONPYCACHEPREFIX=/tmp/pycache -e RUFF_CACHE_DIR=/tmp/ruff-cache -e MYPY_CACHE_DIR=/tmp/mypy-cache

EXEC := docker compose exec -w $(WORKDIR) $(CACHE_ENV) web

.PHONY: help
help: ## ターゲット一覧を表示する
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: where
where: ## どの作業ツリーを対象にしているか表示する（worktree の取り違え確認用）
	@echo "対象       : $(if $(WT),worktree「$(WT)」,main の作業ツリー)"
	@echo "コンテナ内 : $(WORKDIR)"
	@echo "ブランチ   : $(shell git --git-dir=$(if $(WT),.git/worktrees/$(WT),.git) rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(不明)')"

# ---- テスト ----

.PHONY: test
test: ## フルスイート（domain + integration + e2e。t=myapp.tests.xxx で個別指定）
	$(EXEC) python manage.py test $(t)

.PHONY: test-domain
test-domain: ## domain 層のみ（Django 設定なし・DB 不要・最速）
	docker compose exec -w $(WORKDIR) $(CACHE_ENV) -e DJANGO_SETTINGS_MODULE= web python -m unittest discover -s myapp/tests/domain -t .

.PHONY: test-integration
test-integration: ## integration 層のみ（リポジトリ往復・画面動作・テンプレート検査）
	$(EXEC) python manage.py test myapp.tests.integration

.PHONY: test-e2e
test-e2e: ## E2E のみ（Playwright 実ブラウザ・遅い）
	$(EXEC) python manage.py test myapp.tests.e2e

# ---- フロントエンド ----
# React 画面のビルドは frontend コンテナで行う（ホストに Node 環境は作らない）。
# `make up` で watch ビルドも一緒に起動する。単発で成果物を作るときは frontend-build。
# node_modules と dist は gitignore なので worktree には無い。worktree で React を
# 触るときは、その worktree で frontend-build を先に実行する（E2E の前提でもある）。

.PHONY: frontend-build
frontend-build: ## React 画面をビルドする（E2E テスト実行の前提）
	docker compose run --rm -w $(WORKDIR)/frontend frontend sh -c "npm install && npm run build"

.PHONY: frontend-check
frontend-check: ## TypeScript の型チェック（tsc --noEmit）
	docker compose run --rm -w $(WORKDIR)/frontend frontend sh -c "npm install && npm run typecheck"

# ---- 品質チェック ----

.PHONY: lint
lint: ## ruff check + ruff format --check + mypy（コミット前に必須）
	$(EXEC) ruff check .
	$(EXEC) ruff format --check .
	$(EXEC) mypy .

.PHONY: format
format: ## ruff format で整形する（lint が整形漏れを指摘したらこれを実行する）
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
