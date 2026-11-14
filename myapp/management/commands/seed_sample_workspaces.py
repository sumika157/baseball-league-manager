"""見本つきのワークスペースを大量に作る・消す（ワークスペースが多い状態の実測のための道具。仕上げ T1 #74）。

    docker compose exec web python manage.py seed_sample_workspaces --count 200
    docker compose exec web python manage.py seed_sample_workspaces --count 200 --prefix load --private
    docker compose exec web python manage.py seed_sample_workspaces --delete              # 確認してから消す
    docker compose exec web python manage.py seed_sample_workspaces --delete --yes        # 確認を省く

- 作る: `<prefix>-0001`…の N 人のユーザー（パスワードは使えない値。ログインできない）を作り、1人につき1つ、
  見本ありのワークスペースを作る。作成は画面と同じサービス（`build_workspace_service().create`）を通すので、
  持てる数の上限と見本の生成もそのまま効く（上限を素通りする口は無い）。既定は公開（`measure_pages` を
  未ログインで測れるように）。`--private` で非公開。10個ごとに進み具合を、最後に合計と1個あたりの時間を出す。
- 消す（`--delete`）: その prefix のユーザーが持つワークスペースを、`workspace_delete` と同じサービス
  （`WorkspaceService.delete`）で消し、続けてユーザーを消す。ほかの prefix・ほかのユーザーには触れない。
- `DEBUG` が偽（本番）なら、作るのも消すのも拒む（本番に計測用のアカウントを作らせない）。
- 同じ prefix のユーザーが1人でもいれば作らずに止まる（先に `--delete` で片付ける）。
"""

from __future__ import annotations

import re
import time
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from myapp.domain.exceptions import DomainError
from myapp.domain.workspace import Visibility, WorkspaceRole
from myapp.management.workspaces import real_scope_for_key
from myapp.presentation.views import build_workspace_list_query, build_workspace_service

DEFAULT_PREFIX = "load"
CONFIRMATION = "yes"
PROGRESS_EVERY = 10
# 利用者名に使える文字（Django の既定の利用者名の検査より狭くする。prefix は正規表現にも埋める）
PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def username(prefix: str, number: int) -> str:
    """計測用のユーザー名（`load-0001` の形。番号は1から）。"""
    return f"{prefix}-{number:04d}"


def _users_of(prefix: str) -> list[Any]:
    """その prefix で作ったユーザー（`<prefix>-<数字>` の名前の人だけ。`load-test` のような名前は拾わない）。"""
    pattern = re.compile(rf"^{re.escape(prefix)}-\d+$")
    candidates = get_user_model().objects.filter(username__startswith=f"{prefix}-").order_by("username")
    return [user for user in candidates if pattern.match(user.username)]


class Command(BaseCommand):
    help = "見本つきのワークスペースを大量に作る・消す（計測用。DEBUG のときだけ）"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--count", type=int, default=None, help="作るユーザーとワークスペースの数")
        parser.add_argument(
            "--prefix", default=DEFAULT_PREFIX, help=f"ユーザー名の頭（既定 {DEFAULT_PREFIX}。<prefix>-0001 の形）"
        )
        parser.add_argument("--private", action="store_true", help="非公開で作る（既定は公開）")
        parser.add_argument("--delete", action="store_true", help="その prefix のワークスペースとユーザーを消す")
        parser.add_argument("--yes", action="store_true", help="--delete の確認を省く")

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG:
            raise CommandError("このコマンドは計測用です。DEBUG が偽の環境（本番）では動かしません。")
        prefix = options["prefix"]
        if not PREFIX_PATTERN.match(prefix):
            raise CommandError("--prefix は英数字と _ だけで指定してください。")
        if options["delete"]:
            if options["count"] is not None or options["private"]:
                raise CommandError("--delete は --count・--private と一緒に指定できません。")
            self._delete(prefix, confirmed=options["yes"])
            return
        if options["yes"]:
            raise CommandError("--yes は --delete と一緒に指定してください。")
        count = options["count"]
        if count is None or count < 1:
            raise CommandError("--count で作る数（1以上）を指定してください。")
        visibility = Visibility.PRIVATE if options["private"] else Visibility.PUBLIC
        self._create(prefix, count, visibility)

    def _create(self, prefix: str, count: int, visibility: Visibility) -> None:
        if existing := _users_of(prefix):
            raise CommandError(
                f"prefix「{prefix}」のユーザーがすでに {len(existing)} 人います。"
                f"先に --prefix {prefix} --delete で消してください。"
            )
        service = build_workspace_service()
        users = get_user_model().objects
        started = time.perf_counter()
        for number in range(1, count + 1):
            name = username(prefix, number)
            try:
                # ユーザーとワークスペースを一緒に作る（失敗したら、ワークスペースの無いユーザーを残さない。
                # サービスのトランザクションはこの中で入れ子になる）
                with transaction.atomic():
                    # パスワードを渡さないと使えない値（`set_unusable_password`）が入る。ログインできない
                    user = users.create_user(username=name, password=None)
                    service.create(
                        owner_id=user.pk, name=f"{name} の見本", visibility=visibility, with_sample_league=True
                    )
            except DomainError as error:
                raise CommandError(
                    f"{name} のワークスペースを作れませんでした: {error}\n"
                    f"作れた {number - 1} 個は残っています。--prefix {prefix} --delete で片付けられます。"
                ) from error
            if number % PROGRESS_EVERY == 0 or number == count:
                self.stdout.write(f"{number}/{count} 個（{time.perf_counter() - started:.1f}秒）")
        elapsed = time.perf_counter() - started
        self.stdout.write(
            self.style.SUCCESS(
                f"見本つきのワークスペースを {count} 個作りました（{visibility.label}）: "
                f"合計 {elapsed:.1f}秒・1個あたり {elapsed / count * 1000:.0f}ms"
            )
        )

    def _delete(self, prefix: str, *, confirmed: bool) -> None:
        users = _users_of(prefix)
        if not users:
            self.stdout.write(f"prefix「{prefix}」のユーザーはいません（何も消していません）。")
            return
        listing = build_workspace_list_query()
        keys = [row.key for user in users for row in listing.list_for(user.pk) if row.role is WorkspaceRole.OWNER]
        self.stdout.write(
            f"prefix「{prefix}」のユーザー {len(users)} 人と、その人が持つワークスペース {len(keys)} 個を消します"
            "（元に戻せません）"
        )
        if not confirmed:
            try:
                answer = input(f"消すには {CONFIRMATION} と入力してください: ")
            except EOFError:  # 入力の無い実行（`exec -T` など）は、確かめられないので取りやめる
                answer = ""
            if answer.strip() != CONFIRMATION:
                raise CommandError("取りやめました（何も消していません）。")
        service = build_workspace_service()
        started = time.perf_counter()
        for index, key in enumerate(keys, start=1):
            try:
                service.delete(real_scope_for_key(key).workspace_id)
            except DomainError as error:
                # ユーザーは消さない（持ち主のワークスペースが残ると、ユーザーの削除は PROTECT で落ちる）
                raise CommandError(
                    f"ワークスペース {key} を消せませんでした: {error}\n"
                    "ユーザーは消していません。直してから --delete をやり直してください。"
                ) from error
            if index % PROGRESS_EVERY == 0 or index == len(keys):
                self.stdout.write(f"{index}/{len(keys)} 個（{time.perf_counter() - started:.1f}秒）")
        get_user_model().objects.filter(pk__in=[user.pk for user in users]).delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"ワークスペース {len(keys)} 個とユーザー {len(users)} 人を消しました（prefix「{prefix}」）"
            )
        )
