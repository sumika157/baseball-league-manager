"""ペナントの世界の「いま」を読む。世界バーと世界の一覧の材料。

読むだけで、書き込みはしない。順位・試合・選手の参照は世界の範囲で組み立てた
`TeamApplicationService` が受け持ち、ここは世界の見出し（今日・局面・受け持つ球団）だけを作る。

見出しの材料は `WorldSummaryQuery` が**世界の数にかかわらず一定のクエリ数で**まとめて読む。
世界の一覧で世界ごとに読み直さないため。自軍の順位だけは世界ごとの計算になる（`standings_for` で
世界の範囲のサービスを受け取り、その年の試合だけを読む）。
"""

from __future__ import annotations

from collections.abc import Callable

from ..domain.pennant.season import season_phase
from .dto import OwnStanding, PennantWorldListRow, WorldContext, WorldSummary
from .queries import WorldSummaryQuery
from .services import TeamApplicationService


def _context_of(summary: WorldSummary) -> WorldContext:
    return WorldContext(
        world_id=summary.world_id,
        name=summary.name,
        phase=season_phase(
            has_played=summary.last_played_on is not None,
            fixtures_pending=summary.has_pending_fixtures,
        ),
        today=summary.last_played_on,
        managed_team_id=summary.managed_team_id,
        managed_team_name=summary.managed_team_name,
        default_league_id=summary.default_league_id,
    )


class PennantWorldViewService:
    """世界の見出しを作る。`standings_for` は、世界の id から、その世界の範囲で組み立てたサービスを返す。"""

    def __init__(
        self,
        *,
        summaries: WorldSummaryQuery,
        standings_for: Callable[[int], TeamApplicationService],
    ) -> None:
        self._summaries = summaries
        self._standings_for = standings_for

    def get_context(self, world_id: int) -> WorldContext:
        """無ければ WorldNotFound。"""
        return _context_of(self._summaries.get(world_id))

    def list_rows(self) -> list[PennantWorldListRow]:
        """世界の一覧の行。新しく作った世界から順に。順位は、受け持つ球団のいまのシーズンのもの。"""
        return [self._row_of(summary) for summary in self._summaries.list_all()]

    def _row_of(self, summary: WorldSummary) -> PennantWorldListRow:
        context = _context_of(summary)
        season_year = context.today.year if context.today is not None else summary.start_year
        return PennantWorldListRow(
            context=context,
            season_year=season_year,
            own_standing=self._own_standing(context, season_year),
        )

    def _own_standing(self, context: WorldContext, year: int) -> OwnStanding | None:
        if context.managed_team_id is None or context.today is None:
            return None
        for league in self._standings_for(context.world_id).get_league_standings(year):
            for row in league.rows:
                if row.team_id == context.managed_team_id:
                    return OwnStanding(
                        rank=row.rank,
                        wins=row.wins,
                        losses=row.losses,
                        ties=row.ties,
                        games_behind=row.games_behind,
                    )
        return None
