"""ペナントの世界を、属するリーグ・球団・選手・試合ごと消す。

    docker compose exec web python manage.py pennant_delete --world 3

実データには触れない。消した世界は元に戻せないので、必要なら先に `db.sqlite3` をバックアップする。
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from myapp.domain.exceptions import DomainError
from myapp.presentation.views import build_pennant_world_service


class Command(BaseCommand):
    help = "ペナントの世界を、属する行ごと消す"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--world", type=int, required=True, metavar="ID", help="消す世界の id")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            build_pennant_world_service().delete_world(options["world"])
        except DomainError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS(f"世界 {options['world']} を消しました"))
