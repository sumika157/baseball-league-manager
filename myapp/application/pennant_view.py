"""ペナントの世界の「いま」を読む。世界バーと世界の一覧の材料。

読むだけで、書き込みはしない。順位・試合・選手の参照は世界の範囲で組み立てた
`TeamApplicationService` が受け持ち、ここは世界の見出し（今日・局面・受け持つ球団）だけを作る。

見出しの材料は `WorldSummaryQuery` が**世界の数にかかわらず一定のクエリ数で**まとめて読む。
世界の一覧で世界ごとに読み直さないため。自軍の順位だけは世界ごとの計算になる（`standings_for` で
世界の範囲のサービスを受け取り、その年の試合だけを読む）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from ..domain.pennant.season import is_final_season, season_phase, season_year
from ..domain.pennant.world import MAX_WORLDS_PER_OWNER
from .dto import OwnRace, OwnStanding, PennantWorldList, PennantWorldListRow, WorldContext, WorldSummary
from .pennant_race import own_race, tiebreak_for
from .queries import GameListQuery, WorldSummaryQuery
from .services import TeamApplicationService


def _context_of(summary: WorldSummary) -> WorldContext:
    year = season_year(
        next_fixture_on=summary.next_fixture_on, last_played_on=summary.last_played_on, start_year=summary.start_year
    )
    return WorldContext(
        world_id=summary.world_id,
        name=summary.name,
        phase=season_phase(last_played_on=summary.last_played_on, next_fixture_on=summary.next_fixture_on),
        season_year=year,
        today=summary.last_played_on,
        managed_team_id=summary.managed_team_id,
        managed_team_name=summary.managed_team_name,
        default_league_id=summary.default_league_id,
        owner_id=summary.owner_id,
        is_final_season=is_final_season(current_year=year, start_year=summary.start_year),
    )


class PennantWorldViewService:
    """世界の見出しを作る。`standings_for` は、世界の id から、その世界の範囲で組み立てたサービスを返す。"""

    def __init__(
        self,
        *,
        summaries: WorldSummaryQuery,
        standings_for: Callable[[int], TeamApplicationService],
        games_for: Callable[[int], GameListQuery],
    ) -> None:
        self._summaries = summaries
        self._standings_for = standings_for
        self._games_for = games_for

    def get_context(self, world_id: int) -> WorldContext:
        """無ければ WorldNotFound。"""
        return _context_of(self._summaries.get(world_id))

    def list_rows(self) -> list[PennantWorldListRow]:
        """世界の一覧の行。新しく作った世界から順に。順位は、受け持つ球団のいまのシーズンのもの。"""
        return [self._row_of(summary) for summary in self._summaries.list_all()]

    def list_worlds(self, viewer_id: int | None) -> PennantWorldList:
        """世界の一覧を、`viewer_id` の世界（オーナーが本人）とほかの人の世界に分ける。

        どちらも最後に進めた日が新しい順（まだ進めていない世界は後ろ）。`viewer_id` が None
        （未ログイン）なら、自分の世界は無い。オーナーのいない世界（コマンドで作った世界）は
        ほかの人の世界。
        """
        rows = sorted(
            self.list_rows(), key=lambda row: (row.context.today or date.min, row.context.world_id), reverse=True
        )
        mine = [row for row in rows if viewer_id is not None and row.context.owner_id == viewer_id]
        others = [row for row in rows if row not in mine]
        return PennantWorldList(mine=mine, others=others, world_limit=MAX_WORLDS_PER_OWNER)

    def _row_of(self, summary: WorldSummary) -> PennantWorldListRow:
        context = _context_of(summary)
        standing, race = self._own_standing(context, summary)
        return PennantWorldListRow(context=context, own_standing=standing, race=race)

    def _own_standing(self, context: WorldContext, summary: WorldSummary) -> tuple[OwnStanding | None, OwnRace | None]:
        """受け持つ球団のいまの順位と、優勝の確定・マジック。順位が無い（試合の無い年など）ときは (None, None)。"""
        year = context.season_year
        # 試合の無い年（締めた直後の翌年の開幕前）は、順位を出さない
        if context.managed_team_id is None or context.today is None or context.today.year != year:
            return None, None
        teams = self._standings_for(context.world_id)
        standings = teams.get_league_standings(year)
        for league in standings:
            for row in league.rows:
                if row.team_id == context.managed_team_id:
                    standing = OwnStanding(
                        rank=row.rank,
                        wins=row.wins,
                        losses=row.losses,
                        ties=row.ties,
                        games_behind=row.games_behind,
                    )
                    tiebreak = tiebreak_for(year, games=self._games_for(context.world_id), teams=teams)
                    race = own_race(standings, summary.remaining_by_team, context.managed_team_id, tiebreak)
                    return standing, race
        return None, None
