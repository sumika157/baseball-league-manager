"""ペナントの世界のシーズンを進める（日程が無ければ、先に開幕年の日程を作る）。

    docker compose exec web python manage.py pennant_advance --world 3 --to week

`--to` は進める単位:

| 値 | 内容 |
| --- | --- |
| `day` | 次に試合がある日まで（試合のない日は数えない） |
| `week` | 次に試合がある日から7日間 |
| `next-game` | 自軍（世界の受け持つ球団）の次の試合の日まで |
| `month` | 次に試合がある日の月の末まで |
| `season-end` | 未消化の日程をすべて |

1日ずつ進めても1週間まとめて進めても、同じ結果になる（試合ごとの乱数は世界のシードと試合から作る）。
実データには触れない。
"""

from __future__ import annotations

import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from myapp.domain.exceptions import DomainError
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.presentation.views import build_pennant_season_service

TARGETS = {
    "day": AdvanceTarget.DAY,
    "week": AdvanceTarget.WEEK,
    "next-game": AdvanceTarget.NEXT_MANAGED_GAME,
    "month": AdvanceTarget.MONTH_END,
    "season-end": AdvanceTarget.SEASON_END,
}


class Command(BaseCommand):
    help = "ペナントの世界のシーズンを進める"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--world", type=int, required=True, metavar="ID", help="進める世界の id")
        parser.add_argument(
            "--to",
            dest="target",
            choices=sorted(TARGETS),
            default="day",
            help="どこまで進めるか（既定は day）",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        started = time.perf_counter()
        try:
            report = build_pennant_season_service(options["world"]).advance(TARGETS[options["target"]])
        except DomainError as error:
            raise CommandError(str(error)) from error
        elapsed = time.perf_counter() - started

        if report.schedule_created:
            self.stdout.write("開幕年の日程を作りました")
        if not report.played_dates:
            self.stdout.write("進める日程がありません（シーズンは終了しています）")
            return
        first, last = report.played_dates[0], report.played_dates[-1]
        self.stdout.write(
            self.style.SUCCESS(
                f"{first} 〜 {last}（{len(report.played_dates)}日・{report.games}試合）を進めました"
                f"　所要 {elapsed:.2f}秒"
            )
        )
        if report.season_finished:
            self.stdout.write("日程をすべて消化しました（シーズン終了）")
        else:
            self.stdout.write(f"未消化の日程: {report.remaining_fixtures}試合")
