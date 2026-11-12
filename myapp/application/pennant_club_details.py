"""編成画面に並べる選手の、選ぶ材料（年齢・能力の要約・今季の成績）。

読むだけで、書き込みはしない。編成（`ClubManagementService`）とは別の小さなサービスにして、
あちらに能力の表示や成績の知識を足さない。年は編成の画面が使っている年（`ClubPlanView.year`。
能力を引く年と今季は同じ規則で決まる）を渡してもらい、ここで決め直さない。

読む量は選手の人数によらず一定: 球団の選手と成績を参照クエリで1回、能力を1回（選手ごとには引かない）。
「戦力外」タブの見込み（#308 P3）は、世界を1回と球団の在籍を1回足して読む。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date

from ..domain.repositories import RatingsRepository, WorldRepository
from .dto import (
    ActivePlayerStats,
    ClubBattingStat,
    ClubPitchingStat,
    ClubPlayerDetail,
    ClubPlayerRow,
    ClubTenure,
    ReleasePlayerRow,
)
from .pennant_outlook import outlook_cell
from .pennant_ratings import overall_cell, rating_cells
from .queries import ClubTenureQuery, PlayerStatsQuery


class ClubDetailsService:
    """編成画面の選手の詳細を作る。

    参照クエリとリポジトリは世界 `world_id` の範囲で組み立てて渡す。`today` は年齢の基準日。"""

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        stats: PlayerStatsQuery,
        ratings: RatingsRepository,
        tenures: ClubTenureQuery,
        today: Callable[[], date],
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._stats = stats
        self._ratings = ratings
        self._tenures = tenures
        self._today = today

    def details[Row: (ClubPlayerRow, ReleasePlayerRow)](
        self, team_id: int, year: int, players: Sequence[Row], *, with_outlook: bool = False
    ) -> tuple[ClubPlayerDetail[Row], ...]:
        """`players` と同じ並びで、年 `year` の能力と今季（`year` 年）の成績・年齢を添える。

        `with_outlook` を立てると、`team_id` が受け持つ球団のときだけ、`year` 年の能力から見た見込みの区分も添える
        （「戦力外」タブ。締める年の能力・その年の4月1日の年齢で、締める処理と同じ材料から導く）。
        """
        rows = {row.player_id: row for row in self._stats.list_roster(year=year, team_id=team_id, with_profile=True)}
        ratings = {
            item.player_id: item for item in self._ratings.find_by_players([p.player_id for p in players], year)
        }
        world = self._worlds.find_by_id(self._world_id) if with_outlook else None
        tenures: dict[int, ClubTenure] = (
            self._tenures.for_players(team_id, [p.player_id for p in players])
            if world is not None and world.managed_team_id == team_id
            else {}
        )
        today = self._today()
        details = []
        for player in players:
            row = rows.get(player.player_id)
            found = ratings.get(player.player_id)
            tenure = tenures.get(player.player_id)
            details.append(
                ClubPlayerDetail(
                    player=player,
                    age=row.profile.age_or_none(today) if row is not None else None,
                    overall=overall_cell(found.ratings) if found is not None else None,
                    cells=rating_cells(found.ratings) if found is not None else (),
                    batting=_batting_of(row) if row is not None and not player.position.is_pitcher else None,
                    pitching=_pitching_of(row) if row is not None and player.position.is_pitcher else None,
                    outlook=(
                        outlook_cell(
                            found.ratings,
                            tenure=tenure,
                            world=world,
                            team_id=team_id,
                            player_id=player.player_id,
                            year=year,
                        )
                        if found is not None and tenure is not None and world is not None
                        else None
                    ),
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
