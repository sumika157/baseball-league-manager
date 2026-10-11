"""実データのリーグを分岐して、ペナントの世界（セーブデータ）を作る。

    docker compose exec web python manage.py pennant_create --name "2026年" --league 1 --league 2 --managed-team 1

写すのはリーグ・球団・選手・**現在の在籍だけ**（加入年は開幕年）。試合と過去の在籍は写さず、
球場は共有する。実データには何も書かない。作った世界は実データの画面・集計・管理画面に現れない。
消すときは `pennant_delete --world <id>`。
"""

from __future__ import annotations

import secrets
import time
from datetime import date
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from myapp.domain.exceptions import DomainError
from myapp.domain.pennant.world import MAX_SEED
from myapp.presentation.views import build_pennant_world_service


class Command(BaseCommand):
    help = "実データのリーグを分岐して、ペナントの世界を作る"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--name", required=True, help="世界の名前")
        parser.add_argument(
            "--league",
            dest="leagues",
            type=int,
            action="append",
            required=True,
            metavar="ID",
            help="分岐元の実データのリーグ id（複数回指定できる）",
        )
        parser.add_argument("--year", type=int, default=date.today().year, help="開幕年（既定は今年）")
        parser.add_argument("--seed", type=int, default=None, help="乱数のシード（既定は無作為）")
        parser.add_argument("--owner", default=None, metavar="USERNAME", help="オーナーにするユーザー名")
        parser.add_argument(
            "--managed-team",
            type=int,
            required=True,
            metavar="ID",
            help="受け持つ球団。分岐元の実データの球団 id で指す（受け持ちの無い世界は作らない）",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        owner_id = None
        if options["owner"]:
            user = get_user_model().objects.filter(username=options["owner"]).first()
            if user is None:
                raise CommandError(f"ユーザーが見つかりません: {options['owner']}")
            owner_id = user.pk

        seed = options["seed"] if options["seed"] is not None else secrets.randbelow(MAX_SEED)

        started = time.perf_counter()
        try:
            created = build_pennant_world_service().create_world(
                name=options["name"],
                owner_id=owner_id,
                source_league_ids=options["leagues"],
                start_year=options["year"],
                seed=seed,
                managed_source_team_id=options["managed_team"],
            )
        except DomainError as error:
            raise CommandError(str(error)) from error
        elapsed = time.perf_counter() - started

        self.stdout.write(
            self.style.SUCCESS(
                f"世界 {created.world.id}「{created.world.name}」を作りました"
                f"（開幕年 {created.world.start_year} / シード {created.world.seed}）"
            )
        )
        self.stdout.write(
            f"リーグ {created.league_count} / 球団 {created.team_count} / 選手 {created.player_count}人"
            f"（初期能力 {created.rating_count}人ぶん）　所要 {elapsed:.2f}秒"
        )
