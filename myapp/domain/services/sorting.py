"""一覧の並べ替え。

「何を基準に並べ替えられるか」は野球の指標そのものなので、
画面ではなくここに置く。既定の向き（True = 大きい順）も併せて持たせる。
打率や本塁打は多いほど良く、防御率や敗戦は少ないほど良い、という違いを
画面側で覚えずに済ませるため。
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from ..entities import Player
from ..simulation.ratings import BatterRatings, PitcherRatings

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


# --- 能力の表（ペナントの球団の画面） ---
#
# 成績の表と同じ形（キー → (取り出す関数, 既定の向き)）。能力は大きいほど良いので大きい順が既定。
# 既定の並びは背番号順（成績の表の既定は OPS・防御率だが、能力の表は選手を探すのが主な使い方）。


@dataclass(frozen=True)
class RatedBatter:
    """能力の表の1行の野手。能力が無い選手（まだ能力を持たない）は ratings が None。"""

    player: Player
    ratings: BatterRatings | None


@dataclass(frozen=True)
class RatedPitcher:
    """能力の表の1行の投手。能力が無い選手は ratings が None。"""

    player: Player
    ratings: PitcherRatings | None


def _rating_getter(name: str) -> Callable[[Any], int]:
    return lambda rated: getattr(rated.ratings, name)


# 能力の項目名は `BatterRatings.LABELS` / `PitcherRatings.LABELS` が出典なので、そこから作る
BATTER_RATING_SORT_KEYS: dict[str, tuple[Callable[[RatedBatter], Any], bool]] = {
    "number": (lambda r: r.player.number.value, False),
    "name": (lambda r: r.player.name, False),
    **{name: (_rating_getter(name), True) for name in BatterRatings.LABELS},
    "average": (lambda r: r.player.batting.batting_average, True),
    "ops": (lambda r: r.player.batting.ops, True),
}

PITCHER_RATING_SORT_KEYS: dict[str, tuple[Callable[[RatedPitcher], Any], bool]] = {
    "number": (lambda r: r.player.number.value, False),
    "name": (lambda r: r.player.name, False),
    **{name: (_rating_getter(name), True) for name in PitcherRatings.LABELS},
    "era": (lambda r: r.player.pitching.earned_run_average, False),
    "innings": (lambda r: r.player.pitching.innings.outs, True),
}

DEFAULT_RATING_SORT = "number"


def _sort_rated(
    items: list[Any],
    keys: dict[str, tuple[Callable[[Any], Any], bool]],
    rating_names: Collection[str],
    key: str | None,
    descending: bool | None,
) -> tuple[list[Any], str, bool]:
    """能力の表を並べ替える。不正なキーは背番号順に落とす。

    能力で並べるとき、能力の無い選手は向きによらず末尾に回す（0 とみなして上位や下位に寄せない）。
    未登板の投手の防御率も同じ理由で末尾。同値は背番号の小さい順で安定させる。
    """
    key, descending = _resolve(keys, key, descending, DEFAULT_RATING_SORT)
    getter = keys[key][0]
    ordered = sorted(items, key=lambda item: item.player.number.value)

    def placeable(item: Any) -> bool:
        if key in rating_names:
            return item.ratings is not None
        if key in _RATE_PITCHER_KEYS:
            return item.player.pitching.innings.outs > 0
        return True

    placed = [item for item in ordered if placeable(item)]
    rest = [item for item in ordered if not placeable(item)]
    # reverse=True でも、同値の並び（背番号順）は崩れない
    placed.sort(key=getter, reverse=descending)
    return placed + rest, key, descending


def sort_rated_batters(
    items: list[RatedBatter], key: str | None = None, descending: bool | None = None
) -> tuple[list[RatedBatter], str, bool]:
    """野手の能力の表を並べ替える。key が未指定・不正なら背番号の小さい順。"""
    return _sort_rated(items, BATTER_RATING_SORT_KEYS, BatterRatings.LABELS, key, descending)


def sort_rated_pitchers(
    items: list[RatedPitcher], key: str | None = None, descending: bool | None = None
) -> tuple[list[RatedPitcher], str, bool]:
    """投手の能力の表を並べ替える。key が未指定・不正なら背番号の小さい順。"""
    return _sort_rated(items, PITCHER_RATING_SORT_KEYS, PitcherRatings.LABELS, key, descending)
