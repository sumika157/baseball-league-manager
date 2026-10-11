"""打席の結果と走者の走力から、走者と打者の進塁（`RunnerAdvance`）を組み立てる。

定数は `seed_virtual_games` で調整済みの値を出発点にしている。そのうえで走者の走力で
増減させる（走力が15点高いと、その事象の odds が exp(β) 倍になる）。

**走者は先の塁から順に動かし、`occupied` をその場で書き換える。** 一塁走者を先に動かすと、
二塁走者がまだ居るために行き先が塞がって見える。

**1試合平均得点はここでほぼ決まる。** 打撃の指標を合わせても得点が多いときは、
打撃ではなくここ（単打・二塁打での進塁、ゴロアウトでの進塁）を下げる。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from ..entities import RunnerAdvance
from ..value_objects import AdvanceReason, Base, InningsPitched, PlateAppearanceResult
from .odds import RATING_UNIT, from_odds, to_odds
from .randomness import GameRandom
from .ratings import AVERAGE_RATING

P = PlateAppearanceResult
R = AdvanceReason

OUTS_PER_INNING = InningsPitched.OUTS_PER_INNING

# 打者が球を打たない（走者が走りやすい）打席。盗塁はここでだけ試みる。
# 打球が飛ぶ打席に混ぜると、走塁のアウトが打球の処理と同じ打席に並んでしまう。
_NO_CONTACT = (P.WALK, P.INTENTIONAL_WALK, P.HIT_BY_PITCH, P.STRIKEOUT_LOOKING, P.STRIKEOUT_SWINGING)


@dataclass(frozen=True)
class BaserunningRules:
    # 盗塁。一塁走者が、二塁が空いていて2アウト未満のときに試みる
    steal_attempt_rate: float = 0.55
    steal_success_rate: float = 0.72
    # 単打・二塁打での走者の進み方（上限を決める確率）
    extra_base_on_single_from_first: float = 0.20
    score_on_single_from_second: float = 0.48
    score_on_double_from_first: float = 0.33
    # 併殺（一塁に走者がいて2アウト未満のゴロ）と、併殺でないゴロでの進塁
    double_play_rate: float = 0.40
    advance_on_ground_out: float = 0.20
    # 走力の効き（β）
    speed_steal_attempt: float = 0.60
    speed_steal_success: float = 0.40
    speed_extra_base: float = 0.35
    speed_double_play: float = -0.30  # 打者の走力が高いほど併殺になりにくい


DEFAULT_RULES = BaserunningRules()


def adjusted(probability: float, beta: float, speed: int) -> float:
    """走力に応じて確率を増減する（odds を exp(β × (走力 − 50) / 15) 倍する）。"""
    if probability <= 0.0 or probability >= 1.0:
        return probability
    return from_odds(to_odds(probability) * math.exp(beta * (speed - AVERAGE_RATING) / RATING_UNIT))


def build_advances(
    rng: GameRandom,
    rules: BaserunningRules,
    result: PlateAppearanceResult,
    batter_id: int,
    occupied: dict[Base, int],
    outs: int,
    speeds: Mapping[int, int],
) -> list[RunnerAdvance]:
    """打席の結果に応じて、走者と打者の進塁を組み立てる。`occupied` を書き換える。

    盗塁は打者が球を打たない打席で、打者の進塁より前に起きる。盗塁の結果は打者の進塁を
    組み立てる前に塁の状態へ反映する（押し出しの判定が変わるため）。
    """
    advances: list[RunnerAdvance] = []
    if result in _NO_CONTACT:
        _maybe_steal(rng, rules, advances, occupied, outs, speeds)

    if result is P.HOME_RUN:
        _send_home(advances, occupied)
        _batter_to(advances, batter_id, Base.HOME, R.BATTED_BALL, occupied)
    elif result.is_hit:
        _advance_on_hit(rng, rules, advances, result, occupied, speeds)
        _batter_to(advances, batter_id, result.default_batter_base, R.BATTED_BALL, occupied)
    elif result in (P.WALK, P.INTENTIONAL_WALK, P.HIT_BY_PITCH):
        _push_forced(advances, occupied)
        _batter_to(advances, batter_id, Base.FIRST, R.AWARDED_BASE, occupied)
    elif result is P.REACHED_ON_ERROR:
        _advance_all(advances, occupied, R.ERROR, error_index=0)
        _batter_to(advances, batter_id, Base.FIRST, R.ERROR, occupied, error_index=0)
    elif result is P.FIELDERS_CHOICE:
        _put_out(advances, occupied, Base.FIRST, R.FORCE_OUT)
        _batter_to(advances, batter_id, Base.FIRST, R.FIELDERS_CHOICE, occupied)
    elif result is P.SACRIFICE_BUNT:
        _advance_all(advances, occupied, R.BATTED_BALL)
        _batter_to(advances, batter_id, Base.OUT, R.PUT_OUT, occupied)
    elif result is P.SACRIFICE_FLY:
        runner = occupied.pop(Base.THIRD)
        advances.append(RunnerAdvance(runner, Base.THIRD, Base.HOME, R.TAG_UP))
        _batter_to(advances, batter_id, Base.OUT, R.PUT_OUT, occupied)
    elif result is P.GROUND_OUT:
        _ground_out(rng, rules, advances, batter_id, occupied, outs, speeds)
    else:
        # 三振・フライ・ライナー・邪飛。走者は動かない
        _batter_to(advances, batter_id, Base.OUT, R.PUT_OUT, occupied)
    return advances


def _maybe_steal(
    rng: GameRandom,
    rules: BaserunningRules,
    advances: list[RunnerAdvance],
    occupied: dict[Base, int],
    outs: int,
    speeds: Mapping[int, int],
) -> None:
    """一塁走者の盗塁。二塁が空いていて、2アウト未満のときだけ試みる。

    2アウトで試みないのは、盗塁刺と打者のアウトが重なると1つの半回で
    アウトが4つになるため（打者はもう打席を終えている）。
    """
    runner = occupied.get(Base.FIRST)
    if runner is None or Base.SECOND in occupied or outs >= OUTS_PER_INNING - 1:
        return
    speed = speeds.get(runner, AVERAGE_RATING)
    if rng.random() >= adjusted(rules.steal_attempt_rate, rules.speed_steal_attempt, speed):
        return

    if rng.random() < adjusted(rules.steal_success_rate, rules.speed_steal_success, speed):
        advances.append(RunnerAdvance(runner, Base.FIRST, Base.SECOND, R.STOLEN_BASE))
        occupied[Base.SECOND] = runner
    else:
        advances.append(RunnerAdvance(runner, Base.FIRST, Base.OUT, R.CAUGHT_STEALING))
    del occupied[Base.FIRST]


def _ground_out(
    rng: GameRandom,
    rules: BaserunningRules,
    advances: list[RunnerAdvance],
    batter_id: int,
    occupied: dict[Base, int],
    outs: int,
    speeds: Mapping[int, int],
) -> None:
    """ゴロアウト。一塁に走者がいれば併殺になることがある（打者が速いと併殺を避けやすい）。"""
    double_play = adjusted(rules.double_play_rate, rules.speed_double_play, speeds.get(batter_id, AVERAGE_RATING))
    if Base.FIRST in occupied and outs < OUTS_PER_INNING - 1 and rng.random() < double_play:
        _put_out(advances, occupied, Base.FIRST, R.FORCE_OUT)
    elif outs < OUTS_PER_INNING - 1 and rng.random() < rules.advance_on_ground_out:
        # 2アウトのゴロでは、打者がアウトになって回が終わるので走者は進まない（得点も入らない）
        _advance_all(advances, occupied, R.BATTED_BALL)
    _batter_to(advances, batter_id, Base.OUT, R.PUT_OUT, occupied)


def _advance_on_hit(
    rng: GameRandom,
    rules: BaserunningRules,
    advances: list[RunnerAdvance],
    result: PlateAppearanceResult,
    occupied: dict[Base, int],
    speeds: Mapping[int, int],
) -> None:
    """安打での走者の動き。先の塁の走者から順に決める。"""
    for base in reversed(Base.occupiable()):
        if base in occupied:
            speed = speeds.get(occupied[base], AVERAGE_RATING)
            wanted = _destination_on_hit(rng, rules, result, base, speed)
            _place(advances, base, wanted, occupied, R.BATTED_BALL)


def _destination_on_hit(
    rng: GameRandom, rules: BaserunningRules, result: PlateAppearanceResult, base: Base, speed: int
) -> Base:
    """その安打で走者がどこまで行くか（上限）。"""
    if result is P.TRIPLE:
        return Base.HOME
    if result is P.DOUBLE:
        if base is Base.FIRST:
            home = adjusted(rules.score_on_double_from_first, rules.speed_extra_base, speed)
            return Base.HOME if rng.random() < home else Base.THIRD
        return Base.HOME
    # 単打
    if base is Base.THIRD:
        return Base.HOME
    if base is Base.SECOND:
        home = adjusted(rules.score_on_single_from_second, rules.speed_extra_base, speed)
        return Base.HOME if rng.random() < home else Base.THIRD
    third = adjusted(rules.extra_base_on_single_from_first, rules.speed_extra_base, speed)
    return Base.THIRD if rng.random() < third else Base.SECOND


def _forced_bases(occupied: dict[Base, int]) -> list[Base]:
    """打者が一塁を与えられたときに、押し出される走者の塁。

    一塁から詰まっている連続した塁だけが押し出される（一塁と三塁なら三塁は動かない）。
    """
    forced: list[Base] = []
    for base in Base.occupiable():
        if base not in occupied:
            break
        forced.append(base)
    return forced


def _advance_all(
    advances: list[RunnerAdvance], occupied: dict[Base, int], reason: AdvanceReason, *, error_index: int | None = None
) -> None:
    """塁上の走者を1つずつ進める。詰まっていれば進めない走者も出る。"""
    for base in reversed(Base.occupiable()):
        if base in occupied:
            _place(advances, base, Base(base.value + 1), occupied, reason, error_index)


def _push_forced(advances: list[RunnerAdvance], occupied: dict[Base, int]) -> None:
    """打者が一塁を与えられたときの押し出し。詰まっている走者だけが進む。"""
    for base in reversed(_forced_bases(occupied)):
        runner = occupied.pop(base)
        target = Base(base.value + 1)
        advances.append(RunnerAdvance(runner, base, target, R.FORCED))
        if target.occupies_base:
            occupied[target] = runner


def _send_home(advances: list[RunnerAdvance], occupied: dict[Base, int]) -> None:
    advances.extend(
        RunnerAdvance(occupied.pop(base), base, Base.HOME, R.BATTED_BALL)
        for base in reversed(Base.occupiable())
        if base in occupied
    )


def _put_out(advances: list[RunnerAdvance], occupied: dict[Base, int], base: Base, reason: AdvanceReason) -> None:
    advances.append(RunnerAdvance(occupied.pop(base), base, Base.OUT, reason))


def _batter_to(
    advances: list[RunnerAdvance],
    batter_id: int,
    target: Base,
    reason: AdvanceReason,
    occupied: dict[Base, int],
    error_index: int | None = None,
) -> None:
    advances.append(RunnerAdvance(batter_id, Base.BATTER, target, reason, error_index=error_index))
    if target.occupies_base:
        occupied[target] = batter_id


def _place(
    advances: list[RunnerAdvance],
    base: Base,
    wanted: Base,
    occupied: dict[Base, int],
    reason: AdvanceReason,
    error_index: int | None = None,
) -> None:
    """走者を空いている塁まで進める。先を走る走者が止まっていれば手前で止まる。

    前を走る走者から順に呼ぶので、その走者が空けた塁はもう `occupied` に無い。
    隣の塁まで塞がっている走者は動かない（記録も残さない）。
    """
    target = wanted
    while target.occupies_base and target in occupied:
        if target.value <= base.value + 1:
            return  # これ以上手前には下がれない。進塁できない
        target = Base(target.value - 1)
    runner = occupied.pop(base)
    advances.append(RunnerAdvance(runner, base, target, reason, error_index=error_index))
    if target.occupies_base:
        occupied[target] = runner
