"""ペナントの世界のシーズンを締める（引退・ドラフト・成長と衰えを済ませ、翌年の日程を作る）。

    docker compose exec web python manage.py pennant_close_season --world 3

シーズンの日程をすべて消化した世界だけが締められる（`pennant_advance --to season-end` の後）。
元に戻せない。同じ年を2回締めようとすると、何も変えずにエラーにする。
実データには触れない。
"""

from __future__ import annotations

import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from myapp.domain.exceptions import DomainError
from myapp.presentation.views import build_pennant_offseason_service


class Command(BaseCommand):
    help = "ペナントの世界のシーズンを締める（引退・ドラフト・成長と衰え・翌年の日程）"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--world", type=int, required=True, metavar="ID", help="締める世界の id")

    def handle(self, *args: Any, **options: Any) -> None:
        started = time.perf_counter()
        try:
            result = build_pennant_offseason_service(options["world"]).close_season()
        except DomainError as error:
            raise CommandError(str(error)) from error
        elapsed = time.perf_counter() - started

        self.stdout.write(
            self.style.SUCCESS(
                f"{result.year}年のシーズンを締めました（引退 {result.retired_count}人・"
                f"新人 {result.draftee_count}人（うち外国人 {result.foreign_draftee_count}人））　所要 {elapsed:.2f}秒"
            )
        )
        self.stdout.write(f"{result.next_year}年の日程を作りました（{result.fixture_count}試合）")
        if result.without_ratings_count:
            self.stdout.write(f"能力の無い選手 {result.without_ratings_count}人は、翌年の能力を作りませんでした")
        if result.released_club_count:
            labels = "・".join(section.value for section in result.released_sections)
            own = f"（受け持ちの球団: {labels}）" if result.released_sections else ""
            self.stdout.write(f"引退した選手を含む編成を自動に戻した球団: {result.released_club_count}球団{own}")
