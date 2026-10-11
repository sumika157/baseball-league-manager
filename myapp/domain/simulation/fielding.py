"""打球の処理経路（`fielded_by`）と、失策を犯す守備者。

結果ごとの固定の経路ではなく、**守備位置の重みで散らす**。経路が刺殺・補殺・併殺参加の
出典になる（`scoring.fielding_credits`）ので、特定の位置に偏ると守備成績が不自然になる。

- 併殺は3人の経路（6-4-3 など）。経路の最後が刺殺、それより前が補殺。
- 三振は空のまま。**捕手の刺殺として導かれる**（規則 9.10。入力させずに導く）。
- 安打・四死球・本塁打・失策出塁は経路を付けない。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from ..entities import FieldingError
from ..value_objects import ErrorKind, FieldingPosition, PlateAppearanceResult
from .odds import RATING_UNIT
from .randomness import GameRandom, weighted_index
from .ratings import AVERAGE_RATING

FP = FieldingPosition
P = PlateAppearanceResult

Path = tuple[FieldingPosition, ...]

# (経路, 重み)。経路の長さは処理にかかわった人数。
_GROUND_OUT_FIRST: list[tuple[Path, float]] = [
    ((FP.SHORTSTOP, FP.FIRST_BASE), 0.27),
    ((FP.SECOND_BASE, FP.FIRST_BASE), 0.23),
    ((FP.THIRD_BASE, FP.FIRST_BASE), 0.20),
    ((FP.FIRST_BASE,), 0.12),  # 一塁手の単独処理（3U）
    ((FP.FIRST_BASE, FP.PITCHER), 0.04),  # 一塁手から投手がカバー（3-1）
    ((FP.PITCHER, FP.FIRST_BASE), 0.10),
    ((FP.CATCHER, FP.FIRST_BASE), 0.04),
]
_DOUBLE_PLAY: list[tuple[Path, float]] = [
    ((FP.SHORTSTOP, FP.SECOND_BASE, FP.FIRST_BASE), 0.30),  # 6-4-3
    ((FP.SECOND_BASE, FP.SHORTSTOP, FP.FIRST_BASE), 0.27),  # 4-6-3
    ((FP.THIRD_BASE, FP.SECOND_BASE, FP.FIRST_BASE), 0.20),  # 5-4-3
    ((FP.FIRST_BASE, FP.SHORTSTOP, FP.FIRST_BASE), 0.09),  # 3-6-3
    ((FP.PITCHER, FP.SHORTSTOP, FP.FIRST_BASE), 0.06),  # 1-6-3
    ((FP.THIRD_BASE, FP.SHORTSTOP, FP.FIRST_BASE), 0.04),
    ((FP.CATCHER, FP.SECOND_BASE, FP.FIRST_BASE), 0.04),  # 2-4-3
]
_FIELDERS_CHOICE: list[tuple[Path, float]] = [
    ((FP.SHORTSTOP, FP.SECOND_BASE), 0.25),
    ((FP.THIRD_BASE, FP.SECOND_BASE), 0.22),
    ((FP.SECOND_BASE, FP.SHORTSTOP), 0.20),
    ((FP.FIRST_BASE, FP.SHORTSTOP), 0.12),
    ((FP.PITCHER, FP.SHORTSTOP), 0.11),
    ((FP.CATCHER, FP.THIRD_BASE), 0.10),
]
_SACRIFICE_BUNT: list[tuple[Path, float]] = [
    ((FP.PITCHER, FP.FIRST_BASE), 0.32),
    ((FP.CATCHER, FP.FIRST_BASE), 0.28),
    ((FP.THIRD_BASE, FP.FIRST_BASE), 0.25),
    ((FP.FIRST_BASE,), 0.15),
]

# 打球が最初に向かう位置の重み（経路が1人のもの）
_FLY_OUT: list[tuple[FieldingPosition, float]] = [
    (FP.LEFT_FIELD, 0.24),
    (FP.CENTER_FIELD, 0.27),
    (FP.RIGHT_FIELD, 0.23),
    (FP.SHORTSTOP, 0.06),
    (FP.SECOND_BASE, 0.06),
    (FP.THIRD_BASE, 0.05),
    (FP.FIRST_BASE, 0.05),
    (FP.CATCHER, 0.02),
    (FP.PITCHER, 0.02),
]
_SACRIFICE_FLY: list[tuple[FieldingPosition, float]] = [
    (FP.LEFT_FIELD, 0.33),
    (FP.CENTER_FIELD, 0.34),
    (FP.RIGHT_FIELD, 0.33),
]
_LINE_OUT: list[tuple[FieldingPosition, float]] = [
    (FP.SHORTSTOP, 0.15),
    (FP.SECOND_BASE, 0.13),
    (FP.THIRD_BASE, 0.14),
    (FP.FIRST_BASE, 0.10),
    (FP.PITCHER, 0.05),
    (FP.LEFT_FIELD, 0.14),
    (FP.CENTER_FIELD, 0.15),
    (FP.RIGHT_FIELD, 0.14),
]
_FOUL_FLY_OUT: list[tuple[FieldingPosition, float]] = [
    (FP.CATCHER, 0.30),
    (FP.FIRST_BASE, 0.20),
    (FP.THIRD_BASE, 0.20),
    (FP.LEFT_FIELD, 0.10),
    (FP.RIGHT_FIELD, 0.10),
    (FP.SECOND_BASE, 0.05),
    (FP.SHORTSTOP, 0.05),
]

# 失策を犯す守備者の重み。守備力が低い守備者ほど選ばれやすい（`ERROR_SKILL_BETA`）
_ERROR_POSITIONS: dict[FieldingPosition, float] = {
    FP.SHORTSTOP: 0.22,
    FP.THIRD_BASE: 0.17,
    FP.SECOND_BASE: 0.12,
    FP.FIRST_BASE: 0.07,
    FP.PITCHER: 0.08,
    FP.CATCHER: 0.05,
    FP.LEFT_FIELD: 0.09,
    FP.CENTER_FIELD: 0.08,
    FP.RIGHT_FIELD: 0.09,
}
ERROR_SKILL_BETA = -0.6
_OUTFIELD = (FP.LEFT_FIELD, FP.CENTER_FIELD, FP.RIGHT_FIELD)
_INFIELD_ERROR_KINDS = [(ErrorKind.FIELDING, 0.44), (ErrorKind.THROWING, 0.56)]
_OUTFIELD_ERROR_KINDS = [(ErrorKind.FIELDING, 0.30), (ErrorKind.THROWING, 0.20), (ErrorKind.DROPPED_FLY, 0.50)]

# 守備位置ごとの重み。チームの守備力（被安打・失策率）を、位置の重みつきの平均で作る。
DEFENSE_WEIGHTS: dict[FieldingPosition, float] = {
    FP.CATCHER: 0.14,
    FP.FIRST_BASE: 0.06,
    FP.SECOND_BASE: 0.13,
    FP.THIRD_BASE: 0.12,
    FP.SHORTSTOP: 0.18,
    FP.LEFT_FIELD: 0.08,
    FP.CENTER_FIELD: 0.17,
    FP.RIGHT_FIELD: 0.12,
}


def _pick[T](rng: GameRandom, options: Sequence[tuple[T, float]]) -> T:
    return options[weighted_index(rng, [weight for _, weight in options])][0]


def fielded_path(rng: GameRandom, result: PlateAppearanceResult, *, double_play: bool = False) -> Path:
    """打球の処理経路。守備位置の重みで散らす。経路を持たない結果は空。"""
    if result is P.GROUND_OUT:
        return _pick(rng, _DOUBLE_PLAY if double_play else _GROUND_OUT_FIRST)
    if result is P.FIELDERS_CHOICE:
        return _pick(rng, _FIELDERS_CHOICE)
    if result is P.SACRIFICE_BUNT:
        return _pick(rng, _SACRIFICE_BUNT)
    if result is P.FLY_OUT:
        return (_pick(rng, _FLY_OUT),)
    if result is P.SACRIFICE_FLY:
        return (_pick(rng, _SACRIFICE_FLY),)
    if result is P.LINE_OUT:
        return (_pick(rng, _LINE_OUT),)
    if result is P.FOUL_FLY_OUT:
        return (_pick(rng, _FOUL_FLY_OUT),)
    return ()


def team_defense(fielders: Sequence[tuple[FieldingPosition, int]]) -> float:
    """守備位置と守備力の組から、チームの守備力（位置の重みつきの平均）を作る。

    守備に就いていない位置（投手・指名打者）は含めない。重みの合計で割るので、
    位置が欠けていても値は 1〜100 の範囲に収まる。
    """
    total = 0.0
    weight_sum = 0.0
    for position, rating in fielders:
        weight = DEFENSE_WEIGHTS.get(position)
        if weight is None:
            continue
        total += weight * rating
        weight_sum += weight
    return total / weight_sum if weight_sum else float(AVERAGE_RATING)


def draw_error(rng: GameRandom, fielders: Sequence[tuple[FieldingPosition, int, int]]) -> FieldingError:
    """失策を犯す守備者と種類を選ぶ。`fielders` は (守備位置, 選手 id, 守備力)。

    守備位置の重みに、守備力が低いほど大きくなる係数を掛けて選ぶ。
    """
    candidates = [item for item in fielders if item[0] in _ERROR_POSITIONS]
    weights = [
        _ERROR_POSITIONS[position] * math.exp(ERROR_SKILL_BETA * (rating - AVERAGE_RATING) / RATING_UNIT)
        for position, _, rating in candidates
    ]
    position, player_id, _ = candidates[weighted_index(rng, weights)]
    kinds = _OUTFIELD_ERROR_KINDS if position in _OUTFIELD else _INFIELD_ERROR_KINDS
    return FieldingError(player_id, position, _pick(rng, kinds))
