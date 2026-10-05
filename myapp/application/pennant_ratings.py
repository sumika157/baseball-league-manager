"""ペナントの世界の選手の能力を、画面に見せる形にする。

読むだけで、書き込みはしない。成績の表（`TeamApplicationService.list_batters` など）とは別の小さなサービスにして、
あの大きなサービスに能力の知識を足さない。

能力は選手 × 年で1組。ここでは**能力を引く年**（domain の `ratings_year`。編成とシミュレーションと同じ規則）の
能力だけを見せる（P6 で翌年の行ができても、試合で使われる能力と食い違わない）。
区分と目立たせ方は domain の `RatingGrade` が出典で、ここでは写すだけ。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date

from ..domain import services as domain_services
from ..domain.entities import Player
from ..domain.pennant.ratings import PlayerRatings
from ..domain.pennant.season import ratings_year
from ..domain.repositories import FixtureRepository, RatingsRepository, TeamRepository, WorldRepository
from ..domain.simulation.ratings import BatterRatings, PitcherRatings, RatingGrade
from .dto import PlayerRatingsCard, RatingCell, RatingColumn, RatingsRow, RatingsTable
from .queries import SimulationContextQuery


def _cells_of(ratings: BatterRatings | PitcherRatings) -> tuple[RatingCell, ...]:
    """能力の項目を、`LABELS` の並び（画面に出す順）で区分つきの表示にする。"""
    cells = []
    for key, label in ratings.LABELS.items():
        value: int = getattr(ratings, key)
        grade = RatingGrade.from_value(value)
        cells.append(RatingCell(key=key, label=label, value=value, grade=grade.label, emphasis=grade.emphasis))
    return tuple(cells)


class PennantRatingsViewService:
    """選手ページの能力のカードと、球団の能力の表を作る。`today` は年齢の基準日（世界の「今日」）。

    見せる能力の年は、試合・編成と同じ規則（domain の `ratings_year`）で決める。
    翌年の能力ができても（P6）、画面に出る能力と試合で使われる能力が食い違わない。
    """

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        fixtures: FixtureRepository,
        context_query: SimulationContextQuery,
        teams: TeamRepository,
        ratings: RatingsRepository,
        today: Callable[[], date],
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._fixtures = fixtures
        self._context_query = context_query
        self._teams = teams
        self._ratings = ratings
        self._today = today

    def current_year(self) -> int:
        """能力を見せる年（次に試合をする年。日を進める処理・編成と同じ規則）。"""
        return ratings_year(
            next_game_on=self._fixtures.first_date(),
            last_played_on=self._context_query.last_played_on(),
            start_year=self._worlds.find_by_id(self._world_id).start_year,
        )

    def get_card(self, player_id: int) -> PlayerRatingsCard | None:
        """選手のいまの年の能力のカード。その年の能力が無い選手や、範囲の外の選手は None。"""
        found = self._ratings.find_by_players([player_id], self.current_year())
        if not found:
            return None
        item = found[0]
        return PlayerRatingsCard(year=item.year, is_pitcher=item.is_pitcher, cells=_cells_of(item.ratings))

    def get_table(
        self,
        team_id: int,
        *,
        pitchers: bool,
        sort: str | None = None,
        descending: bool | None = None,
    ) -> RatingsTable:
        """球団の1軍の野手（または投手）の能力の表。無ければ TeamNotFound。

        能力は球団の選手ぶんを**1回のクエリで**読む（選手ごとに引かない）。その年の能力が無い選手は「—」。
        並べ替えのキーと既定の向きは domain（不正なキーは背番号順に落ちる）。
        """
        team = self._teams.find_by_id(team_id)
        members = [p for p in team.active_players if p.is_pitcher == pitchers]
        year = self.current_year()
        by_player = {
            item.player_id: item
            for item in self._ratings.find_by_players([p.id for p in members if p.id is not None], year)
        }
        today = self._today()
        if pitchers:
            return self._pitcher_table(members, by_player, today, year, sort, descending)
        return self._batter_table(members, by_player, today, year, sort, descending)

    def _batter_table(
        self,
        members: list[Player],
        by_player: Mapping[int, PlayerRatings],
        today: date,
        year: int,
        sort: str | None,
        descending: bool | None,
    ) -> RatingsTable:
        rated = [domain_services.RatedBatter(p, _batter_ratings(p, by_player)) for p in members]
        ordered, key, desc = domain_services.sort_rated_batters(rated, sort, descending)
        rows = tuple(
            _row(
                item.player,
                today,
                item.ratings,
                batting_average=item.player.batting.batting_average,
                ops=item.player.batting.ops,
            )
            for item in ordered
        )
        return _table(False, BatterRatings.LABELS, rows, year, key, desc)

    def _pitcher_table(
        self,
        members: list[Player],
        by_player: Mapping[int, PlayerRatings],
        today: date,
        year: int,
        sort: str | None,
        descending: bool | None,
    ) -> RatingsTable:
        rated = [domain_services.RatedPitcher(p, _pitcher_ratings(p, by_player)) for p in members]
        ordered, key, desc = domain_services.sort_rated_pitchers(rated, sort, descending)
        rows = tuple(
            _row(
                item.player,
                today,
                item.ratings,
                earned_run_average=item.player.pitching.earned_run_average,
                innings_pitched=str(item.player.pitching.innings),
            )
            for item in ordered
        )
        return _table(True, PitcherRatings.LABELS, rows, year, key, desc)


def _batter_ratings(player: Player, by_player: Mapping[int, PlayerRatings]) -> BatterRatings | None:
    """選手のその年の野手の能力。無い、または種類が合わない行は無いものとして扱う。"""
    item = by_player.get(player.id) if player.id is not None else None
    return item.ratings if item is not None and isinstance(item.ratings, BatterRatings) else None


def _pitcher_ratings(player: Player, by_player: Mapping[int, PlayerRatings]) -> PitcherRatings | None:
    item = by_player.get(player.id) if player.id is not None else None
    return item.ratings if item is not None and isinstance(item.ratings, PitcherRatings) else None


def _row(
    player: Player,
    today: date,
    ratings: BatterRatings | PitcherRatings | None,
    *,
    batting_average: float = 0.0,
    ops: float = 0.0,
    earned_run_average: float = 0.0,
    innings_pitched: str = "",
) -> RatingsRow:
    assert player.id is not None, "一覧に載る選手は保存済み"
    return RatingsRow(
        id=player.id,
        number=player.number.value,
        name=player.name,
        position=player.position.label,
        is_foreign_player=player.profile.is_foreign_player,
        age=player.profile.age(today),
        cells=_cells_of(ratings) if ratings is not None else (),
        batting_average=batting_average,
        ops=ops,
        earned_run_average=earned_run_average,
        innings_pitched=innings_pitched,
    )


def _table(
    is_pitcher: bool,
    labels: Mapping[str, str],
    rows: tuple[RatingsRow, ...],
    year: int,
    key: str,
    descending: bool,
) -> RatingsTable:
    return RatingsTable(
        is_pitcher=is_pitcher,
        year=year,
        columns=tuple(RatingColumn(key=name, label=label) for name, label in labels.items()),
        rows=rows,
        sort=key,
        descending=descending,
    )
