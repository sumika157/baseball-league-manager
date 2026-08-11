"""一覧の並べ替え。

「何を基準に並べ替えられるか」は野球の指標そのものなので、
画面ではなくここに置く。既定の向き（True = 大きい順）も併せて持たせる。
打率や本塁打は多いほど良く、防御率や敗戦は少ないほど良い、という違いを
画面側で覚えずに済ませるため。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..entities import Player

# 並べ替えキー → (選手から比較する値を取り出す関数, 既定の向き)。
# 取り出す値は指標ごとに数値・文字列が混ざるため Any にしてある。
SortKeys = dict[str, tuple[Callable[[Player], Any], bool]]

BATTER_SORT_KEYS: SortKeys = {
    "number": (lambda p: p.number.value, False),
    "name": (lambda p: p.name, False),
    "average": (lambda p: p.batting.batting_average, True),
    "hits": (lambda p: p.batting.hits, True),
    "doubles": (lambda p: p.batting.doubles, True),
    "triples": (lambda p: p.batting.triples, True),
    "home_runs": (lambda p: p.batting.home_runs, True),
    "rbi": (lambda p: p.batting.runs_batted_in, True),
    "walks": (lambda p: p.batting.walks, True),
    "sacrifice_flies": (lambda p: p.batting.sacrifice_flies, True),
    "obp": (lambda p: p.batting.on_base_percentage, True),
    "slg": (lambda p: p.batting.slugging_percentage, True),
    "ops": (lambda p: p.batting.ops, True),
    "iso": (lambda p: p.batting.isolated_power, True),
}

PITCHER_SORT_KEYS: SortKeys = {
    "number": (lambda p: p.number.value, False),
    "name": (lambda p: p.name, False),
    "innings": (lambda p: p.pitching.innings.outs, True),
    "era": (lambda p: p.pitching.earned_run_average, False),
    "wins": (lambda p: p.pitching.wins, True),
    "losses": (lambda p: p.pitching.losses, True),
    "saves": (lambda p: p.pitching.saves, True),
    "holds": (lambda p: p.pitching.holds, True),
    "hold_points": (lambda p: p.pitching.hold_points, True),
    "starts": (lambda p: p.pitching.starts, True),
    "strikeouts": (lambda p: p.pitching.strikeouts, True),
    "whip": (lambda p: p.pitching.whip, False),
    "k9": (lambda p: p.pitching.strikeouts_per_nine, True),
    "bb9": (lambda p: p.pitching.walks_per_nine, False),
    # FIP はリーグ共通の定数を足して仕上げる指標なので、素点で並べても
    # 順序は変わらない（全員に同じ値が足されるだけ）。定数を持たない
    # 並べ替えの場面でも、素点をそのまま比較に使える
    "fip": (lambda p: p.pitching.fip_base, False),
    "home_runs_allowed": (lambda p: p.pitching.home_runs_allowed, True),
    "hit_by_pitch_allowed": (lambda p: p.pitching.hit_by_pitch_allowed, True),
}

DEFAULT_BATTER_SORT = "ops"
DEFAULT_PITCHER_SORT = "era"


# 率で並べる指標。未登板だと 0 になり、実力と無関係に上位や下位へ寄るため、
# これらで並べるときは未登板を常に末尾へ回す。
_RATE_PITCHER_KEYS = {"era", "whip", "k9", "bb9", "fip"}


def _resolve(keys: SortKeys, key: str | None, descending: bool | None, default_key: str) -> tuple[str, bool]:
    """URL 由来のキーを検証する。不正なら既定に落とす（エラーにしない）。"""
    resolved = key if key is not None and key in keys else default_key
    if descending is None:
        descending = keys[resolved][1]
    return resolved, bool(descending)


def _ordered(players: list[Player], getter: Callable[[Player], Any], descending: bool) -> list[Player]:
    """指定のキーで並べ替え、同値は背番号の小さい順で安定させる。

    sorted(..., key=(指標, 背番号), reverse=True) と書くと同値のときの
    背番号まで逆順になってしまうため、背番号で並べてから指標で並べ直す。
    Python のソートは安定なので、この順序なら背番号は常に昇順に保たれる。
    """
    ordered = sorted(players, key=lambda p: p.number.value)
    ordered.sort(key=getter, reverse=descending)
    return ordered


def sort_batters(
    players: list[Player], key: str | None = None, descending: bool | None = None
) -> tuple[list[Player], str, bool]:
    """野手を並べ替える。key が未指定・不正なら OPS の高い順。

    戻り値は (並べ替え後, 実際に使ったキー, 向き)。画面側で見出しの表示を
    合わせるため、採用されたキーと向きも返す。
    """
    key, descending = _resolve(BATTER_SORT_KEYS, key, descending, DEFAULT_BATTER_SORT)
    return _ordered(players, BATTER_SORT_KEYS[key][0], descending), key, descending


def sort_pitchers(
    players: list[Player], key: str | None = None, descending: bool | None = None
) -> tuple[list[Player], str, bool]:
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
