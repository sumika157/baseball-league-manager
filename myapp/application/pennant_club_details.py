"""編成画面に並べる選手の、選ぶ材料（年齢・能力の要約・今季の成績）。

読むだけで、書き込みはしない。編成（`ClubManagementService`）とは別の小さなサービスにして、
あちらに能力の表示や成績の知識を足さない。年は編成の画面が使っている年（`ClubPlanView.year`。
能力を引く年と今季は同じ規則で決まる）を渡してもらい、ここで決め直さない。

読む量は選手の人数によらず一定: 球団の選手と成績を参照クエリで1回、能力を1回（選手ごとには引かない）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date

from ..domain.repositories import RatingsRepository
from .dto import (
    ActivePlayerStats,
    ClubBattingStat,
    ClubPitchingStat,
    ClubPlayerDetail,
    ClubPlayerRow,
)
from .pennant_ratings import overall_cell, rating_cells
from .queries import PlayerStatsQuery


class ClubDetailsService:
    """編成画面の選手の詳細を作る。参照クエリとリポジトリは世界の範囲で組み立てて渡す。`today` は年齢の基準日。"""

    def __init__(self, *, stats: PlayerStatsQuery, ratings: RatingsRepository, today: Callable[[], date]) -> None:
        self._stats = stats
        self._ratings = ratings
        self._today = today

    def details(self, team_id: int, year: int, players: Sequence[ClubPlayerRow]) -> tuple[ClubPlayerDetail, ...]:
        """`players` と同じ並びで、年 `year` の能力と今季（`year` 年）の成績・年齢を添える。"""
        rows = {row.player_id: row for row in self._stats.list_roster(year=year, team_id=team_id, with_profile=True)}
        ratings = {
            item.player_id: item for item in self._ratings.find_by_players([p.player_id for p in players], year)
        }
        today = self._today()
        details = []
        for player in players:
            row = rows.get(player.player_id)
            found = ratings.get(player.player_id)
            details.append(
                ClubPlayerDetail(
                    player=player,
                    age=row.profile.age_or_none(today) if row is not None else None,
                    overall=overall_cell(found.ratings) if found is not None else None,
                    cells=rating_cells(found.ratings) if found is not None else (),
                    batting=_batting_of(row) if row is not None and not player.position.is_pitcher else None,
                    pitching=_pitching_of(row) if row is not None and player.position.is_pitcher else None,
                )
            )
        return tuple(details)


def _batting_of(row: ActivePlayerStats) -> ClubBattingStat | None:
    line = row.batting
    if line.plate_appearances == 0:
        return None
    return ClubBattingStat(
        plate_appearances=line.plate_appearances,
        batting_average=line.batting_average,
        home_runs=line.home_runs,
        runs_batted_in=line.runs_batted_in,
        ops=line.ops,
    )


def _pitching_of(row: ActivePlayerStats) -> ClubPitchingStat | None:
    line = row.pitching
    if line.innings.outs == 0:
        return None
    return ClubPitchingStat(
        innings_pitched=str(line.innings),
        earned_run_average=line.earned_run_average,
        wins=line.wins,
        losses=line.losses,
        saves=line.saves,
    )
