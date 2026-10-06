"""優勝争いの材料の組み立て。順位表の行と残り試合から、自軍の優勝の確定・マジックを導く。

規則は domain の `pennant_race`（途中の確定・マジック）と `season_champion`（終了時の同率の決着）が
唯一の出典で、ここは順位表（`StandingRow`）を読み替えるだけ。世界の一覧と GM ホームが同じ読み替えを使う。保存はしない。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from ..domain.pennant.race import RaceTeam, pennant_race, season_champion
from ..domain.pennant.schedule import Fixture
from .dto import LeagueStandings, OwnRace, StandingRow
from .queries import GameListQuery
from .services import TeamApplicationService


@dataclass(frozen=True)
class ChampionTiebreak:
    """終了時の同率の決着に要る材料。同率が出たときだけ呼ぶので、読むのを遅らせる（関数で持つ）。"""

    # (勝った球団, 負けた球団) の対戦ごとの結果（引分は含めない）
    results: Callable[[], Iterable[tuple[int, int]]]
    # 前年の順位（球団 -> 順位。開幕年は空）
    previous_rank: Callable[[], Mapping[int, int]]
    # 球団の並び順
    order: Callable[[], Sequence[int]]


def tiebreak_for(year: int, *, games: GameListQuery, teams: TeamApplicationService) -> ChampionTiebreak:
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
        last_year = tiebreak_for(year - 1, games=games, teams=teams)
        for league in teams.get_league_standings(year - 1):
            leaders = [row.team_id for row in league.rows if row.rank == 1]
            champion = None
            if len(leaders) > 1:
                champion = season_champion(leaders, last_year.results(), last_year.previous_rank(), last_year.order())
            for row in league.rows:
                is_runner_up = champion is not None and row.team_id in leaders and row.team_id != champion
                ranks[row.team_id] = 2 if is_runner_up else row.rank
        return ranks

    def order() -> list[int]:
        return [team.id for team in teams.list_teams().rows]

    return ChampionTiebreak(results=results, previous_rank=previous_rank, order=order)


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
