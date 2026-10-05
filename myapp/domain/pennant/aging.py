"""年齢による能力の成長と衰え。**純粋な関数**で、DB にも Django にも触れない。

締める年 Y の4月1日時点の満年齢で、Y→Y+1 の1年ぶんの変化を決める。変化は

    期待値（年齢曲線 × 成長型 × 項目の差）+ 選手ごとの共通のぶれ + 項目ごとのぶれ

を四捨五入して 1〜100 に収めたもの。期待値の表と倍率が**規則の出典**で、設計書 3.6 と
12.「P6a の詳細」に最終値を記録する。曲線だけでは分布を毎年は保てないので、
翌年の能力を作る側（`offseason.py`）が最後に水準の錨（`simulation/spread.py`）をかける。
乱数は `normal()`（`random()` だけを使う）。成長型は変えない（翌年の行に写す）。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from ..simulation.randomness import GameRandom, normal
from ..simulation.ratings import RATING_MAX, RATING_MIN, BatterRatings, GrowthType, PitcherRatings

# 年齢曲線（普通型の、1年あたりの変化の期待値。単位は能力の点）。
# (その年齢から, 変化) の昇順。最初の行は「〜19歳」を兼ねる
AGE_CURVE: tuple[tuple[int, float], ...] = (
    (0, 3.0),
    (20, 2.5),
    (22, 2.0),
    (24, 1.1),
    (26, 0.2),
    (28, -0.3),
    (30, -1.1),
    (32, -2.0),
    (34, -3.0),
    (36, -4.0),
    (38, -5.0),
)
_CURVE_AGES = [age for age, _ in AGE_CURVE]

# 成長型: 曲線を何歳ぶんずらして読むか、伸び（正の変化）を何倍にするか
GROWTH_SHIFT = {GrowthType.EARLY: 2, GrowthType.NORMAL: 0, GrowthType.LATE: -2}
GROWTH_GAIN = {GrowthType.EARLY: 1.25, GrowthType.NORMAL: 1.0, GrowthType.LATE: 0.8}

# 75 以上の項目は伸びにくい（伸びを何倍にするか）
HIGH_RATING = 75
HIGH_RATING_GAIN = 0.5

# ぶれの標準偏差（点）。選手ごとの共通のぶれは全項目に同じ向きで、項目ごとのぶれは別々に効く
PLAYER_SD = 1.2
ITEM_SD = 1.5


@dataclass(frozen=True)
class ItemCurve:
    """項目ごとの差。曲線を `b(a − shift)` で読み、伸びを `gain` 倍、衰えを `decline` 倍する。"""

    shift: int
    gain: float
    decline: float


# 項目ごとの差。ミートと一発回避が基準
BATTER_CURVES = {
    "contact": ItemCurve(0, 0.95, 1.0),
    "power": ItemCurve(1, 0.8, 1.1),  # 頂点がやや遅い
    "eye": ItemCurve(3, 0.5, 0.6),  # 遅くまで伸び、衰えはゆるい
    "speed": ItemCurve(-2, 0.9, 1.1),  # 最も早く衰える
    "fielding": ItemCurve(-1, 1.0, 1.1),  # 走力の次に早い
}
PITCHER_CURVES = {
    "stuff": ItemCurve(-1, 1.0, 1.1),  # 球速は早く落ちる
    "control": ItemCurve(2, 0.65, 0.7),  # 遅くまで伸びる
    "home_run_avoidance": ItemCurve(0, 1.0, 1.0),
    "stamina": ItemCurve(0, 0.5, 0.8),  # 動きが小さい
}


def curve_change(age: int) -> float:
    """年齢曲線の変化（普通型・基準の項目）。"""
    return AGE_CURVE[bisect_right(_CURVE_AGES, age) - 1][1]


def expected_change(item: ItemCurve, *, age: int, growth: GrowthType, value: int) -> float:
    """項目ひとつの、1年ぶんの変化の期待値（ぶれを除く）。

    成長型と項目の差で曲線を読む年齢をずらし、伸び（正）と衰え（負）に別々の倍率をかける。
    成長型の倍率は伸びにだけ効く（早熟でも衰えは速くならない）。
    """
    change = curve_change(age + GROWTH_SHIFT[growth] - item.shift)
    if change > 0:
        change *= item.gain * GROWTH_GAIN[growth]
        if value >= HIGH_RATING:
            change *= HIGH_RATING_GAIN
        return change
    return change * item.decline


def _aged(value: int, item: ItemCurve, *, age: int, growth: GrowthType, shared: float, rng: GameRandom) -> int:
    change = expected_change(item, age=age, growth=growth, value=value) + shared + normal(rng, 0.0, ITEM_SD)
    return min(RATING_MAX, max(RATING_MIN, round(value + change)))


def age_ratings(
    ratings: BatterRatings | PitcherRatings, *, age: int, rng: GameRandom
) -> BatterRatings | PitcherRatings:
    """1年ぶん進めた能力。成長型は変えない。乱数は共通のぶれ → 項目ごと（宣言の順）の順に引く。"""
    shared = normal(rng, 0.0, PLAYER_SD)
    curves = BATTER_CURVES if isinstance(ratings, BatterRatings) else PITCHER_CURVES
    values = {
        name: _aged(getattr(ratings, name), item, age=age, growth=ratings.growth, shared=shared, rng=rng)
        for name, item in curves.items()
    }
    if isinstance(ratings, BatterRatings):
        return BatterRatings(**values, growth=ratings.growth)
    return PitcherRatings(**values, growth=ratings.growth)
