"""引退（外国人は「退団」）の確率。**純粋な関数**で、DB にも Django にも触れない。

年齢で基本の確率を決め、能力・出場機会・外国人かどうか・入団からの年数で倍率をかける。
その確率に乱数（`random()` 1回）を当てるのは呼び出し側（`offseason.py`）。
表の値が**規則の出典**で、設計書 3.6 と 12.「P6a の詳細」に最終値を記録する。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

# この年齢以上は必ず引退する
MAX_PLAYING_AGE = 41

# 年齢ごとの基本の確率。(その年齢から, 確率) の昇順（MAX_PLAYING_AGE 以上は表を見ずに必ず引退）
BASE_CHANCE: tuple[tuple[int, float], ...] = (
    (0, 0.03),
    (24, 0.03),
    (27, 0.03),
    (30, 0.07),
    (33, 0.24),
    (35, 0.48),
    (37, 0.72),
    (39, 0.90),
)
_BASE_AGES = [age for age, _ in BASE_CHANCE]

# 能力（総合値）の倍率 `exp(−VALUE_SLOPE × (v − VALUE_CENTER))` を、この範囲に収める
VALUE_CENTER = 45.0
VALUE_SLOPE = 0.10
VALUE_FACTOR_RANGE = (0.3, 3.0)
# 出場機会（その年の1軍）。主力は引退しにくく、出場なしは引退しやすい
REGULAR_PLATE_APPEARANCES = 300
REGULAR_OUTS = 240
REGULAR_FACTOR = 0.5
IDLE_FACTOR = 1.5
IDLE_MIN_AGE = 25
# 外国人は退団しやすい
FOREIGN_FACTOR = 2.0
# 入団から間もない若手は守られる
ROOKIE_SEASONS = 2
ROOKIE_MAX_AGE = 24
ROOKIE_FACTOR = 0.7
# 倍率をかけた後の上限（強制の年齢を除く）
MAX_CHANCE = 0.95


@dataclass(frozen=True)
class PlayingTime:
    """その年の1軍での出場機会。打席数（野手）とアウト数（投手。投球回 × 3）。"""

    plate_appearances: int = 0
    outs: int = 0

    @property
    def is_regular(self) -> bool:
        """主力か。野手は300打席以上、投手は240アウト（80回）以上。"""
        return self.plate_appearances >= REGULAR_PLATE_APPEARANCES or self.outs >= REGULAR_OUTS

    @property
    def is_idle(self) -> bool:
        """1軍で出場が無かったか。"""
        return self.plate_appearances == 0 and self.outs == 0


def retirement_chance(
    *, age: int, value: float, playing_time: PlayingTime, is_foreign: bool, seasons_in_world: int
) -> float:
    """引退する確率（0〜1）。`MAX_PLAYING_AGE` 以上は必ず 1。

    `value` は能力の総合値（野手は打撃の総合値、投手は抑える力。捕手は守備力と打撃の大きい方）。
    `seasons_in_world` は、その年を含めて世界の球団にいた年数。
    """
    if age >= MAX_PLAYING_AGE:
        return 1.0
    chance = BASE_CHANCE[bisect_right(_BASE_AGES, age) - 1][1]
    low, high = VALUE_FACTOR_RANGE
    chance *= min(high, max(low, math.exp(-VALUE_SLOPE * (value - VALUE_CENTER))))
    if playing_time.is_regular:
        chance *= REGULAR_FACTOR
    elif playing_time.is_idle and age >= IDLE_MIN_AGE:
        chance *= IDLE_FACTOR
    if is_foreign:
        chance *= FOREIGN_FACTOR
    if seasons_in_world <= ROOKIE_SEASONS and age <= ROOKIE_MAX_AGE:
        chance *= ROOKIE_FACTOR
    return min(MAX_CHANCE, chance)
