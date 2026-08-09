"""ドメインサービス。

単一のエンティティには属さないが、業務ルールであるものを置く。

ランキングは「誰を対象に含めるか（規定に達しているか）」「何を良しとするか」という
野球のルールそのものなので、表示の都合ではなくドメインの関心事として扱う。
ここに置くことで、画面を持たなくても順位づけを単体テストできる。
"""

from __future__ import annotations

from dataclasses import dataclass

from .entities import Player


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
