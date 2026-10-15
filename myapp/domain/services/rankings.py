"""規定とタイトルランキング。

「誰を対象に含めるか（規定に達しているか）」「何を良しとするか」という
野球のルールそのものなので、表示の都合ではなくドメインの関心事として扱う。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from ..entities import Player


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
        # 同値なら前の選手と同順位（1・1・3 のように次の順位は飛ぶ）
        rank = ranked[-1].rank if previous_value is not None and value == previous_value else index
        ranked.append(RankedPlayer(rank=rank, player=player, value=value))
        previous_value = value

    return ranked[:limit] if limit is not None else ranked


# 規定打席・規定投球回。日本プロ野球の規則にならう。
#   打者: チームの試合数 × 3.1 打席
#   投手: チームの試合数 × 1.0 投球回
QUALIFYING_PLATE_APPEARANCES_PER_GAME = 3.1
QUALIFYING_INNINGS_PER_GAME = 1.0

# 最高勝率の対象になる勝利数。NPB の規則（13勝以上）にならう。
# 規定投球回ではなく勝利数で絞るので、救援投手でも13勝すれば対象になる。
WINNING_PERCENTAGE_MINIMUM_WINS = 13


class Title(StrEnum):
    """タイトルの部門。NPB のタイトルのうち、成績だけで決まる部門（投票や選考で決まる賞は持たない）。

    **部門の並び・名前の唯一の出典**。タイトルの画面の部門・歴代の記録の列・ペナントの表彰（保存する部門の値）・
    ORM の選択肢がここを読む。値は保存する文字列なので変えない。並びは画面の並び（打撃 → 投手）。
    """

    AVERAGE = "average"
    HOME_RUNS = "home_runs"
    RUNS_BATTED_IN = "rbi"
    HITS = "hits"
    STOLEN_BASES = "stolen_bases"
    ON_BASE_PERCENTAGE = "obp"
    EARNED_RUN_AVERAGE = "era"
    WINS = "wins"
    WINNING_PERCENTAGE = "winning_percentage"
    STRIKEOUTS = "strikeouts"
    SAVES = "saves"
    HOLD_POINTS = "hold_points"

    @property
    def label(self) -> str:
        return _TITLE_LABELS[self]

    @property
    def note(self) -> str:
        """対象の条件（「規定打席以上」など）。率の部門と最高勝率だけ。"""
        return _TITLE_NOTES.get(self, "")

    @property
    def order(self) -> int:
        """定義の順番（画面の並び）。"""
        return list(Title).index(self)

    @property
    def is_pitching(self) -> bool:
        """投手の部門か（画面で打撃と投手を分けて並べる）。"""
        return self in _PITCHING_TITLES


_TITLE_LABELS = {
    Title.AVERAGE: "首位打者",
    Title.HOME_RUNS: "本塁打王",
    Title.RUNS_BATTED_IN: "打点王",
    Title.HITS: "最多安打",
    Title.STOLEN_BASES: "盗塁王",
    Title.ON_BASE_PERCENTAGE: "最高出塁率",
    Title.EARNED_RUN_AVERAGE: "最優秀防御率",
    Title.WINS: "最多勝利",
    Title.WINNING_PERCENTAGE: "最高勝率",
    Title.STRIKEOUTS: "最多奪三振",
    Title.SAVES: "最多セーブ",
    Title.HOLD_POINTS: "最優秀中継ぎ投手",
}
_TITLE_NOTES = {
    Title.AVERAGE: "規定打席以上",
    Title.ON_BASE_PERCENTAGE: "規定打席以上",
    Title.EARNED_RUN_AVERAGE: "規定投球回以上",
    Title.WINNING_PERCENTAGE: f"{WINNING_PERCENTAGE_MINIMUM_WINS}勝以上",
}
_PITCHING_TITLES = frozenset(
    {
        Title.EARNED_RUN_AVERAGE,
        Title.WINS,
        Title.WINNING_PERCENTAGE,
        Title.STRIKEOUTS,
        Title.SAVES,
        Title.HOLD_POINTS,
    }
)


def required_plate_appearances(team_games: int) -> int:
    """規定打席。端数は切り上げる。"""
    return math.ceil(team_games * QUALIFYING_PLATE_APPEARANCES_PER_GAME)


def required_outs(team_games: int) -> int:
    """規定投球回をアウト数で表したもの。"""
    return math.ceil(team_games * QUALIFYING_INNINGS_PER_GAME * 3)


def qualified_batters(
    players: list[Player],
    *,
    team_games: dict[int, int] | None = None,
    minimum_at_bats: int = 1,
) -> list[Player]:
    """打撃タイトルの対象となる選手。

    team_games（選手id → その選手のチームの試合数）を渡すと規定打席で絞る。
    渡さない場合は最低打数だけで絞る。規定で絞らないと、1打数1安打の選手が
    打率10割で首位に立ってしまう。
    """
    batters = [p for p in players if not p.is_pitcher]

    if team_games is None:
        return [p for p in batters if p.batting.at_bats >= minimum_at_bats]

    return [
        p
        for p in batters
        if p.batting.plate_appearances >= required_plate_appearances(team_games.get(p.id or 0, 0))
        and p.batting.at_bats > 0
    ]


def qualified_pitchers(players: list[Player], *, team_games: dict[int, int] | None = None) -> list[Player]:
    """投球タイトルの対象となる投手。

    team_games を渡すと規定投球回で絞る。渡さない場合は未登板だけを除く。
    """
    pitchers = [p for p in players if p.is_pitcher and p.pitching.innings.outs > 0]

    if team_games is None:
        return pitchers

    return [p for p in pitchers if p.pitching.innings.outs >= required_outs(team_games.get(p.id or 0, 0))]


def leaders_by_ops(
    players: list[Player],
    *,
    limit: int | None = 5,
    team_games: dict[int, int] | None = None,
    minimum_at_bats: int = 1,
) -> list[RankedPlayer]:
    """OPS の高い順。規定打席に達した選手のみ。"""
    entries = [
        (p, p.batting.ops) for p in qualified_batters(players, team_games=team_games, minimum_at_bats=minimum_at_bats)
    ]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_batting_average(
    players: list[Player],
    *,
    limit: int | None = 5,
    team_games: dict[int, int] | None = None,
    minimum_at_bats: int = 1,
) -> list[RankedPlayer]:
    """打率の高い順。規定打席に達した選手のみ。"""
    entries = [
        (p, p.batting.batting_average)
        for p in qualified_batters(players, team_games=team_games, minimum_at_bats=minimum_at_bats)
    ]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_on_base_percentage(
    players: list[Player],
    *,
    limit: int | None = 5,
    team_games: dict[int, int] | None = None,
    minimum_at_bats: int = 1,
) -> list[RankedPlayer]:
    """出塁率の高い順。首位打者と同じく規定打席に達した選手のみ。"""
    entries = [
        (p, p.batting.on_base_percentage)
        for p in qualified_batters(players, team_games=team_games, minimum_at_bats=minimum_at_bats)
    ]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_hits(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """安打の多い順。本数そのものが記録なので規定を設けない。"""
    entries = [(p, float(p.batting.hits)) for p in players if not p.is_pitcher and p.batting.hits > 0]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_stolen_bases(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """盗塁の多い順。本数そのものが記録なので規定を設けない。"""
    entries = [(p, float(p.batting.stolen_bases)) for p in players if not p.is_pitcher and p.batting.stolen_bases > 0]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_home_runs(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """本塁打の多い順。本数そのものが記録なので打数の規定は設けない。"""
    entries = [(p, float(p.batting.home_runs)) for p in players if not p.is_pitcher and p.batting.home_runs > 0]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_runs_batted_in(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """打点の多い順。本塁打と同じく本数そのものが記録なので規定を設けない。"""
    entries = [
        (p, float(p.batting.runs_batted_in)) for p in players if not p.is_pitcher and p.batting.runs_batted_in > 0
    ]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_era(
    players: list[Player],
    *,
    limit: int | None = 5,
    team_games: dict[int, int] | None = None,
) -> list[RankedPlayer]:
    """防御率の低い順。規定投球回に達した投手のみ。"""
    entries = [(p, p.pitching.earned_run_average) for p in qualified_pitchers(players, team_games=team_games)]
    entries.sort(key=lambda e: (e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_strikeouts(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """奪三振の多い順。"""
    entries = [(p, float(p.pitching.strikeouts)) for p in players if p.is_pitcher and p.pitching.strikeouts > 0]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_wins(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """勝利の多い順。数そのものが記録なので投球回の規定は設けない。"""
    entries = [(p, float(p.pitching.wins)) for p in players if p.is_pitcher and p.pitching.wins > 0]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_saves(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """セーブの多い順。勝利と同じく数そのものが記録なので規定を設けない。"""
    entries = [(p, float(p.pitching.saves)) for p in players if p.is_pitcher and p.pitching.saves > 0]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_hold_points(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """HP（ホールド＋救援勝利）の多い順。最優秀中継ぎ投手の基準。セーブと同じく規定を設けない。"""
    entries = [(p, float(p.pitching.hold_points)) for p in players if p.is_pitcher and p.pitching.hold_points > 0]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_winning_percentage(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """勝率の高い順。13勝以上の投手のみ（WINNING_PERCENTAGE_MINIMUM_WINS）。

    勝率は1勝0敗でも10割になるため、率の部門として対象を絞る。NPB の最高勝率は
    規定投球回ではなく勝利数で絞るので、qualified_pitchers は使わない。
    """
    entries = [
        (p, p.pitching.winning_percentage)
        for p in players
        if p.is_pitcher and p.pitching.wins >= WINNING_PERCENTAGE_MINIMUM_WINS
    ]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)
