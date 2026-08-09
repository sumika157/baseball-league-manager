"""ドメインサービス。

単一のエンティティには属さないが、業務ルールであるものを置く。

ランキングは「誰を対象に含めるか（規定に達しているか）」「何を良しとするか」という
野球のルールそのものなので、表示の都合ではなくドメインの関心事として扱う。
ここに置くことで、画面を持たなくても順位づけを単体テストできる。
"""

from __future__ import annotations

from dataclasses import dataclass

from .entities import Player, Team
from .value_objects import Season, TeamRecord


@dataclass(frozen=True)
class RankedPlayer:
    """順位づけされた選手と、その順位の根拠となった値。"""

    rank: int
    player: Player
    value: float


def _rank(entries: list[tuple[Player, float]], limit: int | None) -> list[RankedPlayer]:
    """値の並び順が確定した一覧に順位を振る。同値は同順位とする。"""
    ranked: list[RankedPlayer] = []
    previous_value = None
    for index, (player, value) in enumerate(entries, start=1):
        if previous_value is not None and value == previous_value:
            rank = ranked[-1].rank  # 同率
        else:
            rank = index
        ranked.append(RankedPlayer(rank=rank, player=player, value=value))
        previous_value = value

    return ranked[:limit] if limit is not None else ranked


def qualified_batters(players: list[Player], *, minimum_at_bats: int = 1) -> list[Player]:
    """打撃成績の対象となる選手。

    打数が規定に満たない選手を除く。除かないと、1打数1安打の選手が
    打率10割で首位に立つ、あるいは打数0の選手が並ぶといった結果になる。
    """
    return [
        p for p in players
        if not p.is_pitcher and p.batting.at_bats >= minimum_at_bats
    ]


def qualified_pitchers(players: list[Player]) -> list[Player]:
    """投球成績の対象となる選手。未登板（投球回0）は除く。"""
    return [p for p in players if p.is_pitcher and p.pitching.innings.outs > 0]


def leaders_by_ops(
    players: list[Player], *, limit: int | None = 5, minimum_at_bats: int = 1
) -> list[RankedPlayer]:
    """OPS の高い順。"""
    entries = [(p, p.batting.ops) for p in qualified_batters(players, minimum_at_bats=minimum_at_bats)]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_batting_average(
    players: list[Player], *, limit: int | None = 5, minimum_at_bats: int = 1
) -> list[RankedPlayer]:
    """打率の高い順。"""
    entries = [
        (p, p.batting.batting_average)
        for p in qualified_batters(players, minimum_at_bats=minimum_at_bats)
    ]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_home_runs(
    players: list[Player], *, limit: int | None = 5
) -> list[RankedPlayer]:
    """本塁打の多い順。本数そのものが記録なので打数の規定は設けない。"""
    entries = [
        (p, float(p.batting.home_runs))
        for p in players
        if not p.is_pitcher and p.batting.home_runs > 0
    ]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_era(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """防御率の低い順。"""
    entries = [(p, p.pitching.earned_run_average) for p in qualified_pitchers(players)]
    entries.sort(key=lambda e: (e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_strikeouts(
    players: list[Player], *, limit: int | None = 5
) -> list[RankedPlayer]:
    """奪三振の多い順。"""
    entries = [
        (p, float(p.pitching.strikeouts))
        for p in players
        if p.is_pitcher and p.pitching.strikeouts > 0
    ]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


# ------------------------------------------------------------------
# 順位表
# ------------------------------------------------------------------


@dataclass(frozen=True)
class StandingRow:
    """順位表の1行。"""

    rank: int
    team_id: int
    team_name: str
    record: TeamRecord
    games_behind: float

    @property
    def is_leader(self) -> bool:
        return self.rank == 1


def standings(teams: list[Team], season: Season) -> list[StandingRow]:
    """指定シーズンの順位表を作る。

    順位は勝率の高い順で決まる。勝率が同じなら同順位として扱う。
    そのシーズンの成績が未登録のチームは順位表に載せない
    （0勝0敗として最下位に並べると、未登録なのか全敗なのか区別できなくなる）。
    """
    entries = [
        (team, team.season_record(season))
        for team in teams
    ]
    entries = [(team, entry.record) for team, entry in entries if entry is not None]
    entries.sort(key=lambda e: (-e[1].winning_percentage, -e[1].wins, e[0].name))

    if not entries:
        return []

    leader_record = entries[0][1]

    rows: list[StandingRow] = []
    previous_percentage = None
    for index, (team, record) in enumerate(entries, start=1):
        if previous_percentage is not None and record.winning_percentage == previous_percentage:
            rank = rows[-1].rank  # 同率
        else:
            rank = index
        rows.append(
            StandingRow(
                rank=rank,
                team_id=team.id,
                team_name=team.name,
                record=record,
                games_behind=record.games_behind(leader_record),
            )
        )
        previous_percentage = record.winning_percentage

    return rows


def recorded_seasons(teams: list[Team]) -> list[Season]:
    """成績が1件でも登録されているシーズンを新しい順に返す。"""
    years = {entry.season for team in teams for entry in team.seasons}
    return sorted(years, key=lambda s: s.year, reverse=True)
