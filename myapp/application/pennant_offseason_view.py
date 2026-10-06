"""ペナントの世界の「Y年オフの結果」を読む。読むだけで、書き込みはしない。

締める処理（`PennantOffseasonService`）は結果を保存しない。引退・退団した選手、新人、残った選手の能力の変化は、
在籍・プロフィール・能力の行から `OffseasonQuery` が導く。ここはその材料を画面向けの行にするだけで、
集約（`Team`）は組み立てない。

能力の変化と総合の出典は domain（`rating_change`・`overall_value`）、新人の経路の出典は `DraftRoute.of_profile`。
"""

from __future__ import annotations

from ..domain.pennant.draft import DraftRoute
from ..domain.pennant.initial_ratings import age_at_season_start
from ..domain.pennant.offseason import rating_change
from ..domain.pennant.season import is_season_closed
from ..domain.repositories import WorldRepository
from ..domain.value_objects import Season
from .dto import (
    LeagueOption,
    OffseasonChangeRow,
    OffseasonFacts,
    OffseasonPlayerFact,
    OffseasonRetiredRow,
    OffseasonRookieRow,
    OffseasonSummary,
    WorldContext,
)
from .pennant_ratings import overall_cell
from .queries import OffseasonQuery

# 成長と衰えの大きかった選手に載せる人数
TOP_CHANGES = 10


class OffseasonViewService:
    """オフの結果の材料を作る。範囲（世界）は `OffseasonQuery` が持つ。"""

    def __init__(self, *, worlds: WorldRepository, offseason: OffseasonQuery) -> None:
        self._worlds = worlds
        self._offseason = offseason

    def get_summary(self, world: WorldContext, year: int, *, league_id: int | None = None) -> OffseasonSummary | None:
        """Y 年のオフの結果。Y 年を締めていない（範囲外・まだ締めていない年も）なら None。

        `league_id` は引退・退団と新人の絞り込み。無効な値は、世界の既定のリーグ（無ければ先頭）に落とす。
        """
        if not Season.MIN_YEAR <= year < Season.MAX_YEAR or not is_season_closed(
            year=year,
            start_year=self._worlds.find_by_id(world.world_id).start_year,
            next_year_ratings_exist=self._offseason.has_ratings(year + 1),
        ):
            return None
        facts = self._offseason.facts(year)
        own_id = world.managed_team_id
        selected = self._selected_league(facts, league_id, world.default_league_id)

        retired = [self._retired_row(fact, year) for fact in facts.retired]
        rookies = [self._rookie_row(fact, year) for fact in facts.rookies]
        changes = sorted(
            (self._change_row(fact, year) for fact in facts.retained), key=lambda row: (-row.delta, row.player_id)
        )
        return OffseasonSummary(
            year=year,
            next_year=year + 1,
            own_team_id=own_id,
            own_team_name=world.managed_team_name,
            own_retired=tuple(row for row in retired if row.team_id == own_id),
            own_rookies=tuple(row for row in rookies if row.team_id == own_id),
            own_changes=tuple(row for row in changes if row.team_id == own_id),
            leagues=facts.leagues,
            selected_league_id=selected,
            retired=tuple(row for fact, row in zip(facts.retired, retired, strict=True) if fact.league_id == selected),
            rookies=tuple(row for fact, row in zip(facts.rookies, rookies, strict=True) if fact.league_id == selected),
            retired_total=len(retired),
            rookie_total=len(rookies),
            risers=tuple([row for row in changes if row.delta > 0][:TOP_CHANGES]),
            decliners=tuple(
                sorted((row for row in changes if row.delta < 0), key=lambda r: (r.delta, r.player_id))[:TOP_CHANGES]
            ),
        )

    @staticmethod
    def _selected_league(facts: OffseasonFacts, requested: int | None, default: int | None) -> int | None:
        known = {league.id for league in facts.leagues}
        if requested in known:
            return requested
        if default in known:
            return default
        return _first(facts.leagues)

    @staticmethod
    def _retired_row(fact: OffseasonPlayerFact, year: int) -> OffseasonRetiredRow:
        return OffseasonRetiredRow(
            player_id=fact.player_id,
            name=fact.name,
            team_id=fact.team_id,
            team_name=fact.team_name,
            number=fact.number,
            position=fact.position.label,
            age=age_at_season_start(fact.profile, year),
            leaving="退団" if fact.profile.is_foreign_player else "引退",
            rating=overall_cell(fact.before) if fact.before is not None else None,
        )

    @staticmethod
    def _rookie_row(fact: OffseasonPlayerFact, year: int) -> OffseasonRookieRow:
        return OffseasonRookieRow(
            player_id=fact.player_id,
            name=fact.name,
            team_id=fact.team_id,
            team_name=fact.team_name,
            number=fact.number,
            position=fact.position.label,
            age=age_at_season_start(fact.profile, year + 1),
            route=DraftRoute.of_profile(fact.profile).label,
            rating=overall_cell(fact.after) if fact.after is not None else None,
        )

    @staticmethod
    def _change_row(fact: OffseasonPlayerFact, year: int) -> OffseasonChangeRow:
        assert fact.before is not None and fact.after is not None, "残った選手は両方の年の能力を持つ"
        delta = rating_change(fact.before, fact.after)
        return OffseasonChangeRow(
            player_id=fact.player_id,
            name=fact.name,
            team_id=fact.team_id,
            team_name=fact.team_name,
            position=fact.position.label,
            age=age_at_season_start(fact.profile, year + 1),
            before=overall_cell(fact.before),
            after=overall_cell(fact.after),
            delta_label=f"{delta:+.1f}",
            delta=delta,
        )


def _first(leagues: tuple[LeagueOption, ...]) -> int | None:
    return leagues[0].id if leagues else None
