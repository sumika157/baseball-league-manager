"""ペナントの世界の「Y年オフの結果」を読む。読むだけで、書き込みはしない。

締める処理（`PennantOffseasonService`）は結果を保存しない。引退・退団・戦力外の選手、新人、昇格した選手（#170）、
残った選手の能力の変化は、
在籍・プロフィール・能力の行から `OffseasonQuery` が導く。ここはその材料を画面向けの行にするだけで、
集約（`Team`）は組み立てない。ドラフトの盤（巡 × 球団）も新人の指名（`DraftPick`）と在籍から並べる（`draft_board`）。
例外はドラフトの抽選の経過で、締めると会議の記録が消えて導けないので、締める処理が残した記録（`DraftLotteryRecord`）を
読む（当たった球団は記録に持たず、新人の在籍の球団から。#298 D4）。

能力の変化と総合の出典は domain（`rating_change`・`overall_value`）、新人と獲得した外国人の経路の出典は
`foreign_market.newcomer_route_label`（#169）、抽選の表記は `draft_lottery.contest_label`。
"""

from __future__ import annotations

from collections.abc import Mapping

from ..domain.pennant.draft_lottery import DraftLotteryRecord
from ..domain.pennant.foreign_market import newcomer_route_label
from ..domain.pennant.initial_ratings import age_at_season_start
from ..domain.pennant.offseason import rating_change
from ..domain.pennant.season import is_season_closed
from ..domain.repositories import WorldRepository
from ..domain.value_objects import DepartureRoute, GameStage, Season
from .draft_board import BoardPick, draft_board
from .dto import (
    DraftBoard,
    DraftBoardCell,
    DraftBoardColumn,
    DraftLotteryView,
    LeagueOption,
    OffseasonChangeRow,
    OffseasonFacts,
    OffseasonPlayerFact,
    OffseasonPopularityRow,
    OffseasonPromotedRow,
    OffseasonRetiredRow,
    OffseasonRookieRow,
    OffseasonSummary,
    WorldContext,
)
from .pennant_popularity import PopularityReader, change_label
from .pennant_ratings import overall_cell
from .queries import AttendanceQuery, OffseasonQuery

# 成長と衰えの大きかった選手に載せる人数
TOP_CHANGES = 10


class OffseasonViewService:
    """オフの結果の材料を作る。範囲（世界）は `OffseasonQuery` が持つ。"""

    def __init__(
        self,
        *,
        worlds: WorldRepository,
        offseason: OffseasonQuery,
        popularities: PopularityReader,
        attendance: AttendanceQuery,
    ) -> None:
        self._worlds = worlds
        self._offseason = offseason
        # 人気の変化と観客動員の節（#208 E1）
        self._popularities = popularities
        self._attendance = attendance

    def get_summary(self, world: WorldContext, year: int, *, league_id: int | None = None) -> OffseasonSummary | None:
        """Y 年のオフの結果。Y 年を締めていない（範囲外・まだ締めていない年も）なら None。

        `league_id` は引退・退団・戦力外と新人の絞り込み。無効な値は、世界の既定のリーグ（無ければ先頭）に落とす。
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
        names = {team.id: team.name for team in facts.teams}
        # 抽選で当たった球団は、指名された新人の最初の在籍の球団（記録には持たない）
        drafted_by = {fact.player_id: fact.team_name for fact in facts.rookies}
        lotteries = {
            record.player_id: _lottery_view(record, names, winner=drafted_by.get(record.player_id, ""))
            for record in facts.lotteries
        }
        rookies = [self._rookie_row(fact, year, lotteries.get(fact.player_id)) for fact in facts.rookies]
        promoted = [self._promoted_row(fact, year) for fact in facts.promoted]
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
            own_promoted=tuple(row for row in promoted if row.team_id == own_id),
            own_changes=tuple(row for row in changes if row.team_id == own_id),
            leagues=facts.leagues,
            selected_league_id=selected,
            retired=tuple(row for fact, row in zip(facts.retired, retired, strict=True) if fact.league_id == selected),
            rookies=tuple(row for fact, row in zip(facts.rookies, rookies, strict=True) if fact.league_id == selected),
            promoted=tuple(
                row for fact, row in zip(facts.promoted, promoted, strict=True) if fact.league_id == selected
            ),
            retired_total=len(retired),
            released_total=sum(1 for fact in facts.retired if fact.left_via is DepartureRoute.RELEASED),
            rookie_total=len(rookies),
            promoted_total=len(promoted),
            risers=tuple([row for row in changes if row.delta > 0][:TOP_CHANGES]),
            decliners=tuple(
                sorted((row for row in changes if row.delta < 0), key=lambda r: (r.delta, r.player_id))[:TOP_CHANGES]
            ),
            board=_offseason_board(facts, year, selected, own_id, lotteries),
            popularity=self._popularity_rows(facts, year, selected, own_id),
        )

    def _popularity_rows(
        self, facts: OffseasonFacts, year: int, league_id: int | None, own_id: int | None
    ) -> tuple[OffseasonPopularityRow, ...]:
        """選んだリーグの球団の人気の変化（翌年の人気の高い順）と、締めた年の観客動員（#208 E1）。

        保存した行だけを読む（この機能より前に締めた年は翌年の行が無いので空）。
        """
        after = self._popularities.recorded(year + 1)
        if not after:
            return ()
        before = self._popularities.recorded(year)
        attendance = {
            row.team_id: row for row in self._attendance.home_totals(stage=GameStage.REGULAR, year=year, team_id=None)
        }
        rows = []
        for team in facts.teams:
            if team.league_id != league_id or team.id not in after:
                continue
            home = attendance.get(team.id)
            rows.append(
                OffseasonPopularityRow(
                    team_id=team.id,
                    team_name=team.name,
                    is_own=team.id == own_id,
                    before=before[team.id].value if team.id in before else None,
                    after=after[team.id].value,
                    change_label=change_label(before.get(team.id), after[team.id]),
                    attendance_total=0 if home is None else home.total,
                    attendance_average=0 if home is None else home.average,
                )
            )
        return tuple(sorted(rows, key=lambda row: (-row.after, row.team_id)))

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
            leaving=_leaving_label(fact),
            rating=overall_cell(fact.before) if fact.before is not None else None,
            is_development=fact.was_development,
        )

    @staticmethod
    def _promoted_row(fact: OffseasonPlayerFact, year: int) -> OffseasonPromotedRow:
        return OffseasonPromotedRow(
            player_id=fact.player_id,
            name=fact.name,
            team_id=fact.team_id,
            team_name=fact.team_name,
            number=str(fact.number),
            number_before=fact.number_before_promotion,
            position=fact.position.label,
            age=age_at_season_start(fact.profile, year + 1),
            rating=overall_cell(fact.after) if fact.after is not None else None,
        )

    @staticmethod
    def _rookie_row(fact: OffseasonPlayerFact, year: int, lottery: DraftLotteryView | None) -> OffseasonRookieRow:
        return OffseasonRookieRow(
            player_id=fact.player_id,
            name=fact.name,
            team_id=fact.team_id,
            team_name=fact.team_name,
            number=fact.number,
            position=fact.position.label,
            age=age_at_season_start(fact.profile, year + 1),
            route=newcomer_route_label(fact.profile),
            rating=overall_cell(fact.after) if fact.after is not None else None,
            pick="" if fact.profile.draft is None else fact.profile.draft.order_label,
            lottery=lottery,
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


def _lottery_view(record: DraftLotteryRecord, names: Mapping[int, str], *, winner: str) -> DraftLotteryView:
    return DraftLotteryView(
        bidders=tuple(names.get(team_id, "") for team_id in record.bidder_ids),
        winner=winner,
        bidder_ids=record.bidder_ids,
    )


def _offseason_board(
    facts: OffseasonFacts,
    year: int,
    league_id: int | None,
    own_id: int | None,
    lotteries: Mapping[int, DraftLotteryView],
) -> DraftBoard | None:
    """選んだリーグのドラフトの盤（巡 × 球団。#298 D4）。

    セルはその年の指名で入団した新人（順位の無い新人は置かない）。"""
    columns = [
        DraftBoardColumn(team_id=team.id, team_name=team.name, is_mine=team.id == own_id)
        for team in facts.teams
        if team.league_id == league_id
    ]
    picks = [
        BoardPick(
            round_number=pick.round,
            is_development=pick.is_development,
            cell=DraftBoardCell(
                team_id=fact.team_id,
                name=fact.name,
                position=fact.position.label,
                player_id=fact.player_id,
                lottery=lotteries.get(fact.player_id),
            ),
        )
        for fact in facts.rookies
        if (pick := fact.profile.draft) is not None and pick.round is not None and pick.year == year
    ]
    return draft_board(columns, picks)


def _first(leagues: tuple[LeagueOption, ...]) -> int | None:
    return leagues[0].id if leagues else None


def _leaving_label(fact: OffseasonPlayerFact) -> str:
    """オフの結果の区分の表記。戦力外は「戦力外」。ほか（引退・経路が不明な旧データ）は、外国人が「退団」、日本人が「引退」。"""
    if fact.left_via is DepartureRoute.RELEASED:
        return DepartureRoute.RELEASED.label
    return "退団" if fact.profile.is_foreign_player else DepartureRoute.RETIRED.label
