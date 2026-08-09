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
# 並べ替え
# ------------------------------------------------------------------

# 「何を基準に並べ替えられるか」は野球の指標そのものなので、
# 画面ではなくここに置く。既定の向き（True = 大きい順）も併せて持たせる。
# 打率や本塁打は多いほど良く、防御率や敗戦は少ないほど良い、という違いを
# 画面側で覚えずに済ませるため。
BATTER_SORT_KEYS = {
    'number': (lambda p: p.number.value, False),
    'name': (lambda p: p.name, False),
    'average': (lambda p: p.batting.batting_average, True),
    'hits': (lambda p: p.batting.hits, True),
    'doubles': (lambda p: p.batting.doubles, True),
    'triples': (lambda p: p.batting.triples, True),
    'home_runs': (lambda p: p.batting.home_runs, True),
    'rbi': (lambda p: p.batting.runs_batted_in, True),
    'obp': (lambda p: p.batting.on_base_percentage, True),
    'ops': (lambda p: p.batting.ops, True),
}

PITCHER_SORT_KEYS = {
    'number': (lambda p: p.number.value, False),
    'name': (lambda p: p.name, False),
    'innings': (lambda p: p.pitching.innings.outs, True),
    'era': (lambda p: p.pitching.earned_run_average, False),
    'wins': (lambda p: p.pitching.wins, True),
    'losses': (lambda p: p.pitching.losses, True),
    'strikeouts': (lambda p: p.pitching.strikeouts, True),
    'whip': (lambda p: p.pitching.whip, False),
    'k9': (lambda p: p.pitching.strikeouts_per_nine, True),
}

DEFAULT_BATTER_SORT = 'ops'
DEFAULT_PITCHER_SORT = 'era'


# 率で並べる指標。未登板だと 0 になり、実力と無関係に上位や下位へ寄るため、
# これらで並べるときは未登板を常に末尾へ回す。
_RATE_PITCHER_KEYS = {'era', 'whip', 'k9'}


def _resolve(keys, key, descending, default_key):
    """URL 由来のキーを検証する。不正なら既定に落とす（エラーにしない）。"""
    if key not in keys:
        key = default_key
    if descending is None:
        descending = keys[key][1]
    return key, bool(descending)


def _ordered(players, getter, descending):
    """指定のキーで並べ替え、同値は背番号の小さい順で安定させる。

    sorted(..., key=(指標, 背番号), reverse=True) と書くと同値のときの
    背番号まで逆順になってしまうため、背番号で並べてから指標で並べ直す。
    Python のソートは安定なので、この順序なら背番号は常に昇順に保たれる。
    """
    ordered = sorted(players, key=lambda p: p.number.value)
    ordered.sort(key=getter, reverse=descending)
    return ordered


def sort_batters(players: list[Player], key: str = None, descending: bool = None):
    """野手を並べ替える。key が未指定・不正なら OPS の高い順。

    戻り値は (並べ替え後, 実際に使ったキー, 向き)。画面側で見出しの表示を
    合わせるため、採用されたキーと向きも返す。
    """
    key, descending = _resolve(BATTER_SORT_KEYS, key, descending, DEFAULT_BATTER_SORT)
    return _ordered(players, BATTER_SORT_KEYS[key][0], descending), key, descending


def sort_pitchers(players: list[Player], key: str = None, descending: bool = None):
    """投手を並べ替える。key が未指定・不正なら防御率の低い順。"""
    key, descending = _resolve(PITCHER_SORT_KEYS, key, descending, DEFAULT_PITCHER_SORT)
    getter = PITCHER_SORT_KEYS[key][0]

    if key in _RATE_PITCHER_KEYS:
        pitched = [p for p in players if p.pitching.innings.outs > 0]
        unpitched = [p for p in players if p.pitching.innings.outs == 0]
        ordered = _ordered(pitched, getter, descending)
        ordered += sorted(unpitched, key=lambda p: p.number.value)
        return ordered, key, descending

    return _ordered(players, getter, descending), key, descending


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
