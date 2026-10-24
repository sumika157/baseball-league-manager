"""ペナントの世界の、締めた年のうち表彰の無い年のタイトル受賞者を記録する（#248 より前に締めた世界のため）。

    docker compose exec web python manage.py pennant_record_awards --world 3
    docker compose exec web python manage.py pennant_record_awards --world 3 --check   # 書かずに一覧するだけ

表彰はシーズンを締めるときに記録する。それより前に締めた年は表彰が無く、歴代の記録・球団史・選手ページに
その年の受賞者が出ない。このコマンドは、その年の受賞者を締める処理と同じ関数で導いて書く（**追加だけ**。
表彰のある年は飛ばすので、何度流しても同じ）。締めていない年には書かない。実データには触れない。
"""

from __future__ import annotations

import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from myapp.domain.exceptions import DomainError
from myapp.management.workspaces import world_scope
from myapp.presentation.views import build_pennant_offseason_service


class Command(BaseCommand):
    help = "ペナントの世界の、締めた年のうち表彰の無い年のタイトル受賞者を記録する"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--world", type=int, required=True, metavar="ID", help="世界の id")
        parser.add_argument(
            "--check", action="store_true", help="書かずに、表彰の無い締めた年を一覧する（あればエラーで終わる）"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["check"]:
            self._check(options["world"])
            return
        started = time.perf_counter()
        try:
            years = build_pennant_offseason_service(world_scope(options["world"])).record_missing_awards()
        except DomainError as error:
            raise CommandError(str(error)) from error
        elapsed = time.perf_counter() - started

        if not years:
            self.stdout.write("表彰の無い締めた年はありません（何も書いていません）")
            return
        labels = "・".join(f"{year}年" for year in years)
        self.stdout.write(self.style.SUCCESS(f"{labels}の表彰を記録しました　所要 {elapsed:.2f}秒"))

    def _check(self, world_id: int) -> None:
        try:
            years = build_pennant_offseason_service(world_scope(world_id)).missing_award_years()
        except DomainError as error:
            raise CommandError(str(error)) from error
        if not years:
            self.stdout.write("表彰の無い締めた年はありません")
            return
        labels = "・".join(f"{year}年" for year in years)
        raise CommandError(f"表彰の無い締めた年があります: {labels}（--check を外して流すと記録します）")
