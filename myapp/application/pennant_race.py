"""優勝争いの材料の組み立て。順位表の行と残り試合から、自軍の優勝の確定・マジックを導く。

規則は domain の `pennant_race`（途中の確定・マジック）と `season_champion`（終了時の同率の決着）が
唯一の出典で、ここは順位表（`StandingRow`）を読み替えるだけ。世界の一覧と GM ホームが同じ読み替えを使う。保存はしない。
終わったシーズンの年ごとの最終順位と優勝（`final_standings`。優勝の決め方は domain の `league_champion`）も、
世界の終了・歴代の記録・球団史が同じ関数を通る。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from ..domain.entities import Game, League, Team
from ..domain.pennant.postseason import FinalStanding, LeagueFinish
from ..domain.pennant.race import RaceTeam, final_order, final_ranks, league_champion, pennant_race, season_champion
from ..domain.pennant.schedule import Fixture
from ..domain.value_objects import TeamRecord
from .dto import LeagueStandings, Listing, OwnRace, StandingRow
from .queries import GameListQuery
from .standings import league_standings


class StandingsSource(Protocol):
    """順位表と球団の並びの読み取り。同率の決着と勝ち上がりが使う2つだけ（`LeagueViewService` が満たす）。

    具象のサービスに依存しないのは、使う読み取りをこの2つに絞って示すため（#219 で分けたサービスの境界を広げない）。
    """

    def get_league_standings(self, year: int) -> list[LeagueStandings]: ...

    def list_teams(self) -> Listing: ...


@dataclass(frozen=True)
class ChampionTiebreak:
    """終了時の同率の決着に要る材料。同率が出たときだけ呼ぶので、読むのを遅らせる（関数で持つ）。"""

    # (勝った球団, 負けた球団) の対戦ごとの結果（引分は含めない）
    results: Callable[[], Iterable[tuple[int, int]]]
    # 前年の順位（球団 -> 順位。開幕年は空）
    previous_rank: Callable[[], Mapping[int, int]]
    # 球団の並び順
    order: Callable[[], Sequence[int]]


def team_order(league_view: StandingsSource) -> list[int]:
    """球団の並び順（同率の決着の最後の基準）。"""
    return [team.id for team in league_view.list_teams().rows]


def tiebreak_for(year: int, *, games: GameListQuery, league_view: StandingsSource) -> ChampionTiebreak:
    """`year` 年の同率の決着の材料。試合・前年の順位・球団の並びは、使うときに読む。"""

    def results() -> list[tuple[int, int]]:
        decided = []
        for game in games.list_rows(year=year):
            if game.is_recorded and game.winner_team_id is not None:
                loser = game.away_team_id if game.winner_team_id == game.home_team_id else game.home_team_id
                decided.append((game.winner_team_id, loser))
        return decided

    def previous_rank() -> dict[int, int]:
        """前年の**最終順位**。前年に同率首位があれば、規定で決めた優勝を1位とする（ほかの同率首位は2位）。"""
        ranks: dict[int, int] = {}
        last_year = tiebreak_for(year - 1, games=games, league_view=league_view)
        for league in league_view.get_league_standings(year - 1):
            league_ranks = {row.team_id: row.rank for row in league.rows}
            # 同率首位が無ければ、前々年の順位や試合は読まない
            tied = sum(1 for rank in league_ranks.values() if rank == 1) > 1
            champion = (
                league_champion(league_ranks, last_year.results(), last_year.previous_rank(), last_year.order())
                if tied
                else None
            )
            ranks.update(final_ranks(league_ranks, champion))
        return ranks

    def order() -> list[int]:
        return team_order(league_view)

    return ChampionTiebreak(results=results, previous_rank=previous_rank, order=order)


class LeagueFinishReader:
    """その年の公式戦の最終順位（全順序。`league_finishes`）を読む。勝ち上がりの読み取りと、ドラフト会議の指名順
    （シーズンを締める処理）が共有する（同じ年の順位を2通りに作らない）。"""

    def __init__(self, *, league_view: StandingsSource, games: GameListQuery) -> None:
        self._league_view = league_view
        # 同率の決着（当該球団間の対戦勝率など）の材料。公式戦だけを読む
        self._games = games

    def finishes(self, year: int) -> list[LeagueFinish]:
        """`year` 年の公式戦のリーグごとの最終順位（ポストシーズンの結果は使わない）。"""
        standings = self._league_view.get_league_standings(year)
        return league_finishes(standings, tiebreak_for(year, games=self._games, league_view=self._league_view))


def own_race(
    standings: list[LeagueStandings],
    remaining: Mapping[int, int],
    team_id: int,
    tiebreak: ChampionTiebreak | None = None,
) -> OwnRace | None:
    """`team_id` の優勝争い。球団が順位表に無い・相手がいない・確定でもマジック点灯でもなければ None。

    `standings` は対象のシーズンの順位。**試合をまだしていない球団は順位表に載らないので、相手に含めない**
    （開幕直後でマジックが点灯する余地は無いので、結果は変わらない）。`remaining` は球団ごとの未消化の試合数
    （載っていない球団は 0）。優勝は同じリーグの中で争うので、自軍のリーグの行だけを使う。

    `tiebreak` を渡すと、リーグの全試合が終わって同率首位が並んだとき、規定（`season_champion`）で決まった
    優勝球団が自軍なら確定とする。途中の確定は安全側のまま（同率で終わる余地があれば確定にしない）。
    """
    for league in standings:
        if not any(row.team_id == team_id for row in league.rows):
            continue
        teams = [RaceTeam(row.team_id, row.wins, row.losses, remaining.get(row.team_id, 0)) for row in league.rows]
        race = pennant_race(team_id, teams)
        if race is None:
            return None
        if (
            not race.clinched
            and tiebreak is not None
            and _wins_the_tiebreak(league.rows, remaining, team_id, tiebreak)
        ):
            return OwnRace(clinched=True, magic=None)
        if race.clinched or race.magic is not None:
            return OwnRace(clinched=race.clinched, magic=race.magic)
        return None
    return None


def _wins_the_tiebreak(
    rows: list[StandingRow], remaining: Mapping[int, int], team_id: int, tiebreak: ChampionTiebreak
) -> bool:
    if any(remaining.get(row.team_id, 0) for row in rows):
        return False
    leaders = [row.team_id for row in rows if row.rank == 1]
    if len(leaders) < 2 or team_id not in leaders:
        return False
    return season_champion(leaders, tiebreak.results(), tiebreak.previous_rank(), tiebreak.order()) == team_id


def remaining_by_team(fixtures: Iterable[Fixture]) -> dict[int, int]:
    """球団ごとの未消化の試合数（ホームでもビジターでも1試合）。"""
    counts: Counter[int] = Counter()
    for fixture in fixtures:
        counts[fixture.home_team_id] += 1
        counts[fixture.visitor_team_id] += 1
    return dict(counts)


@dataclass(frozen=True)
class SeasonFinal:
    """1シーズンの最終結果。リーグ別の最終順位と、リーグごとの優勝球団。"""

    year: int
    standings: list[LeagueStandings]
    # リーグ id -> 優勝した球団 id（順位表の無いリーグは載らない）
    champions: Mapping[int, int]
    # リーグごとの全順序の最終順位（ポストシーズンの出場と種を決める。`league_finishes`）
    finishes: Sequence[LeagueFinish]
    # その年の最後の公式戦の日（ポストシーズンの日付の起点）。試合が無ければ None
    last_regular_date: date | None

    def league_of(self, team_id: int) -> LeagueStandings | None:
        """球団が載っているリーグの順位表。その年に試合の無い球団（順位表に載らない）は None。"""
        return next((league for league in self.standings if any(row.team_id == team_id for row in league.rows)), None)

    def result_of(self, team_id: int) -> tuple[StandingRow, bool] | None:
        """球団の最終順位の行と、優勝したか。その年に試合の無い球団は None。"""
        league = self.league_of(team_id)
        if league is None:
            return None
        row = next(row for row in league.rows if row.team_id == team_id)
        return row, self.champions.get(league.league_id) == team_id


def final_standings(
    years: Iterable[int],
    *,
    teams: Sequence[Team],
    leagues: Sequence[League],
    games_of: Callable[[int], list[Game]],
    order: Sequence[int],
) -> list[SeasonFinal]:
    """年ごとの最終順位と優勝。**世界の終了・歴代の記録・球団史が同じ関数を通る**。

    優勝の決め方は domain の `league_champion`。年は古い順に処理し、前年の最終順位
    （同率首位は規定で決めた優勝を1位にしたもの。domain の `final_ranks`）を次の年の同率の決着に渡す。
    前年が `years` に無ければ、前年の順位は無いものとする（開幕年と同じ）。
    同率の決着に使う対戦結果は、その年に読んだ試合から作る（クエリを足さない）。

    ポストシーズンの勝ち上がりの材料（全順序の最終順位 `finishes` と最後の公式戦の日）も、同じ材料から作る。
    全順序は勝ち上がりの読み取り（`PostseasonBracketReader`）と同じ関数 `league_finishes` を通す
    （同率の材料が食い違うと、導いた勝ち上がりが試合と合わず `InvalidPostseason` になる）。
    """
    seasons: list[SeasonFinal] = []
    previous_year: int | None = None
    previous_ranks: dict[int, int] = {}
    for year in sorted(set(years)):
        games = games_of(year)
        standings = league_standings(teams, leagues, games)
        results = [
            (game.winner_team_id, game.away_team_id if game.winner_team_id == game.home_team_id else game.home_team_id)
            for game in games
            if game.is_recorded and game.winner_team_id is not None
        ]
        before = previous_ranks if previous_year == year - 1 else {}
        champions: dict[int, int] = {}
        ranks: dict[int, int] = {}
        for league in standings:
            league_ranks = {row.team_id: row.rank for row in league.rows}
            champion = league_champion(league_ranks, results, before, order)
            if champion is not None:
                champions[league.league_id] = champion
            ranks.update(final_ranks(league_ranks, champion))
        tiebreak = ChampionTiebreak(
            results=_constant(results), previous_rank=_constant(before), order=_constant(order)
        )
        seasons.append(
            SeasonFinal(
                year=year,
                standings=standings,
                champions=champions,
                finishes=league_finishes(standings, tiebreak),
                last_regular_date=max((game.played_on for game in games), default=None),
            )
        )
        previous_year, previous_ranks = year, ranks
    return seasons


def league_finishes(standings: Sequence[LeagueStandings], tiebreak: ChampionTiebreak) -> list[LeagueFinish]:
    """リーグ別の順位表を、ポストシーズンの出場と種を決める**全順序**の最終順位にする。

    **全順序の唯一の出典**で、勝ち上がりの読み取り（`PostseasonBracketReader`。その年の順位表と `tiebreak_for`）と
    `final_standings`（歴代の記録・球団史・世界の終了。年ごとに読んだ試合と前年の最終順位）の両方が通る。
    同率のあるリーグだけ、優勝と同じ規定（domain の `final_order`。首位は `league_champion` と同じ）で並べる。
    同率の材料（対戦結果・前年の順位・球団の並び）は、要るときに1回だけ読む。
    """
    results = _once(lambda: list(tiebreak.results()))
    previous = _once(lambda: dict(tiebreak.previous_rank()))
    order = _once(lambda: list(tiebreak.order()))
    finishes = []
    for league in standings:
        ranks = {row.team_id: row.rank for row in league.rows}
        if len(set(ranks.values())) == len(ranks):
            ordered = sorted(ranks, key=ranks.__getitem__)
        else:
            ordered = final_order(ranks, results(), previous(), order())
        rows = {row.team_id: row for row in league.rows}
        finishes.append(
            LeagueFinish(
                league_id=league.league_id,
                standings=tuple(
                    FinalStanding(
                        team_id=team_id,
                        record=TeamRecord(
                            wins=rows[team_id].wins, losses=rows[team_id].losses, ties=rows[team_id].ties
                        ),
                    )
                    for team_id in ordered
                ),
            )
        )
    return finishes


def _constant[T](value: T) -> Callable[[], T]:
    """いつも同じ値を返す読み取り（既に手元にある同率の材料を `ChampionTiebreak` に渡す）。"""
    return lambda: value


def _once[T](read: Callable[[], T]) -> Callable[[], T]:
    """最初に呼ばれたときだけ読み、以後は同じ値を返す（同率のリーグが複数あっても読み直さない）。"""
    cache: list[T] = []

    def value() -> T:
        if not cache:
            cache.append(read())
        return cache[0]

    return value
