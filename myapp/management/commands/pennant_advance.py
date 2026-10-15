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
| `postseason-end` | ポストシーズンの終わり（優勝が決まる）まで |

公式戦の日程をすべて消化した世界では、`day` と `next-game` はポストシーズンを進める（画面の「進める」と同じ）。
ポストシーズンは1回で最大 `MAX_GAMES_PER_ADVANCE` 試合までで止まるので、`postseason-end` で終わらなければもう一度呼ぶ。
1日ずつ進めても1週間まとめて進めても、同じ結果になる（試合ごとの乱数は世界のシードと試合から作る）。
実データには触れない。
"""

from __future__ import annotations

import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from myapp.application.dto import PostseasonAdvanceReport
from myapp.domain.exceptions import DomainError
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.season import POSTSEASON_SCREEN_TARGETS
from myapp.presentation.views import build_pennant_postseason_service, build_pennant_season_service

TARGETS = {
    "day": AdvanceTarget.DAY,
    "week": AdvanceTarget.WEEK,
    "next-game": AdvanceTarget.NEXT_MANAGED_GAME,
    "month": AdvanceTarget.MONTH_END,
    "season-end": AdvanceTarget.SEASON_END,
    "postseason-end": AdvanceTarget.POSTSEASON_END,
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
        target = TARGETS[options["target"]]
        started = time.perf_counter()
        try:
            if target is AdvanceTarget.POSTSEASON_END:
                self._postseason(options["world"], target, started)
                return
            report = build_pennant_season_service(options["world"]).advance(target)
            if not report.played_dates and report.season_finished and target in POSTSEASON_SCREEN_TARGETS:
                # 公式戦が終わっていれば、同じ範囲でポストシーズンを進める（画面の「進める」と同じ）
                self._postseason(options["world"], target, started)
                return
        except DomainError as error:
            raise CommandError(str(error)) from error
        elapsed = time.perf_counter() - started

        if report.schedule_created:
            self.stdout.write("開幕年の日程を作りました")
        if not report.played_dates:
            self.stdout.write("進める日程がありません（シーズンは終了しています）")
            self._suggest_postseason(options["world"], report.season_finished)
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
            self._suggest_postseason(options["world"], report.season_finished)
        else:
            self.stdout.write(f"未消化の日程: {report.remaining_fixtures}試合")

    def _suggest_postseason(self, world_id: int, season_finished: bool) -> None:
        """公式戦を終えてポストシーズンが残っていれば、進め方を1行で案内する。

        week・month・season-end は公式戦だけを進めるので、黙って「シーズン終了」だけを出さない。
        """
        if not season_finished:
            return
        try:
            left = build_pennant_postseason_service(world_id).has_postseason_left()
        except DomainError as error:
            # 公式戦の結果は保存済み。案内のための読み取りで落とさず、理由だけ知らせる
            self.stderr.write(f"ポストシーズンの状況を確かめられませんでした: {error}")
            return
        if left:
            self.stdout.write("ポストシーズンが残っています（`--to postseason-end` で進めます）")

    def _postseason(self, world_id: int, target: AdvanceTarget, started: float) -> None:
        report = build_pennant_postseason_service(world_id).advance(target)
        self._write_postseason(report, time.perf_counter() - started)

    def _write_postseason(self, report: PostseasonAdvanceReport, elapsed: float) -> None:
        if not report.played_dates:
            self.stdout.write(f"進めるポストシーズンの試合がありません（{report.status.value}）")
            return
        first, last = report.played_dates[0], report.played_dates[-1]
        self.stdout.write(
            self.style.SUCCESS(
                f"ポストシーズン {first} 〜 {last}（{len(report.played_dates)}日・{report.games}試合）を進めました"
                f"　所要 {elapsed:.2f}秒"
            )
        )
        if report.finished:
            self.stdout.write(f"ポストシーズンが終わりました（優勝 {report.champion_name}）")
        else:
            self.stdout.write(f"ポストシーズンは{report.status.value}です")
